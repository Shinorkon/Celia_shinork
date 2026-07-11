from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from redis import Redis
import psycopg

from packages.telemetry import init_logging, set_correlation_id, get_correlation_id, counter

SERVICE_NAME = os.getenv("SERVICE_NAME", "telegram-ingress")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "Carliabot")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Bootstrap mode: if no active users exist in the DB, allow everyone.
# Once at least one user is authorized, only active users can use the bot.
_AUTH_MODE_BOOTSTRAP = True  # set False after first authorized user check
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
INGRESS_STREAM = os.getenv("INGRESS_STREAM", "ingress.accepted")
COMPLETION_STREAM = os.getenv("COMPLETION_STREAM", "worker.completed")
OUTBOX_GROUP = os.getenv("OUTBOX_GROUP", "ingress-outbox-group")
OUTBOX_CONSUMER = os.getenv("OUTBOX_CONSUMER", "ingress-outbox-1")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    service: str
    status: str
    timestamp: str


class WebhookRequest(BaseModel):
    update_id: int | None = None
    message: dict[str, Any] | None = None


class WebhookResult(BaseModel):
    accepted: bool
    reason: str
    trigger_type: str
    user_id: int | None = None
    thread_id: int | None = None


@dataclass
class IngressDecision:
    accepted: bool
    reason: str
    trigger_type: str


def _audit(action: str, metadata: dict[str, Any]) -> None:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs(actor_type, actor_id, action, target_type, target_id, metadata_jsonb)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        "service",
                        SERVICE_NAME,
                        action,
                        "telegram_message",
                        str(metadata.get("message_id", "")),
                        json.dumps(metadata),
                    ),
                )
            conn.commit()
    except Exception:
        pass


def _is_authorized(telegram_user_id: int) -> bool:
    """Check if a user is authorized. Bootstrap: if no active users, allow all."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Check if any active (authorized) users exist at all
                cur.execute("SELECT COUNT(*) FROM users WHERE is_active = TRUE AND role = 'authorized'")
                active_count = cur.fetchone()[0]
                if active_count == 0:
                    # Bootstrap mode — no authorized users yet, allow everyone
                    return True
                # Check this specific user
                cur.execute(
                    "SELECT is_active FROM users WHERE telegram_user_id = %s AND role = 'authorized'",
                    (telegram_user_id,),
                )
                row = cur.fetchone()
                return row is not None and row[0]
    except Exception:
        # DB down — fall back to allowing the message through
        logger.warning("auth_check_db_error: allowing message through")
        return True


def _ensure_user(telegram_user_id: int) -> None:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users(telegram_user_id, role, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (telegram_user_id) DO NOTHING
                    """,
                    (telegram_user_id, "visitor"),
                )
            conn.commit()
    except Exception:
        pass


def _is_actionable(text: str) -> bool:
    lowered = text.lower().strip()
    if not lowered:
        return False

    keywords = (
        "remind",
        "schedule",
        "deploy",
        "ssh",
        "server",
        "resume",
        "cv",
        "task",
        "check",
    )
    return any(k in lowered for k in keywords)


def _decide_trigger(text: str, chat_type: str = "private") -> IngressDecision:
    lowered = text.lower().strip()
    mention = f"@{BOT_USERNAME.lower()}"

    # Always accept direct messages
    if chat_type == "private":
        return IngressDecision(True, "direct_message", "dm")

    if lowered.startswith("/"):
        return IngressDecision(True, "explicit_command", "command")
    if mention in lowered:
        return IngressDecision(True, "explicit_mention", "mention")
    if _is_actionable(lowered):
        return IngressDecision(True, "actionable_message", "classifier")
    return IngressDecision(False, "noise_filtered", "none")


def _send_telegram_message(chat_id: str, text: str, thread_id: str = "") -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not BOT_TOKEN or not chat_id:
        logger.warning(f"telegram_send_skipped: missing_token_or_chat_id")
        return False
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4096],
        **({"message_thread_id": int(thread_id)} if thread_id and thread_id.strip() else {}),
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{TELEGRAM_API}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            logger.warning(f"telegram_send_non_200: status={resp.status}")
            return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        logger.error(f"telegram_send_http_error: status={exc.code} body={body}")
        return False
    except Exception as exc:
        logger.error(f"telegram_send_error: {exc}")
        return False


def _poll_completions() -> None:
    """Poll worker.completed stream and send replies via Telegram."""
    tick = 0
    while True:
        try:
            r = Redis.from_url(REDIS_URL, decode_responses=True)
            try:
                r.xgroup_create(COMPLETION_STREAM, OUTBOX_GROUP, id="0", mkstream=True)
            except Exception:
                pass

            # First claim any pending (unacked) messages from previous crashes
            try:
                pending = r.xreadgroup(
                    OUTBOX_GROUP, OUTBOX_CONSUMER,
                    streams={COMPLETION_STREAM: "0"},
                    count=10, block=100,
                )
                if pending:
                    _, msgs = pending[0]
                    for msg_id, fields in msgs:
                        try:
                            _send_completion(r, msg_id, fields)
                        except Exception as ex:
                            logger.error(f"pending_send_crash: {ex}")
                            try:
                                r.xack(COMPLETION_STREAM, OUTBOX_GROUP, msg_id)
                            except Exception:
                                pass
            except Exception:
                pass

            # Then block waiting for new messages
            entries = r.xreadgroup(
                OUTBOX_GROUP, OUTBOX_CONSUMER,
                streams={COMPLETION_STREAM: ">"},
                count=5, block=2000,
            )
            if not entries:
                tick += 1
                if tick % 30 == 0:
                    logger.info("outbox_heartbeat: alive")
                continue
            _, messages = entries[0]
            for message_id, fields in messages:
                try:
                    _send_completion(r, message_id, fields)
                except Exception as ex:
                    logger.error(f"msg_send_crash: {ex}")
                    try:
                        r.xack(COMPLETION_STREAM, OUTBOX_GROUP, message_id)
                    except Exception:
                        pass
        except Exception as exc:
            logger.error(f"completion_poll_error: {exc}")
            time.sleep(1)


def _send_completion(r: Redis, message_id: str, fields: dict) -> None:
    """Process a single completion event, send to Telegram, and ACK."""
    try:
        payload = json.loads(fields.get("payload", "{}"))
        chat_id = payload.get("chat_id", "")
        output = payload.get("output", "")
        thread_id = payload.get("thread_id", "")
        status = payload.get("status", "")
        run_id = payload.get("run_id", "")
        if not chat_id or not output:
            logger.info(f"outbox_skip_empty: run_id={run_id}")
            r.xack(COMPLETION_STREAM, OUTBOX_GROUP, message_id)
            return
        prefix = "❌" if status == "failed" else "✅" if status == "completed" else "ℹ️"
        full_text = f"{prefix} {output}"
        if _send_telegram_message(chat_id, full_text, thread_id):
            logger.info(f"outbox_sent: run_id={run_id} chat_id={chat_id} len={len(full_text)}")
            counter("ingress.outbox_sent")
        else:
            logger.warning(f"outbox_failed: run_id={run_id} chat_id={chat_id}")
            counter("ingress.outbox_failed")
    except Exception as exc:
        logger.error(f"completion_handler_error: {exc}")
    finally:
        try:
            r.xack(COMPLETION_STREAM, OUTBOX_GROUP, message_id)
        except Exception:
            pass


@app.on_event("startup")
def startup() -> None:
    t = threading.Thread(target=_poll_completions, daemon=True)
    t.start()
    logger.info("ingress_outbox_started")


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


@app.post("/webhook", response_model=WebhookResult)
def webhook(payload: WebhookRequest) -> WebhookResult:
    if payload.message is None:
        raise HTTPException(status_code=400, detail="Missing message field")

    from_user = payload.message.get("from", {})
    user_id = from_user.get("id")
    text = (payload.message.get("text") or "").strip()
    thread_id = payload.message.get("message_thread_id")

    if not isinstance(user_id, int):
        _audit("ingress_drop_missing_user", {"reason": "missing_user_id"})
        return WebhookResult(
            accepted=False,
            reason="missing_user_id",
            trigger_type="none",
            user_id=None,
            thread_id=thread_id,
        )

    if not _is_authorized(user_id):
        _audit(
            "ingress_drop_unauthorized",
            {
                "user_id": user_id,
                "chat_id": payload.message.get("chat", {}).get("id"),
            },
        )
        return WebhookResult(
            accepted=False,
            reason="unauthorized_user",
            trigger_type="none",
            user_id=user_id,
            thread_id=thread_id,
        )

    chat_type = payload.message.get("chat", {}).get("type", "private")
    decision = _decide_trigger(text, chat_type)
    _ensure_user(user_id)
    correlation_id = str(uuid.uuid4())
    set_correlation_id(correlation_id)

    if decision.accepted:
        try:
            redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
            event = {
                "event_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_id": str(user_id),
                "chat_id": str(payload.message.get("chat", {}).get("id", "")),
                "thread_id": str(thread_id if thread_id is not None else ""),
                "trigger_type": decision.trigger_type,
                "reason": decision.reason,
                "text": text,
                "correlation_id": correlation_id,
            }
            redis_client.xadd(INGRESS_STREAM, {"payload": json.dumps(event)})
            _audit("ingress_accepted", event)
            counter("ingress.messages_accepted")
        except Exception as exc:
            logger.warning(f"redis_publish_failed: {exc}")
    else:
        _audit(
            "ingress_noise_filtered",
            {
                "user_id": user_id,
                "chat_id": payload.message.get("chat", {}).get("id"),
                "thread_id": thread_id,
                "text": text,
                "correlation_id": correlation_id,
            },
        )
        counter("ingress.messages_filtered")

    return WebhookResult(
        accepted=decision.accepted,
        reason=decision.reason,
        trigger_type=decision.trigger_type,
        user_id=user_id,
        thread_id=thread_id,
    )
