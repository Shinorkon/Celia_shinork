"""
Worker Runtime — consumes orchestration.dispatched events, executes tasks
via LLM (reasoning roles) or SSH (executor role) behind policy gateway.

Hardened with correlation-id propagation, idempotency, retry/backoff,
circuit breaker, and dead-letter queue.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field
import httpx
from redis import Redis
import psycopg

from packages.telemetry import (
    init_logging,
    set_correlation_id,
    get_correlation_id,
    set_run_id,
    counter,
    histogram,
    start_span,
)
from packages.utils import DeadLetter, IdempotencyStore, CircuitBreaker, retry_with_backoff

from ssh_executor import SSHConfig, SSHExecutor, SSHResult, get_pool
from llm_client import (
    LiteLLMClient, LLMMessage, LLMResponse, ROLE_MODEL_MAP, TOOL_SCHEMAS,
    DEFAULT_MODEL, VISION_MODEL, build_system_prompt,
    tools_for_role, registered_tool_names, TOOL_POLICY_KEYS,
)

SERVICE_NAME = os.getenv("SERVICE_NAME", "worker-runtime")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    service: str
    status: str
    timestamp: str


class TaskRequest(BaseModel):
    run_id: str
    agent_role: Literal[
        "frontoffice",
        "planner",
        "executor",
        "coder",
        "scheduler",
        "comms",
        "document",
        "ops-monitor",
        "qa",
        "memory-writer",
        "ops-reflect",
        "life-reflect",
    ]
    text: str = Field(default="")
    command: str | None = None
    chat_id: str = ""
    thread_id: str = ""
    image_data_url: str = ""
    bypass_confirm: bool = False


class TaskResponse(BaseModel):
    run_id: str
    agent_role: str
    status: str
    output: str
    policy_reason: str | None = None
    ssh_result: dict | None = None


POLICY_GATEWAY_URL = os.getenv(
    "POLICY_GATEWAY_URL",
    "http://policy-gateway:8000/v1/policy/command/evaluate",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DISPATCH_STREAM = os.getenv("DISPATCH_STREAM", "orchestration.dispatched")
COMPLETION_STREAM = os.getenv("COMPLETION_STREAM", "worker.completed")
GROUP_NAME = os.getenv("WORKER_GROUP", "worker-group")
CONSUMER_NAME = os.getenv("WORKER_CONSUMER", "worker-1")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)
DEAD_LETTER_STREAM = os.getenv("DEAD_LETTER_STREAM", "dead.letter")
NOTIFICATION_STREAM = os.getenv("NOTIFICATION_STREAM", "notification.requested")
APPROVAL_TIMEOUT_MINUTES = int(os.getenv("SECURITY_APPROVAL_TIMEOUT_MINUTES", "30"))

# SSH defaults
SSH_HOST = os.getenv("SSH_HOST", "")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER", "root")
SSH_KEY_FILE = os.getenv("SSH_KEY_FILE", "") or None
SSH_ENABLED = bool(SSH_HOST)

# Circuit breakers per downstream
_policy_cb = CircuitBreaker("policy-gateway", threshold=5, timeout_seconds=30)


class ProcessOnceResponse(BaseModel):
    processed: bool
    detail: str
    run_id: str | None = None
    status: str | None = None


def _redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _dead_letter() -> DeadLetter:
    return DeadLetter(_redis(), DEAD_LETTER_STREAM)


def _idempotency() -> IdempotencyStore:
    return IdempotencyStore(_redis(), prefix="worker:idem", ttl_seconds=7200)


def _persist_completion(run_ref: str, result: TaskResponse) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orchestration_runs
                SET status = %s,
                    current_agent = %s,
                    ended_at = NOW(),
                    error = %s
                WHERE run_ref = %s
                RETURNING id
                """,
                (
                    result.status,
                    result.agent_role,
                    result.output if result.status in ("failed", "blocked") else None,
                    run_ref,
                ),
            )
            row = cur.fetchone()
            run_db_id = row[0] if row else None

            if run_db_id is not None:
                cur.execute(
                    """
                    INSERT INTO checkpoints(run_id, step_index, state_jsonb)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (
                        run_db_id,
                        1,
                        json.dumps(
                            {
                                "status": result.status,
                                "agent_role": result.agent_role,
                                "output": result.output,
                                "policy_reason": result.policy_reason,
                                "ssh_result": result.ssh_result,
                            }
                        ),
                    ),
                )

                if result.agent_role == "executor":
                    cur.execute(
                        """
                        INSERT INTO tool_calls(run_id, tool_name, request_jsonb, response_jsonb, allowed, reason)
                        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s)
                        """,
                        (
                            run_db_id,
                            "executor_command",
                            json.dumps({"run_ref": run_ref}),
                            json.dumps(
                                {
                                    "status": result.status,
                                    "output": result.output,
                                    "policy_reason": result.policy_reason,
                                    "ssh_result": result.ssh_result,
                                }
                            ),
                            result.status == "completed",
                            result.policy_reason,
                        ),
                    )

            cur.execute(
                """
                INSERT INTO audit_logs(actor_type, actor_id, action, target_type, target_id, metadata_jsonb)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    "service",
                    SERVICE_NAME,
                    "worker_completed",
                    "run",
                    run_ref,
                    json.dumps(
                        {
                            "status": result.status,
                            "agent_role": result.agent_role,
                        }
                    ),
                ),
            )
        conn.commit()


def _log_usage(
    run_ref: str,
    agent_role: str,
    llm_response: LLMResponse,
) -> None:
    """Persist a usage event for cost tracking."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usage_events(scope_type, scope_id, provider, model,
                                             prompt_tokens, completion_tokens, latency_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "run",
                        run_ref,
                        "litellm",
                        llm_response.model,
                        llm_response.prompt_tokens,
                        llm_response.completion_tokens,
                        int(llm_response.latency_ms),
                    ),
                )
            conn.commit()
    except Exception:
        pass


@app.on_event("startup")
def startup() -> None:
    r = _redis()
    try:
        r.xgroup_create(DISPATCH_STREAM, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass
    t = threading.Thread(target=_poll_dispatch_stream, daemon=True)
    t.start()
    logger.info("worker_started")


def _poll_dispatch_stream() -> None:
    """Continuously poll the dispatch stream for new tasks (runs in daemon thread)."""
    while True:
        try:
            r = _redis()
            entries = r.xreadgroup(GROUP_NAME, CONSUMER_NAME, streams={DISPATCH_STREAM: ">"}, count=1, block=2000)
            if not entries:
                continue
            _, messages = entries[0]
            message_id, fields = messages[0]
            _handle_task(message_id, fields)
        except Exception as exc:
            logger.error(f"worker_poll_error: {exc}")
            time.sleep(1)


def _handle_task(message_id: str, fields: dict) -> None:
    """Process a single task from the dispatch stream."""
    r = _redis()
    span = start_span("worker.process_task")
    payload_raw = fields.get("payload", "{}")
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        r.xack(DISPATCH_STREAM, GROUP_NAME, message_id)
        span.end()
        return

    run_id = payload.get("run_id", str(uuid.uuid4()))
    cid = payload.get("correlation_id", run_id)
    set_correlation_id(cid)
    set_run_id(run_id)

    idem_key = payload.get("event_id", message_id)
    if _idempotency().is_duplicate(idem_key):
        counter("worker.duplicate_event")
        r.xack(DISPATCH_STREAM, GROUP_NAME, message_id)
        span.end()
        return

    agent_role = payload.get("agent_role", "frontoffice")
    text = payload.get("text", "")
    user_id = payload.get("user_id", "")
    chat_id = payload.get("chat_id", "")
    thread_id = payload.get("thread_id", "")
    image_data_url = payload.get("image_data_url", "")

    # Fetch conversation history for this chat
    history: list[LLMMessage] = _load_history(chat_id)

    bypass_confirm = bool(payload.get("bypass_confirm", False))

    try:
        if agent_role == "executor":
            result = _run_executor_command(
                run_id, text, chat_id=chat_id, thread_id=thread_id, bypass_confirm=bypass_confirm,
            )
        elif agent_role == "coder":
            result = _run_coder_agent(
                run_id, text, history=history, chat_id=chat_id, thread_id=thread_id,
                image_data_url=image_data_url, user_id=user_id,
            )
        else:
            result = _run_llm_agent(
                run_id, agent_role, text, history=history, chat_id=chat_id, thread_id=thread_id,
                image_data_url=image_data_url, user_id=user_id,
            )
    except Exception as exc:
        logger.error(f"task_execution_failed: run_id={run_id} error={exc}")
        result = TaskResponse(
            run_id=run_id, agent_role=agent_role, status="failed",
            output=str(exc), policy_reason=None, ssh_result=None,
        )
        _dead_letter().publish(
            original_payload=payload, error=str(exc),
            source="worker-runtime", correlation_id=cid,
        )

    try:
        _persist_completion(run_id, result)
    except Exception as exc:
        logger.error(f"persist_completion_failed: {exc}")

    # Persist chat history so the bot remembers conversations
    if agent_role != "executor" and result.status == "completed":
        try:
            _save_messages(chat_id, text, result.output)
        except Exception as exc2:
            logger.warning(f"save_messages_failed: {exc2}")

        # Best-effort, fire-and-forget: decide if anything in this exchange
        # is worth remembering long-term. Runs in a background thread so a
        # slow or failed memory-writer call never delays the user-facing
        # reply, which is already on its way via the completion event below.
        threading.Thread(
            target=_run_memory_writer,
            args=(run_id, text, result.output, user_id, chat_id),
            daemon=True,
        ).start()

    completion_event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id, "agent_role": agent_role,
        "status": result.status, "output": result.output,
        "correlation_id": cid,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }
    r.xadd(COMPLETION_STREAM, {"payload": json.dumps(completion_event)})
    r.xack(DISPATCH_STREAM, GROUP_NAME, message_id)
    counter(f"worker.{agent_role}.completed")
    span.end()


@app.on_event("shutdown")
def shutdown() -> None:
    pool = get_pool()
    pool.close_all()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        service=SERVICE_NAME,
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE_NAME, "message": "ready"}


# ---------------------------------------------------------------------------
# Executor: SSH command execution with policy-gateway guard
# ---------------------------------------------------------------------------


def _publish_notification(chat_id: str, thread_id: str, text: str, priority: str = "normal") -> None:
    """Publish to NOTIFICATION_STREAM - consumed by telegram-ingress's
    _poll_notifications() and delivered the same way completions are."""
    if not chat_id:
        return
    try:
        r = _redis()
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target_user_id": "",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "text": text,
            "priority": priority,
        }
        r.xadd(NOTIFICATION_STREAM, {"payload": json.dumps(event)})
    except Exception as exc:
        logger.warning(f"publish_notification_error: {exc}")


def _create_pending_approval(
    run_ref: str, command: str, policy_reason: str | None, chat_id: str, thread_id: str,
) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_approvals(run_ref, command, policy_reason, chat_id, thread_id, expires_at)
                VALUES (%s, %s, %s, %s, %s, NOW() + (%s || ' minutes')::interval)
                """,
                (run_ref, command, policy_reason, chat_id, thread_id, str(APPROVAL_TIMEOUT_MINUTES)),
            )
        conn.commit()


def _run_executor_command(
    run_id: str,
    command: str,
    chat_id: str = "",
    thread_id: str = "",
    bypass_confirm: bool = False,
) -> TaskResponse:
    """Evaluate the command against the policy gateway, then execute via SSH.

    Tiering (see policy-gateway/app/authority_tiers.py): `autonomous` runs
    immediately exactly as before; `notify_after` runs immediately and sends
    a summary notification afterward; `confirm_first` pauses and asks for
    confirmation via Telegram instead of executing at all, unless
    bypass_confirm=True — set when telegram-ingress is replaying a command
    the user just approved, so it isn't asked to confirm the same thing twice.
    """
    # 1. Policy evaluation
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(POLICY_GATEWAY_URL, json={"command": command})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return TaskResponse(
            run_id=run_id,
            agent_role="executor",
            status="failed",
            output=f"Policy check failed: {exc}",
        )

    if not data.get("allowed", False):
        return TaskResponse(
            run_id=run_id,
            agent_role="executor",
            status="blocked",
            output="Executor command blocked by policy gateway.",
            policy_reason=data.get("reason_code"),
        )

    normalized_command = data.get("normalized_command", command)
    tier = data.get("tier", "confirm_first")

    if tier == "confirm_first" and not bypass_confirm:
        if not chat_id:
            # No chat to ask through (e.g. a direct /task API call, not the
            # Telegram-driven flow) - can't get a confirmation, so it doesn't
            # run rather than silently skipping the safeguard.
            return TaskResponse(
                run_id=run_id,
                agent_role="executor",
                status="blocked",
                output="This command needs confirmation, but there's no chat to ask through.",
                policy_reason=data.get("reason_code"),
            )
        _create_pending_approval(run_id, normalized_command, data.get("reason_code"), chat_id, thread_id)
        _publish_notification(
            chat_id, thread_id,
            f"⏸️ Want to run this — reply YES to confirm or NO to cancel "
            f"(expires in {APPROVAL_TIMEOUT_MINUTES}m):\n`{normalized_command}`",
            priority="high",
        )
        return TaskResponse(
            run_id=run_id,
            agent_role="executor",
            status="awaiting_approval",
            output=f"Sent a confirmation request for: {normalized_command}",
            policy_reason=data.get("reason_code"),
        )

    # 2. SSH execution (if configured)
    if not SSH_ENABLED:
        return TaskResponse(
            run_id=run_id,
            agent_role="executor",
            status="completed",
            output=f"Executor command approved (SSH disabled – dry run): {normalized_command}",
            policy_reason=data.get("reason_code"),
        )

    ssh_config = SSHConfig(
        host=SSH_HOST,
        port=SSH_PORT,
        username=SSH_USER,
        key_file=SSH_KEY_FILE,
    )

    try:
        pool = get_pool()
        with pool.acquire(ssh_config) as ssh:
            result: SSHResult = ssh.run(normalized_command)
    except RuntimeError as exc:
        return TaskResponse(
            run_id=run_id,
            agent_role="executor",
            status="failed",
            output=f"SSH execution error: {exc}",
            policy_reason=data.get("reason_code"),
        )

    exit_ok = result.exit_code == 0
    status = "completed" if exit_ok else "failed"
    output = (
        f"Exit: {result.exit_code} | {result.duration_ms:.0f}ms"
        f"{' [TRUNCATED]' if result.truncated else ''}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )

    if tier == "notify_after" and not bypass_confirm:
        _publish_notification(
            chat_id, thread_id,
            f"✅ Ran automatically: `{normalized_command}` — {'completed' if exit_ok else 'failed'}",
        )

    return TaskResponse(
        run_id=run_id,
        agent_role="executor",
        status=status,
        output=output,
        policy_reason=data.get("reason_code"),
        ssh_result={
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
        },
    )


# ---------------------------------------------------------------------------
# LLM-powered agent worker (non-executor roles)
# ---------------------------------------------------------------------------


def _load_history(chat_id: str, limit: int = 20) -> list[LLMMessage]:
    """Fetch recent conversation history for a chat from the DB."""
    if not chat_id:
        return []
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.direction, m.payload_jsonb
                    FROM messages m
                    JOIN threads t ON t.id = m.thread_id
                    WHERE t.telegram_chat_id = %s
                    ORDER BY m.created_at ASC
                    LIMIT %s
                    """,
                    (int(chat_id), limit * 2),  # 2x because each turn is user+assistant
                )
                rows = cur.fetchall()
        history: list[LLMMessage] = []
        for direction, payload in rows:
            content = ""
            if isinstance(payload, dict):
                content = payload.get("text", "") or payload.get("output", "")
            role = "user" if direction == "inbound" else "assistant"
            if content:
                history.append(LLMMessage(role=role, content=content))
        return history[-limit * 2:]  # keep most recent
    except Exception as exc:
        logger.warning(f"load_history_error: {exc}")
        return []



# ---------------------------------------------------------------------------
# Memory foundation helpers (Life OS slice 1)
# ---------------------------------------------------------------------------

_KIND_TO_SEGMENT = {
    "preference": "semantic",
    "goal": "semantic",
    "fact": "semantic",
    "habit": "procedural",
    "note": "semantic",
    "correction": "semantic",
    "decision": "episodic",
    "project_state": "episodic",
    "event": "episodic",
}

_JUNK_TITLE_RE = re.compile(
    r"(?i)\b(?:quantity|spending\s+total|receipt\s+processed|updated\s+spending|"
    r"condensed\s+milk\s+quantity)\b"
)
_JUNK_BODY_RE = re.compile(
    r"(?i)(?:\b\d+\s*(?:units?|pcs|pieces)\b|"
    r"total\s+spending\s+updated|"
    r"processed\s+a\s+receipt|"
    r"wants?\s+\d+\s+units?\s+of|"
    r"calculate\s+(?:their|your|my)?\s*total\s+spending)"
)


def _is_junk_memory_item(kind: str, title: str, body: str) -> bool:
    kind = (kind or "").lower()
    title = title or ""
    body = body or ""
    if _JUNK_TITLE_RE.search(title) or _JUNK_BODY_RE.search(body):
        return True
    if kind == "preference" and re.search(r"(?i)\b\d+\s*(?:units?|pcs|x)\b", body):
        return True
    if kind in ("project_state", "goal") and re.search(
        r"(?i)\b(?:receipt|spending\s+total|total\s+spending|mvr)\b", title + " " + body
    ):
        if re.search(r"(?i)\b(?:total|processed|invoice|receipt)\b", title + " " + body):
            return True
    return False


def _segment_for_kind(kind: str, explicit: str | None = None) -> str:
    if explicit in ("episodic", "semantic", "procedural", "working"):
        return explicit
    return _KIND_TO_SEGMENT.get((kind or "").lower(), "semantic")


def _resolve_db_user_id(telegram_user_id: str | int | None) -> int | None:
    if telegram_user_id is None or telegram_user_id == "":
        return None
    try:
        tid = int(telegram_user_id)
    except (TypeError, ValueError):
        return None
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE telegram_user_id = %s",
                    (tid,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception as exc:
        logger.warning(f"resolve_db_user_id_error: {exc}")
        return None


def _load_memory_context(
    limit: int = 10,
    *,
    user_id: str = "",
    query: str = "",
    chat_id: str = "",
) -> str | None:
    """Segment-aware retrieval: working + semantic + episodic + procedural.

    Uses tsvector / tags / recency — not blind top-N only. Isolates by user_id.
    Soft-forgotten rows (forgotten_at set) are excluded.
    """
    db_uid = _resolve_db_user_id(user_id)
    if db_uid is None:
        return None
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                q = (query or "").strip()
                budgets = {
                    "working": 3,
                    "semantic": max(3, limit // 2),
                    "episodic": max(2, limit // 3),
                    "procedural": 2,
                }
                lines: list[str] = []
                touched: list[int] = []
                for segment, seg_limit in budgets.items():
                    if q:
                        cur.execute(
                            """
                            SELECT id, kind, segment, title, body, project_ref,
                                   ts_rank(search_tsv, plainto_tsquery('english', %s)) AS rank
                            FROM memory_items
                            WHERE status = 'active'
                              AND forgotten_at IS NULL
                              AND segment = %s
                              AND user_id = %s
                              AND (
                                search_tsv @@ plainto_tsquery('english', %s)
                                OR title ILIKE '%%' || %s || '%%'
                                OR body ILIKE '%%' || %s || '%%'
                                OR %s = ANY(tags)
                                OR segment = 'working'
                              )
                            ORDER BY
                              CASE WHEN segment = 'working' THEN 0 ELSE 1 END,
                              rank DESC,
                              importance DESC,
                              salience DESC,
                              COALESCE(last_accessed_at, updated_at) DESC
                            LIMIT %s
                            """,
                            (q, segment, db_uid, q, q, q, q.lower(), seg_limit),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT id, kind, segment, title, body, project_ref, 0.0 AS rank
                            FROM memory_items
                            WHERE status = 'active'
                              AND forgotten_at IS NULL
                              AND segment = %s
                              AND user_id = %s
                            ORDER BY
                              importance DESC,
                              salience DESC,
                              COALESCE(last_accessed_at, updated_at) DESC
                            LIMIT %s
                            """,
                            (segment, db_uid, seg_limit),
                        )
                    for row in cur.fetchall():
                        mid, kind, seg, title, body, project_ref, _rank = row
                        touched.append(int(mid))
                        scope = f" (project: {project_ref})" if project_ref else ""
                        lines.append(f"- [{seg}/{kind}] {title}: {body}{scope}")

                if touched:
                    cur.execute(
                        """
                        UPDATE memory_items
                        SET last_accessed_at = NOW(),
                            access_count = access_count + 1
                        WHERE id = ANY(%s)
                        """,
                        (touched,),
                    )
                conn.commit()
        if not lines:
            return None
        return "Known context from prior conversations:\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning(f"load_memory_context_error: {exc}")
        return None


def _save_messages(chat_id: str, user_text: str, assistant_output: str) -> None:
    """Persist user message and assistant response to the messages table."""
    if not chat_id:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Ensure thread exists
                cur.execute(
                    "SELECT id FROM threads WHERE telegram_chat_id = %s",
                    (int(chat_id),),
                )
                row = cur.fetchone()
                if row:
                    thread_db_id = row[0]
                    cur.execute(
                        "UPDATE threads SET updated_at = NOW() WHERE id = %s",
                        (thread_db_id,),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO threads(telegram_chat_id, status)
                        VALUES (%s, 'active')
                        RETURNING id
                        """,
                        (int(chat_id),),
                    )
                    thread_db_id = cur.fetchone()[0]

                # Save user message
                cur.execute(
                    """
                    INSERT INTO messages(thread_id, direction, payload_jsonb)
                    VALUES (%s, 'inbound', %s::jsonb)
                    """,
                    (thread_db_id, json.dumps({"text": user_text})),
                )

                # Save assistant response
                cur.execute(
                    """
                    INSERT INTO messages(thread_id, direction, payload_jsonb)
                    VALUES (%s, 'outbound', %s::jsonb)
                    """,
                    (thread_db_id, json.dumps({"output": assistant_output})),
                )
            conn.commit()
    except Exception as exc:
        logger.warning(f"save_messages_error: {exc}")


# ---------------------------------------------------------------------------
# Tool-calling agent loop — replaces the old regex EXEC: scanning.
#
# Commands only run when the model emits a structured tool_calls entry
# (an actual API field returned by the provider), never because free text in
# `response.content` happens to contain a line that looks like a command.
# That's what closes the prompt-injection path: a file the coder agent reads
# via a tool call can contain arbitrary text — including something that
# *looks* like a command — and it cannot self-execute, because nothing here
# scans message content for anything anymore.
# ---------------------------------------------------------------------------


def _extract_shell_command(tool_call: dict) -> str | None:
    function = tool_call.get("function", {})
    if function.get("name") != "run_shell_command":
        return None
    try:
        args = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        return None
    command = args.get("command")
    return command.strip() if isinstance(command, str) and command.strip() else None



def _quiet_proactive_text(text: str) -> str:
    """Mirror ingress quiet_mode for life-reflect notify_user (no brochure/✅)."""
    import re
    s = (text or "").strip()
    s = re.sub(r"^[\u2705\u274c\u2139\ufe0f✅❌ℹ️]+\s*", "", s)
    banned = re.compile(
        r"(?i)\b(?:"
        r"shnuk|budgy|directors?\s*eye|"
        r"frontdesk|ops\s*team|as an ai|"
        r"i can (?:definitely )?(?:help|check|list|do)|"
        r"here'?s what i can|capability|"
        r"humanised agent|smart sidekick|"
        r"i'?m here (?:for you|to help)|"
        r"(?:on\s+)?(?:the\s+)?vps\b|"
        r"disk\s*space|container\s+status|health\s+check|"
        r"/opt\b|/root/Celia|/home/shino|"
        r"agent_orchestration_platform|"
        r"weekly\s+digest|monthly\s+digest|money\s+digest"
        r")\b"
    )
    parts = re.split(r"(?<=[.!?])\s+|\n+", s)
    kept = [p.strip() for p in parts if p.strip() and not banned.search(p)]
    return " ".join(kept).strip()


def _life_reflect_notify_allowed(chat_id: str) -> bool:
    """Extra rate limit on successful life-reflect pings (Redis)."""
    if not chat_id:
        return False
    min_h = float(os.getenv("LIFE_REFLECT_NOTIFY_MIN_HOURS", "6"))
    key = f"celia:life_reflect:last_notify:{chat_id}"
    try:
        r = _redis()
        if r.get(key):
            return False
        r.setex(key, max(60, int(min_h * 3600)), datetime.now(timezone.utc).isoformat())
        return True
    except Exception as exc:
        logger.warning(f"life_reflect_notify_rate_error: {exc}")
        return True


def _handle_recall_memory_call(
    tool_call: dict, *, user_id: str = "", chat_id: str = ""
) -> str | None:
    """Returns tool-result string if this was recall_memory, else None."""
    function = tool_call.get("function", {})
    if function.get("name") != "recall_memory":
        return None
    try:
        args = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    query = args.get("query") if isinstance(args.get("query"), str) else ""
    query = query.strip()
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 15))
    db_uid = _resolve_db_user_id(user_id or chat_id)
    if db_uid is None:
        return "No user memory context available."
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                if query:
                    cur.execute(
                        """
                        SELECT kind, segment, title, body
                        FROM memory_items
                        WHERE status = 'active'
                          AND forgotten_at IS NULL
                          AND user_id = %s
                          AND (
                            search_tsv @@ plainto_tsquery('english', %s)
                            OR title ILIKE '%%' || %s || '%%'
                            OR body ILIKE '%%' || %s || '%%'
                            OR %s = ANY(tags)
                          )
                        ORDER BY importance DESC, salience DESC,
                                 COALESCE(last_accessed_at, updated_at) DESC
                        LIMIT %s
                        """,
                        (db_uid, query, query, query, query.lower(), limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT kind, segment, title, body
                        FROM memory_items
                        WHERE status = 'active'
                          AND forgotten_at IS NULL
                          AND user_id = %s
                          AND kind IN ('preference', 'correction', 'fact', 'note', 'goal')
                        ORDER BY importance DESC, salience DESC,
                                 COALESCE(last_accessed_at, updated_at) DESC
                        LIMIT %s
                        """,
                        (db_uid, limit),
                    )
                rows = cur.fetchall()
                # Touch last_accessed for salience hygiene (best-effort)
                if rows:
                    cur.execute(
                        """
                        UPDATE memory_items SET last_accessed_at = NOW()
                        WHERE user_id = %s AND status = 'active'
                          AND forgotten_at IS NULL
                          AND title = ANY(%s)
                        """,
                        (db_uid, [r[2] for r in rows]),
                    )
            conn.commit()
    except Exception as exc:
        logger.warning(f"recall_memory_error: {exc}")
        return f"Memory recall failed: {exc}"
    if not rows:
        return "No matching memories."
    lines = [f"- [{kind}/{seg}] {title}: {body}" for kind, seg, title, body in rows]
    return "Memories:\n" + "\n".join(lines)


def _handle_notify_user_call(
    tool_call: dict,
    chat_id: str,
    thread_id: str,
    *,
    agent_role: str = "",
) -> str | None:
    """Returns a tool-result string if this call was a notify_user request
    (handled here), or None if it wasn't one at all."""
    function = tool_call.get("function", {})
    if function.get("name") != "notify_user":
        return None
    try:
        args = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    text = args.get("text")
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return "Notification not sent (empty text)."
    if not chat_id:
        return "Notification not sent (no chat context available)."
    role = (agent_role or "").lower()
    if role == "life-reflect":
        text = _quiet_proactive_text(text)
        if not text:
            return "Notification not sent (quiet strip removed all content)."
        if not _life_reflect_notify_allowed(chat_id):
            return "Notification not sent (rate-limited; try later)."
    _publish_notification(chat_id, thread_id, text)
    return "Notification sent to the user."


def _format_exec_result_for_model(exec_result: TaskResponse) -> str:
    if exec_result.status == "completed":
        lines = exec_result.output.split("\n")
        stdout_lines: list[str] = []
        in_stdout = False
        for line in lines:
            if line.startswith("STDOUT:"):
                in_stdout = True
                continue
            if line.startswith("STDERR:"):
                break
            if in_stdout:
                stdout_lines.append(line)
        clean = "\n".join(stdout_lines).strip()
        return clean or exec_result.output[:500]
    if exec_result.status == "blocked":
        return f"BLOCKED by policy: {exec_result.policy_reason}"
    if exec_result.status == "awaiting_approval":
        return f"PENDING CONFIRMATION: {exec_result.output} (a request was sent to the user via Telegram)"
    return f"FAILED: {exec_result.output[:300]}"


def _run_tool_calling_agent(
    run_id: str,
    role: str,
    text: str,
    history: list[LLMMessage] | None,
    max_turns: int,
    chat_id: str = "",
    thread_id: str = "",
    image_data_url: str = "",
    user_id: str = "",
) -> TaskResponse:
    """Shared multi-turn loop: call the LLM (with tool access if the role has
    any registered in TOOL_SCHEMAS), execute any requested tool calls through
    the policy-gated executor, feed results back as a `tool` message, and
    repeat until the model stops requesting tools or max_turns is hit."""
    client = LiteLLMClient()
    # An attached image forces a vision-capable model for this turn,
    # overriding the role's usual (mostly text-only) model - see VISION_MODEL.
    model = VISION_MODEL if image_data_url else ROLE_MODEL_MAP.get(role, DEFAULT_MODEL)
    tools = tools_for_role(role) or None

    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=build_system_prompt(role)),
    ]
    memory_context = _load_memory_context(user_id=user_id, query=text, chat_id=chat_id)
    if memory_context:
        messages.append(LLMMessage(role="system", content=memory_context))
    if history:
        messages.extend(history)

    if image_data_url:
        user_content: str | list[dict] = [
            {"type": "text", "text": text or "What's in this image?"},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
    else:
        user_content = text
    messages.append(LLMMessage(role="user", content=user_content))

    last_response: LLMResponse | None = None

    try:
        for turn in range(max_turns):
            response = client.chat(messages, model=model, tools=tools)
            last_response = response
            messages.append(
                LLMMessage(role="assistant", content=response.content, tool_calls=response.tool_calls)
            )

            if not response.tool_calls:
                _log_usage(run_id, role, response)
                return TaskResponse(
                    run_id=run_id, agent_role=role,
                    status="completed", output=response.content,
                )

            for call in response.tool_calls:
                notify_result = _handle_notify_user_call(
                    call, chat_id, thread_id, agent_role=role
                )
                if notify_result is not None:
                    result_text = notify_result
                else:
                    recall_result = _handle_recall_memory_call(
                        call, user_id=user_id, chat_id=chat_id
                    )
                    if recall_result is not None:
                        result_text = recall_result
                    else:
                        # life-reflect must never run shell even if schema drifts
                        if role == "life-reflect":
                            result_text = "(tool not allowed for life-reflect)"
                        else:
                            command = _extract_shell_command(call)
                            if command is None:
                                result_text = "(unsupported tool call)"
                            else:
                                exec_result = _run_executor_command(
                                    f"{run_id}-{role}-{turn}", command,
                                    chat_id=chat_id, thread_id=thread_id,
                                )
                                result_text = _format_exec_result_for_model(exec_result)
                messages.append(
                    LLMMessage(role="tool", content=result_text, tool_call_id=call.get("id"))
                )

        if last_response is not None:
            _log_usage(run_id, role, last_response)
        return TaskResponse(
            run_id=run_id,
            agent_role=role,
            status="completed",
            output=(
                (last_response.content if last_response and last_response.content else "(no output)")
                + "\n\n_(max turns reached — task may be incomplete)_"
            ),
        )
    except Exception as exc:
        return TaskResponse(
            run_id=run_id,
            agent_role=role,
            status="failed",
            output=f"Agent error: {exc}",
        )
    finally:
        client.close()


def _run_coder_agent(
    run_id: str, text: str, history: list[LLMMessage] | None = None,
    chat_id: str = "", thread_id: str = "", image_data_url: str = "",
    user_id: str = "",
) -> TaskResponse:
    return _run_tool_calling_agent(
        run_id, "coder", text, history, max_turns=5, chat_id=chat_id, thread_id=thread_id,
        image_data_url=image_data_url, user_id=user_id,
    )


# Most conversational roles only ever need a turn or two of tool use, if
# any. ops-reflect is the exception: its whole job is checking several
# independent signals (services, disk, docker, git across projects) before
# deciding whether anything's worth surfacing, so 3 turns isn't enough
# headroom to reach a conclusion rather than just running out mid-check —
# observed live (it hit the cap after service-status + disk checks, still
# wanting to look at docker and git). Give it the same budget as coder.
_MAX_TURNS_BY_ROLE: dict[str, int] = {
    "ops-reflect": 5,
    "life-reflect": 3,
}
_DEFAULT_LLM_AGENT_MAX_TURNS = 3


def _run_llm_agent(
    run_id: str, agent_role: str, text: str,
    history: list[LLMMessage] | None = None,
    chat_id: str = "", thread_id: str = "", image_data_url: str = "",
    user_id: str = "",
) -> TaskResponse:
    max_turns = _MAX_TURNS_BY_ROLE.get(agent_role, _DEFAULT_LLM_AGENT_MAX_TURNS)
    return _run_tool_calling_agent(
        run_id, agent_role, text, history, max_turns=max_turns, chat_id=chat_id, thread_id=thread_id,
        image_data_url=image_data_url, user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Memory writer — best-effort, fire-and-forget judgment call on whether a
# just-completed exchange contains anything worth remembering long-term.
# Runs in a background thread (see _handle_task); failures here never affect
# the user-facing reply, which has already been sent by the time this runs.
# ---------------------------------------------------------------------------


def _run_memory_writer(run_id: str, user_text: str, assistant_output: str, user_id: str = "", chat_id: str = "") -> None:
    try:
        client = LiteLLMClient()
        try:
            model = ROLE_MODEL_MAP.get("memory-writer", DEFAULT_MODEL)
            exchange = f"User: {user_text}\n\nCarlia: {assistant_output}"
            messages = [
                LLMMessage(role="system", content=build_system_prompt("memory-writer")),
                LLMMessage(role="user", content=exchange),
            ]
            response = client.chat(messages, model=model, tools=TOOL_SCHEMAS.get("memory-writer"))
        finally:
            client.close()

        if not response.tool_calls:
            return  # the common case: nothing in this exchange was memory-worthy

        items: list[dict] = []
        for call in response.tool_calls:
            function = call.get("function", {})
            if function.get("name") != "save_memory_items":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            items.extend(args.get("items") or [])

        valid_items = []
        for item in items:
            kind = item.get("kind")
            title = item.get("title")
            body = item.get("body")
            if not (kind and title and body):
                continue
            if _is_junk_memory_item(kind, title, body):
                logger.info(
                    f"memory_junk_filtered: run_id={run_id} kind={kind} title={title[:80]}"
                )
                continue
            valid_items.append(item)
        if not valid_items:
            return

        db_uid = _resolve_db_user_id(user_id) if user_id else None
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for item in valid_items:
                    kind = item["kind"]
                    segment = _segment_for_kind(kind, item.get("segment"))
                    cur.execute(
                        """
                        INSERT INTO memory_items(
                          kind, title, body, tags, project_ref, source_run_ref,
                          user_id, segment, source_chat_id, importance, salience
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            kind,
                            item["title"],
                            item["body"],
                            item.get("tags") or [],
                            item.get("project_ref"),
                            run_id,
                            db_uid,
                            segment,
                            chat_id or None,
                            float(item.get("importance") or 0.5),
                            float(item.get("salience") or 0.5),
                        ),
                    )
            conn.commit()
        logger.info(f"memory_items_saved: run_id={run_id} count={len(valid_items)}")
    except Exception as exc:
        logger.warning(f"memory_writer_error: {exc}")


@app.post("/task", response_model=TaskResponse)
def run_task(payload: TaskRequest) -> TaskResponse:
    # Executor path: requires command → policy → SSH
    if payload.agent_role == "executor":
        command = (payload.command or payload.text).strip()
        if not command:
            return TaskResponse(
                run_id=payload.run_id,
                agent_role=payload.agent_role,
                status="failed",
                output="Executor task missing command input.",
            )
        return _run_executor_command(
            payload.run_id, command, chat_id=payload.chat_id, thread_id=payload.thread_id,
            bypass_confirm=payload.bypass_confirm,
        )

    if payload.agent_role == "coder":
        return _run_coder_agent(
            payload.run_id, payload.text, chat_id=payload.chat_id, thread_id=payload.thread_id,
            image_data_url=payload.image_data_url, user_id=getattr(payload, "user_id", "") or "",
        )

    # Non-executor path: LLM-powered reasoning
    return _run_llm_agent(
        payload.run_id, payload.agent_role, payload.text,
        chat_id=payload.chat_id, thread_id=payload.thread_id,
        image_data_url=payload.image_data_url,
    )


@app.post("/process-next", response_model=ProcessOnceResponse)
def process_next() -> ProcessOnceResponse:
    span = start_span("worker.process_next")
    try:
        r = _redis()
        entries = r.xreadgroup(GROUP_NAME, CONSUMER_NAME, streams={DISPATCH_STREAM: ">"}, count=1, block=50)
        if not entries:
            return ProcessOnceResponse(processed=False, detail="no_messages")

        _, messages = entries[0]
        message_id, fields = messages[0]
        payload = json.loads(fields.get("payload", "{}"))

        cid = payload.get("correlation_id", str(uuid.uuid4()))
        set_correlation_id(cid)

        run_id = payload.get("run_id", str(uuid.uuid4()))
        set_run_id(run_id)

        # Idempotency check
        idem_key = payload.get("event_id", message_id)
        if _idempotency().is_duplicate(idem_key):
            counter("worker.duplicate_event")
            r.xack(DISPATCH_STREAM, GROUP_NAME, message_id)
            span.end()
            return ProcessOnceResponse(processed=False, detail="duplicate_event")

        # Circuit breaker for policy gateway
        if _policy_cb.is_open:
            _dead_letter().publish(
                original_payload=payload, error="circuit_open_policy_gateway",
                source="worker-runtime", correlation_id=cid,
            )
            r.xack(DISPATCH_STREAM, GROUP_NAME, message_id)
            counter("worker.circuit_open")
            span.end()
            return ProcessOnceResponse(processed=False, detail="circuit_open")

        request = TaskRequest(
            run_id=run_id,
            agent_role=payload.get("agent_role", "frontoffice"),
            text=payload.get("text", ""),
            image_data_url=payload.get("image_data_url", ""),
        )
        result = run_task(request)

        if result.status == "failed":
            _policy_cb.failure()
        else:
            _policy_cb.success()

        try:
            _persist_completion(request.run_id, result)
        except Exception as exc:
            logger.error(f"persist_completion_failed: {exc}")
            _dead_letter().publish(
                original_payload=payload, error=str(exc),
                source="worker-runtime", correlation_id=cid,
            )
            counter("worker.dead_letter")

        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": result.run_id, "agent_role": result.agent_role,
            "status": result.status, "output": result.output,
            "policy_reason": result.policy_reason or "",
            "ssh_result": result.ssh_result,
            "correlation_id": cid,
        }
        r.xadd(COMPLETION_STREAM, {"payload": json.dumps(event)})
        r.xack(DISPATCH_STREAM, GROUP_NAME, message_id)
        counter("worker.task_completed")
        span.end()
        return ProcessOnceResponse(processed=True, detail="completed", run_id=result.run_id, status=result.status)
    except Exception as exc:
        logger.error(f"process_next_fatal: {exc}")
        counter("worker.process_error")
        span.end()
        return ProcessOnceResponse(processed=False, detail=f"error: {exc}")
