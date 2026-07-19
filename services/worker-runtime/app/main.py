"""
Worker Runtime — consumes orchestration.dispatched events, executes tasks
via LLM (reasoning roles) or SSH (executor role) behind policy gateway.

Hardened with correlation-id propagation, idempotency, retry/backoff,
circuit breaker, and dead-letter queue.
"""

from __future__ import annotations

import asyncio
import json
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
    DEFAULT_MODEL, build_system_prompt,
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
    ]
    text: str = Field(default="")
    command: str | None = None
    chat_id: str = ""
    thread_id: str = ""
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

    # Fetch conversation history for this chat
    history: list[LLMMessage] = _load_history(chat_id)

    bypass_confirm = bool(payload.get("bypass_confirm", False))

    try:
        if agent_role == "executor":
            result = _run_executor_command(
                run_id, text, chat_id=chat_id, thread_id=thread_id, bypass_confirm=bypass_confirm,
            )
        elif agent_role == "coder":
            result = _run_coder_agent(run_id, text, history=history, chat_id=chat_id, thread_id=thread_id)
        else:
            result = _run_llm_agent(run_id, agent_role, text, history=history, chat_id=chat_id, thread_id=thread_id)
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
            args=(run_id, text, result.output),
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


def _load_memory_context(limit: int = 10) -> str | None:
    """Fetch the most recently-active memory items (goals, decisions,
    project state, preferences) as a system-message string to ground the
    model's answers and opinions across sessions - distinct from
    `_load_history`, which is per-chat transcript.

    v1 retrieval is recency-only, not semantic search: agent_platform's
    Postgres has no pgvector extension available (it lives in a plain
    postgres:16 instance shared with an unrelated project), and at the
    volume a personal assistant's memory realistically accumulates, the
    most-recently-touched items are a reasonable proxy for "still relevant"
    without the complexity of a keyword-matching heuristic that could
    silently miss items phrased differently than the current message.
    """
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT kind, title, body, project_ref
                    FROM memory_items
                    WHERE status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        if not rows:
            return None
        lines = []
        for kind, title, body, project_ref in rows:
            scope = f" (project: {project_ref})" if project_ref else ""
            lines.append(f"- [{kind}] {title}: {body}{scope}")
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


def _handle_notify_user_call(tool_call: dict, chat_id: str, thread_id: str) -> str | None:
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
) -> TaskResponse:
    """Shared multi-turn loop: call the LLM (with tool access if the role has
    any registered in TOOL_SCHEMAS), execute any requested tool calls through
    the policy-gated executor, feed results back as a `tool` message, and
    repeat until the model stops requesting tools or max_turns is hit."""
    client = LiteLLMClient()
    model = ROLE_MODEL_MAP.get(role, DEFAULT_MODEL)
    tools = TOOL_SCHEMAS.get(role)

    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=build_system_prompt(role)),
    ]
    memory_context = _load_memory_context()
    if memory_context:
        messages.append(LLMMessage(role="system", content=memory_context))
    if history:
        messages.extend(history)
    messages.append(LLMMessage(role="user", content=text))

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
                notify_result = _handle_notify_user_call(call, chat_id, thread_id)
                if notify_result is not None:
                    result_text = notify_result
                else:
                    command = _extract_shell_command(call)
                    if command is None:
                        result_text = "(unsupported tool call)"
                    else:
                        exec_result = _run_executor_command(
                            f"{run_id}-{role}-{turn}", command, chat_id=chat_id, thread_id=thread_id,
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
    chat_id: str = "", thread_id: str = "",
) -> TaskResponse:
    return _run_tool_calling_agent(run_id, "coder", text, history, max_turns=5, chat_id=chat_id, thread_id=thread_id)


# Most conversational roles only ever need a turn or two of tool use, if
# any. ops-reflect is the exception: its whole job is checking several
# independent signals (services, disk, docker, git across projects) before
# deciding whether anything's worth surfacing, so 3 turns isn't enough
# headroom to reach a conclusion rather than just running out mid-check —
# observed live (it hit the cap after service-status + disk checks, still
# wanting to look at docker and git). Give it the same budget as coder.
_MAX_TURNS_BY_ROLE: dict[str, int] = {
    "ops-reflect": 5,
}
_DEFAULT_LLM_AGENT_MAX_TURNS = 3


def _run_llm_agent(
    run_id: str, agent_role: str, text: str,
    history: list[LLMMessage] | None = None,
    chat_id: str = "", thread_id: str = "",
) -> TaskResponse:
    max_turns = _MAX_TURNS_BY_ROLE.get(agent_role, _DEFAULT_LLM_AGENT_MAX_TURNS)
    return _run_tool_calling_agent(run_id, agent_role, text, history, max_turns=max_turns, chat_id=chat_id, thread_id=thread_id)


# ---------------------------------------------------------------------------
# Memory writer — best-effort, fire-and-forget judgment call on whether a
# just-completed exchange contains anything worth remembering long-term.
# Runs in a background thread (see _handle_task); failures here never affect
# the user-facing reply, which has already been sent by the time this runs.
# ---------------------------------------------------------------------------


def _run_memory_writer(run_id: str, user_text: str, assistant_output: str) -> None:
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

        valid_items = [
            item for item in items
            if item.get("kind") and item.get("title") and item.get("body")
        ]
        if not valid_items:
            return

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for item in valid_items:
                    cur.execute(
                        """
                        INSERT INTO memory_items(kind, title, body, tags, project_ref, source_run_ref)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            item["kind"], item["title"], item["body"],
                            item.get("tags") or [], item.get("project_ref"), run_id,
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
        return _run_coder_agent(payload.run_id, payload.text, chat_id=payload.chat_id, thread_id=payload.thread_id)

    # Non-executor path: LLM-powered reasoning
    return _run_llm_agent(
        payload.run_id, payload.agent_role, payload.text,
        chat_id=payload.chat_id, thread_id=payload.thread_id,
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
