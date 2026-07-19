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
from packages.config import _parse_int_set

SERVICE_NAME = os.getenv("SERVICE_NAME", "telegram-ingress")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "Carliabot")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Owner/admin allowlist, seeded once from the environment. Anyone in this set
# is always authorized (even if the DB is unreachable or the users table is
# empty) — this replaces the old "empty users table = allow everyone" fail-open
# bootstrap, and is the fallback that keeps the owner from being locked out of
# their own bot by a DB outage. Everyone else needs an explicit `role =
# 'authorized'` row, granted via the admin console.
OWNER_TELEGRAM_USER_IDS: set[int] = _parse_int_set(os.getenv("ALLOWED_TELEGRAM_USER_IDS", ""))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
INGRESS_STREAM = os.getenv("INGRESS_STREAM", "ingress.accepted")
DISPATCH_STREAM = os.getenv("DISPATCH_STREAM", "orchestration.dispatched")
COMPLETION_STREAM = os.getenv("COMPLETION_STREAM", "worker.completed")
NOTIFICATION_STREAM = os.getenv("NOTIFICATION_STREAM", "notification.requested")
OUTBOX_GROUP = os.getenv("OUTBOX_GROUP", "ingress-outbox-group")
OUTBOX_CONSUMER = os.getenv("OUTBOX_CONSUMER", "ingress-outbox-1")
NOTIFICATION_GROUP = os.getenv("NOTIFICATION_GROUP", "ingress-notification-group")
NOTIFICATION_CONSUMER = os.getenv("NOTIFICATION_CONSUMER", "ingress-notification-1")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

app = FastAPI(title=SERVICE_NAME)

# ---------------------------------------------------------------------------
# Response sanitizer — strips leaked shell artifacts before Telegram delivery
# ---------------------------------------------------------------------------
import re

_SHELL_ARTIFACT_PATTERNS: list[re.Pattern] = [
    # Lines starting with $ (shell commands)
    re.compile(r'^\$\s+.+$', re.MULTILINE),
    # Docker ps table headers and rows
    re.compile(r'^[A-Z]{3,}\s{2,}.*$', re.MULTILINE),
    # Filesystem / disk usage output (df -h)
    re.compile(r'^Filesystem\s+Size\s+Used.*$', re.MULTILINE),
    # /dev/ lines from df output
    re.compile(r'^/dev/[a-z0-9]+\s+.*$', re.MULTILINE),
    # free -h header lines
    re.compile(r'^\s+(total|used|free|shared|buff/cache|available)\s.*$', re.MULTILINE),
    # Mem:/Swap: lines
    re.compile(r'^(Mem|Swap):\s+.*$', re.MULTILINE),
    # uptime output lines
    re.compile(r'^\d{2}:\d{2}:\d{2}\s+up\s+.*$', re.MULTILINE),
    # systemctl/is-active output verbatim
    re.compile(r'^(active|inactive|failed|activating|deactivating)$', re.MULTILINE),
    # EXEC: remnants that escaped worker processing
    re.compile(r'^EXEC:\s*.+$', re.MULTILINE),
    # Policy check error spam
    re.compile(r'^Policy check failed:.*$', re.MULTILINE),
    # Git log output lines (commit hashes)
    re.compile(r'^[a-f0-9]{7,40}\s+.+$', re.MULTILINE),
    # JSON/API response dumps (curly brace on its own line after output)
    re.compile(r'^\{"service".+\}$', re.MULTILINE),
]


def _sanitize_telegram_output(text: str) -> str:
    """Remove leaked shell artifacts from LLM responses.

    The LLM sometimes echoes raw command output in chat. This strips
    known shell-output patterns so the user only sees conversational text.
    """
    for pattern in _SHELL_ARTIFACT_PATTERNS:
        text = pattern.sub('', text)
    # Collapse multiple blank lines into one
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip leading/trailing whitespace per line and leading blank lines
    text = text.strip()
    return text


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
    """Check if a user is authorized.

    Fails closed on every path: the owner allowlist (from
    ALLOWED_TELEGRAM_USER_IDS) always passes without touching the DB; anyone
    else needs an explicit active `role = 'authorized'` row, and a DB error
    denies non-owners rather than admitting them.
    """
    if telegram_user_id in OWNER_TELEGRAM_USER_IDS:
        return True
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_active FROM users WHERE telegram_user_id = %s AND role = 'authorized'",
                    (telegram_user_id,),
                )
                row = cur.fetchone()
                return row is not None and row[0]
    except Exception as exc:
        logger.error(f"auth_check_db_error: denying non-owner user_id={telegram_user_id} error={exc}")
        counter("ingress.auth_db_error_denied")
        return False


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
        # Sanitize leaked shell artifacts before sending to user
        clean_output = _sanitize_telegram_output(output)
        if not clean_output or len(clean_output) < 3:
            logger.info(f"outbox_skip_sanitized_empty: run_id={run_id}")
            r.xack(COMPLETION_STREAM, OUTBOX_GROUP, message_id)
            return
        prefix = "❌" if status == "failed" else "✅" if status == "completed" else "ℹ️"
        full_text = f"{prefix} {clean_output}"
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


def _poll_notifications() -> None:
    """Poll notification.requested stream and send messages via Telegram.

    Carries confirm-first approval requests and notify-after summaries from
    worker-runtime (see _publish_notification in worker-runtime/app/main.py).
    Structurally identical to _poll_completions - a separate consumer group
    on a separate stream, not a variant of the same loop, since the two
    streams have unrelated payload shapes and failure semantics.
    """
    tick = 0
    while True:
        try:
            r = Redis.from_url(REDIS_URL, decode_responses=True)
            try:
                r.xgroup_create(NOTIFICATION_STREAM, NOTIFICATION_GROUP, id="0", mkstream=True)
            except Exception:
                pass

            try:
                pending = r.xreadgroup(
                    NOTIFICATION_GROUP, NOTIFICATION_CONSUMER,
                    streams={NOTIFICATION_STREAM: "0"},
                    count=10, block=100,
                )
                if pending:
                    _, msgs = pending[0]
                    for msg_id, fields in msgs:
                        try:
                            _send_notification(r, msg_id, fields)
                        except Exception as ex:
                            logger.error(f"pending_notification_crash: {ex}")
                            try:
                                r.xack(NOTIFICATION_STREAM, NOTIFICATION_GROUP, msg_id)
                            except Exception:
                                pass
            except Exception:
                pass

            entries = r.xreadgroup(
                NOTIFICATION_GROUP, NOTIFICATION_CONSUMER,
                streams={NOTIFICATION_STREAM: ">"},
                count=5, block=2000,
            )
            if not entries:
                tick += 1
                if tick % 30 == 0:
                    logger.info("notification_heartbeat: alive")
                continue
            _, messages = entries[0]
            for message_id, fields in messages:
                try:
                    _send_notification(r, message_id, fields)
                except Exception as ex:
                    logger.error(f"notification_send_crash: {ex}")
                    try:
                        r.xack(NOTIFICATION_STREAM, NOTIFICATION_GROUP, message_id)
                    except Exception:
                        pass
        except Exception as exc:
            logger.error(f"notification_poll_error: {exc}")
            time.sleep(1)


def _send_notification(r: Redis, message_id: str, fields: dict) -> None:
    """Process a single notification event, send to Telegram, and ACK."""
    try:
        payload = json.loads(fields.get("payload", "{}"))
        chat_id = payload.get("chat_id", "")
        text = payload.get("text", "")
        thread_id = payload.get("thread_id", "")
        if not chat_id or not text:
            r.xack(NOTIFICATION_STREAM, NOTIFICATION_GROUP, message_id)
            return
        if _send_telegram_message(chat_id, text, thread_id):
            counter("ingress.notification_sent")
        else:
            counter("ingress.notification_failed")
    except Exception as exc:
        logger.error(f"notification_handler_error: {exc}")
    finally:
        try:
            r.xack(NOTIFICATION_STREAM, NOTIFICATION_GROUP, message_id)
        except Exception:
            pass


_APPROVAL_YES = {"yes", "y", "confirm", "approve", "go ahead", "do it"}
_APPROVAL_NO = {"no", "n", "deny", "cancel", "don't", "stop"}


def _get_pending_approval(chat_id: str) -> dict | None:
    """Return the most recent unexpired pending approval for this chat, if
    any. A late reply after expiry is treated as if no approval is pending -
    v1 has no separate sweep to mark rows 'expired'; the WHERE clause here
    is what actually enforces the timeout."""
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, run_ref, command, thread_id
                    FROM pending_approvals
                    WHERE chat_id = %s AND status = 'pending' AND expires_at > NOW()
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (chat_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "run_ref": row[1], "command": row[2], "thread_id": row[3]}
    except Exception as exc:
        logger.warning(f"get_pending_approval_error: {exc}")
        return None


def _resolve_pending_approval(approval_id: int, status: str) -> None:
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pending_approvals SET status = %s, resolved_at = NOW() WHERE id = %s",
                    (status, approval_id),
                )
            conn.commit()
    except Exception as exc:
        logger.warning(f"resolve_pending_approval_error: {exc}")


def _replay_approved_command(run_ref: str, command: str, chat_id: str, thread_id: str) -> None:
    """Re-dispatch an approved command straight to worker-runtime, bypassing
    the confirm_first pause this time - it's already been confirmed."""
    try:
        r = Redis.from_url(REDIS_URL, decode_responses=True)
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_ref,
            "agent_role": "executor",
            "text": command,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "bypass_confirm": True,
            "correlation_id": run_ref,
        }
        r.xadd(DISPATCH_STREAM, {"payload": json.dumps(event)})
    except Exception as exc:
        logger.error(f"replay_approved_command_error: {exc}")


@app.on_event("startup")
def startup() -> None:
    t = threading.Thread(target=_poll_completions, daemon=True)
    t.start()
    t2 = threading.Thread(target=_poll_notifications, daemon=True)
    t2.start()
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

    chat_id_str = str(payload.message.get("chat", {}).get("id", ""))

    # A confirm_first command may be sitting pending for this chat. If the
    # reply is clearly yes/no, resolve it here and short-circuit before
    # normal routing; anything else falls through to _decide_trigger below,
    # so the user isn't forced into a yes/no-only conversation while a
    # confirmation is outstanding.
    approval = _get_pending_approval(chat_id_str) if chat_id_str else None
    if approval is not None:
        lowered_text = text.strip().lower()
        if lowered_text in _APPROVAL_YES:
            _resolve_pending_approval(approval["id"], "approved")
            _replay_approved_command(
                approval["run_ref"], approval["command"], chat_id_str, approval["thread_id"] or "",
            )
            _audit("approval_confirmed", {"user_id": user_id, "chat_id": chat_id_str, "run_ref": approval["run_ref"]})
            return WebhookResult(accepted=True, reason="approval_confirmed", trigger_type="approval", user_id=user_id, thread_id=thread_id)
        if lowered_text in _APPROVAL_NO:
            _resolve_pending_approval(approval["id"], "denied")
            _send_telegram_message(chat_id_str, "Cancelled.", str(thread_id) if thread_id is not None else "")
            _audit("approval_denied", {"user_id": user_id, "chat_id": chat_id_str, "run_ref": approval["run_ref"]})
            return WebhookResult(accepted=True, reason="approval_denied", trigger_type="approval", user_id=user_id, thread_id=thread_id)
        # Not a yes/no reply - let it fall through to normal routing below.

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
