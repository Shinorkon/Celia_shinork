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
from llm_client import LiteLLMClient, LLMMessage, LLMResponse, ROLE_MODEL_MAP, agent_chat, build_system_prompt

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
    ]
    text: str = Field(default="")
    command: str | None = None


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

    try:
        if agent_role == "executor":
            result = _run_executor_command(run_id, text)
        elif agent_role == "coder":
            result = _run_coder_agent(run_id, text, history=history)
        else:
            result = _run_llm_agent(run_id, agent_role, text, history=history)
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


def _run_executor_command(run_id: str, command: str) -> TaskResponse:
    """Evaluate the command against the policy gateway, then execute via SSH."""
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
# Coder agent: multi-turn LLM ↔ EXEC loop
# ---------------------------------------------------------------------------


def _run_coder_agent(
    run_id: str, text: str, history: list[LLMMessage] | None = None,
) -> TaskResponse:
    """Multi-turn coding agent. LLM outputs EXEC commands, worker runs them
    via SSH, results feed back into the LLM. Repeats up to 5 turns or until
    the LLM stops issuing EXEC commands."""
    import re

    max_turns = 5
    client = LiteLLMClient()
    model = ROLE_MODEL_MAP.get("coder", "gpt-4o")

    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=build_system_prompt("coder")),
    ]
    if history:
        messages.extend(history)
    messages.append(LLMMessage(role="user", content=text))

    exec_pattern = re.compile(r"^EXEC:\s*(.+)$", re.MULTILINE)
    last_response: LLMResponse | None = None

    try:
        for turn in range(max_turns):
            response = client.chat(messages, model=model)
            messages.append(LLMMessage(role="assistant", content=response.content))
            last_response = response

            matches = list(exec_pattern.finditer(response.content))
            if not matches:
                # No more EXEC commands — agent is done
                _log_usage(run_id, "coder", response)
                return TaskResponse(
                    run_id=run_id, agent_role="coder",
                    status="completed", output=response.content,
                )

            # Execute each EXEC command and collect results
            result_blocks: list[str] = []
            for match in matches:
                command = match.group(1).strip()
                exec_result = _run_executor_command(
                    f"{run_id}-coder-{turn}", command,
                )

                if exec_result.status == "completed":
                    # Extract STDOUT section
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
                    if not clean:
                        clean = exec_result.output[:500]
                elif exec_result.status == "blocked":
                    clean = f"BLOCKED by policy: {exec_result.policy_reason}"
                else:
                    clean = f"FAILED: {exec_result.output[:300]}"

                result_blocks.append(f"$ {command}\n{clean}")

            feedback = "Command results:\n\n" + "\n\n".join(result_blocks)
            messages.append(LLMMessage(role="user", content=feedback))

        # Max turns exhausted
        return TaskResponse(
            run_id=run_id,
            agent_role="coder",
            status="completed",
            output=(
                (last_response.content if last_response else "(no output)")
                + "\n\n_(max turns reached — task may be incomplete)_"
            ),
        )
    except Exception as exc:
        return TaskResponse(
            run_id=run_id,
            agent_role="coder",
            status="failed",
            output=f"Coder agent error: {exc}",
        )
    finally:
        client.close()


def _run_llm_agent(
    run_id: str, agent_role: str, text: str,
    history: list[LLMMessage] | None = None,
) -> TaskResponse:
    """Call LiteLLM for reasoning roles. Parse and execute any EXEC: commands."""
    try:
        response = agent_chat(agent_role, text, history=history)
        _log_usage(run_id, agent_role, response)
        output = response.content

        # Parse and execute any EXEC: commands embedded in the LLM response
        import re
        exec_pattern = re.compile(r'^EXEC:\s*(.+)$', re.MULTILINE)
        for match in exec_pattern.finditer(output):
            command = match.group(1).strip()
            exec_result = _run_executor_command(f"{run_id}-exec", command)
            if exec_result.status == "completed":
                # Extract just stdout from the SSH result
                result_lines = exec_result.output.split("\n")
                stdout_started = False
                clean_lines = []
                for line in result_lines:
                    if line.startswith("STDOUT:"):
                        stdout_started = True
                        continue
                    if line.startswith("STDERR:"):
                        break
                    if stdout_started:
                        clean_lines.append(line)
                result_text = "\n".join(clean_lines).strip() or exec_result.output[:200]
            elif exec_result.status == "blocked":
                result_text = f"(blocked: {exec_result.policy_reason})"
            else:
                result_text = f"(failed)"
            output = output.replace(match.group(0), f"$ {command}\n{result_text}")

        return TaskResponse(
            run_id=run_id,
            agent_role=agent_role,
            status="completed",
            output=output,
        )
    except Exception as exc:
        return TaskResponse(
            run_id=run_id,
            agent_role=agent_role,
            status="failed",
            output=f"LLM call failed: {exc}",
        )


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
        return _run_executor_command(payload.run_id, command)

    # Non-executor path: LLM-powered reasoning
    return _run_llm_agent(payload.run_id, payload.agent_role, payload.text)


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
