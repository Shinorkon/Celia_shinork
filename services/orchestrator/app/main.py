"""
Orchestrator — consumes ingress.accepted events, routes to workers,
persists checkpoints, and emits orchestration.dispatched events.

Hardened with correlation-id propagation, idempotency, retry/backoff,
and dead-letter queue.
"""

from __future__ import annotations

import threading
import time
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field
from redis import Redis
import psycopg

from packages.telemetry import (
    init_logging,
    set_correlation_id,
    get_correlation_id,
    set_run_id,
    counter,
    start_span,
)
from packages.utils import DeadLetter, IdempotencyStore, retry_with_backoff

SERVICE_NAME = os.getenv("SERVICE_NAME", "orchestrator")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
INGRESS_STREAM = os.getenv("INGRESS_STREAM", "ingress.accepted")
DISPATCH_STREAM = os.getenv("DISPATCH_STREAM", "orchestration.dispatched")
DEAD_LETTER_STREAM = os.getenv("DEAD_LETTER_STREAM", "dead.letter")
GROUP_NAME = os.getenv("ORCHESTRATOR_GROUP", "orchestrator-group")
CONSUMER_NAME = os.getenv("ORCHESTRATOR_CONSUMER", "orchestrator-1")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    service: str
    status: str
    timestamp: str


class RouteRequest(BaseModel):
    text: str = Field(min_length=1)


class RouteResponse(BaseModel):
    agent_role: Literal[
        "frontoffice",
        "scheduler",
        "document",
        "executor",
    ]
    reason: str


class ProcessOnceResponse(BaseModel):
    processed: bool
    detail: str
    run_id: str | None = None
    agent_role: str | None = None


def _route_text(text: str) -> tuple[str, str]:
    lowered = text.lower().strip()
    if any(k in lowered for k in ("remind", "schedule", "tomorrow", "next week")):
        return "scheduler", "time_intent"
    if any(k in lowered for k in ("cv", "resume", "cover letter", "bio")):
        return "document", "document_intent"
    # Only route to executor when the text looks like an actual shell command,
    # not a natural-language question that merely mentions servers/SSH/docker.
    if _looks_like_command(lowered):
        return "executor", "ops_intent"
    return "frontoffice", "default_triage"


def _looks_like_command(text: str) -> bool:
    """Returns True if text appears to be a shell command rather than natural language."""
    # If it starts with a known binary → command
    known_binaries = {
        "ls", "cat", "grep", "rg", "systemctl", "docker", "pwd", "echo",
        "whoami", "df", "du", "free", "uptime", "uname", "hostname", "date",
        "ps", "top", "htop", "journalctl", "tail", "head", "wc", "find",
        "stat", "id", "groups", "ss", "netstat", "ip", "ping", "curl",
        "wget", "git", "awk", "sed", "sort", "uniq", "cut", "tr", "tee",
        "crontab", "which", "type", "env", "printenv", "pg_isready",
        "redis-cli", "systemd-analyze", "timedatectl", "loginctl", "hostnamectl",
    }
    first_word = text.split()[0] if text else ""
    if first_word in known_binaries:
        return True
    # If it has pipe (|), redirect (>), or semicolons → command
    if any(c in text for c in ("|", ">", "<", ";")):
        return True
    # Everything else → natural language, let frontoffice handle it
    return False
    return False


def _redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _dead_letter() -> DeadLetter:
    return DeadLetter(_redis(), DEAD_LETTER_STREAM)


def _idempotency() -> IdempotencyStore:
    return IdempotencyStore(_redis(), prefix="orch:idem", ttl_seconds=7200)


@retry_with_backoff(max_retries=2, base_ms=100)
def _persist_dispatch(run_ref: str, role: str, reason: str, payload: dict[str, str]) -> None:
    chat_id_raw = payload.get("chat_id") or "0"
    thread_id_raw = payload.get("thread_id") or ""
    user_id_raw = payload.get("user_id") or "0"
    text = payload.get("text", "")
    chat_id = int(chat_id_raw) if chat_id_raw.lstrip("-").isdigit() else 0
    telegram_thread_id = (
        int(thread_id_raw) if thread_id_raw and thread_id_raw.lstrip("-").isdigit() else None
    )
    telegram_user_id = (
        int(user_id_raw) if user_id_raw and user_id_raw.lstrip("-").isdigit() else None
    )

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            db_user_id = None
            if telegram_user_id is not None:
                cur.execute(
                    """INSERT INTO users(telegram_user_id, role, is_active)
                    VALUES (%s, %s, TRUE) ON CONFLICT (telegram_user_id) DO UPDATE SET is_active = TRUE
                    RETURNING id""",
                    (telegram_user_id, "user"),
                )
                row = cur.fetchone()
                db_user_id = row[0] if row else None

            cur.execute(
                """SELECT id FROM threads
                WHERE telegram_chat_id = %s
                  AND telegram_thread_id IS NOT DISTINCT FROM %s
                ORDER BY id DESC LIMIT 1""",
                (chat_id, telegram_thread_id),
            )
            row = cur.fetchone()
            if row:
                thread_db_id = row[0]
                cur.execute(
                    "UPDATE threads SET updated_at = NOW(), status = 'active' WHERE id = %s",
                    (thread_db_id,),
                )
            else:
                cur.execute(
                    """INSERT INTO threads(telegram_chat_id, telegram_thread_id, user_id, status)
                    VALUES (%s, %s, %s, 'active') RETURNING id""",
                    (chat_id, telegram_thread_id, db_user_id),
                )
                thread_db_id = cur.fetchone()[0]

            cur.execute(
                """INSERT INTO messages(thread_id, direction, payload_jsonb)
                VALUES (%s, %s, %s::jsonb)""",
                (thread_db_id, "inbound", json.dumps({
                    "text": text, "user_id": telegram_user_id,
                    "chat_id": chat_id, "thread_id": telegram_thread_id,
                    "correlation_id": get_correlation_id(),
                })),
            )

            cur.execute(
                """INSERT INTO orchestration_runs(thread_id, run_ref, current_agent, status)
                VALUES (%s, %s, %s, %s) RETURNING id""",
                (thread_db_id, run_ref, role, "dispatched"),
            )
            run_db_id = cur.fetchone()[0]

            cur.execute(
                """INSERT INTO checkpoints(run_id, step_index, state_jsonb)
                VALUES (%s, %s, %s::jsonb)""",
                (run_db_id, 0, json.dumps({
                    "run_ref": run_ref, "reason": reason, "role": role,
                    "payload": payload, "correlation_id": get_correlation_id(),
                })),
            )

            cur.execute(
                """INSERT INTO audit_logs(actor_type, actor_id, action, target_type, target_id, metadata_jsonb)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                ("service", SERVICE_NAME, "orchestration_dispatched", "run", run_ref,
                 json.dumps({"role": role, "reason": reason, "correlation_id": get_correlation_id()})),
            )
        conn.commit()
    counter("orchestrator.runs_dispatched")


@app.on_event("startup")
def startup() -> None:
    r = _redis()
    try:
        r.xgroup_create(INGRESS_STREAM, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass
    t = threading.Thread(target=_poll_ingress_stream, daemon=True)
    t.start()
    logger.info("orchestrator_started")


def _poll_ingress_stream() -> None:
    """Continuously poll the ingress stream for new messages (runs in daemon thread)."""
    while True:
        try:
            r = _redis()
            entries = r.xreadgroup(GROUP_NAME, CONSUMER_NAME, streams={INGRESS_STREAM: ">"}, count=1, block=2000)
            if not entries:
                continue
            _, messages = entries[0]
            message_id, fields = messages[0]
            _handle_message(message_id, fields)
        except Exception as exc:
            logger.error(f"poll_error: {exc}")
            time.sleep(1)


def _handle_message(message_id: str, fields: dict) -> None:
    """Process a single message from the ingress stream."""
    r = _redis()
    span = start_span("orchestrator.process_message")
    payload_raw = fields.get("payload", "{}")
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
        span.end()
        return

    cid = payload.get("correlation_id", str(uuid.uuid4()))
    set_correlation_id(cid)

    idem_key = payload.get("event_id", message_id)
    if _idempotency().is_duplicate(idem_key):
        counter("orchestrator.duplicate_event")
        r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
        span.end()
        return

    text = payload.get("text", "")
    role, reason = _route_text(text)
    run_id = str(uuid.uuid4())
    set_run_id(run_id)

    try:
        _persist_dispatch(run_id, role, reason, payload)
    except Exception as exc:
        logger.error(f"persist_failed: {exc}")
        _dead_letter().publish(
            original_payload=payload, error=str(exc),
            source="orchestrator", correlation_id=cid,
        )
        r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
        counter("orchestrator.dead_letter")
        span.end()
        return

    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id, "agent_role": role, "reason": reason,
        "text": text, "user_id": payload.get("user_id", ""),
        "chat_id": payload.get("chat_id", ""),
        "thread_id": payload.get("thread_id", ""),
        "correlation_id": cid,
    }
    r.xadd(DISPATCH_STREAM, {"payload": json.dumps(event)})
    r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
    counter("orchestrator.dispatched")
    span.end()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, status="ok", timestamp=datetime.now(timezone.utc).isoformat())


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE_NAME, "message": "ready"}


@app.post("/route", response_model=RouteResponse)
def route_task(payload: RouteRequest) -> RouteResponse:
    role, reason = _route_text(payload.text)
    return RouteResponse(agent_role=role, reason=reason)


@app.post("/process-next", response_model=ProcessOnceResponse)
def process_next() -> ProcessOnceResponse:
    span = start_span("orchestrator.process_next")
    try:
        r = _redis()
        entries = r.xreadgroup(GROUP_NAME, CONSUMER_NAME, streams={INGRESS_STREAM: ">"}, count=1, block=50)
        if not entries:
            return ProcessOnceResponse(processed=False, detail="no_messages")

        _, messages = entries[0]
        message_id, fields = messages[0]
        payload_raw = fields.get("payload", "{}")
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
            return ProcessOnceResponse(processed=False, detail="invalid_payload")

        cid = payload.get("correlation_id", str(uuid.uuid4()))
        set_correlation_id(cid)

        idem_key = payload.get("event_id", message_id)
        if _idempotency().is_duplicate(idem_key):
            counter("orchestrator.duplicate_event")
            r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
            span.end()
            return ProcessOnceResponse(processed=False, detail="duplicate_event")

        text = payload.get("text", "")
        role, reason = _route_text(text)
        run_id = str(uuid.uuid4())
        set_run_id(run_id)

        try:
            _persist_dispatch(run_id, role, reason, payload)
        except Exception as exc:
            logger.error(f"persist_failed: {exc}")
            _dead_letter().publish(
                original_payload=payload, error=str(exc),
                source="orchestrator", correlation_id=cid,
            )
            r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
            counter("orchestrator.dead_letter")
            span.end()
            return ProcessOnceResponse(processed=False, detail="persist_failed_dlq", run_id=run_id)

        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id, "agent_role": role, "reason": reason,
            "text": text, "user_id": payload.get("user_id", ""),
            "chat_id": payload.get("chat_id", ""),
            "thread_id": payload.get("thread_id", ""),
            "correlation_id": cid,
        }
        r.xadd(DISPATCH_STREAM, {"payload": json.dumps(event)})
        r.xack(INGRESS_STREAM, GROUP_NAME, message_id)
        counter("orchestrator.dispatched")
        span.end()
        return ProcessOnceResponse(processed=True, detail="dispatched", run_id=run_id, agent_role=role)
    except Exception as exc:
        logger.error(f"process_next_fatal: {exc}")
        counter("orchestrator.process_error")
        span.end()
        return ProcessOnceResponse(processed=False, detail=f"error: {exc}")
