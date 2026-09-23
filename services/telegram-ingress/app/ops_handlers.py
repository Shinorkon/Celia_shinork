"""Ops confirm gate (Phase C) — no brochure, no auto-fire.

When intent_router says ops, ask a short "want me to check X?" and park a
pending. On yes, dispatch a *gated* read command to the executor (policy
gateway still applies). Writes/deploys stay confirm and map to cautious
commands only when the user already confirmed the NL ask.

Chat stays on the LLM path; lists/finance are handled earlier and untouched.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from app.intent_router import classify_intent
from app.side_effect_policy import classify_action, is_refuse

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, str], bool]

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DISPATCH_STREAM = os.getenv("DISPATCH_STREAM", "orchestration.dispatched")
_PENDING_TTL_SEC = int(os.getenv("OPS_PENDING_TTL_SEC", "300"))
_PENDING_KEY = "celia:ops:pending:{chat_id}"

_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "do it", "go ahead"}
_NO = {"no", "n", "nope", "cancel", "don't", "stop", "nah"}

_MEM_PENDING: dict[str, dict] = {}

# NL topic → safe read-only command (policy gateway still evaluates).
_TOPIC_COMMANDS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"(?i)\bdocker\b|\bcontainer"), "docker", "docker ps --format 'table {{.Names}}\t{{.Status}}'"),
    (re.compile(r"(?i)\bdisk\b|\bdf\b|\bspace\b"), "disk space", "df -h"),
    (re.compile(r"(?i)\buptime\b|\bload\b"), "uptime", "uptime"),
    (re.compile(r"(?i)\bmem(?:ory)?\b|\bfree\b"), "memory", "free -h"),
    (re.compile(r"(?i)\bnginx\b"), "nginx", "systemctl is-active nginx || true"),
    (re.compile(r"(?i)\bsystemd\b|\bservice"), "services", "systemctl list-units --type=service --state=running --no-pager | head -30"),
    (re.compile(r"(?i)\bcron\b"), "cron", "crontab -l 2>/dev/null || echo '(no crontab)'"),
    (re.compile(r"(?i)\bssh\b|\bvps\b|\bserver\b"), "server status", "uptime && df -h / | tail -1"),
]


def _redis():
    try:
        from redis import Redis
        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("ops_pending_redis_unavailable: %s", exc)
        return None


def _key(chat_id: str) -> str:
    return _PENDING_KEY.format(chat_id=chat_id)


def clear_memory_for_tests() -> None:
    _MEM_PENDING.clear()


def get_pending(chat_id: str) -> Optional[dict]:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_key(chat_id))
            if raw:
                data = json.loads(raw)
                if float(data.get("expires_at", 0)) > time.time():
                    return data
                r.delete(_key(chat_id))
                return None
        except Exception as exc:
            logger.warning("ops_pending_get_error: %s", exc)
    data = _MEM_PENDING.get(chat_id)
    if not data:
        return None
    if float(data.get("expires_at", 0)) <= time.time():
        _MEM_PENDING.pop(chat_id, None)
        return None
    return data


def set_pending(chat_id: str, payload: dict) -> None:
    payload = {
        **payload,
        "expires_at": time.time() + _PENDING_TTL_SEC,
    }
    _MEM_PENDING[chat_id] = payload
    r = _redis()
    if r is not None:
        try:
            r.setex(_key(chat_id), _PENDING_TTL_SEC, json.dumps(payload))
        except Exception as exc:
            logger.warning("ops_pending_set_error: %s", exc)


def clear_pending(chat_id: str) -> None:
    _MEM_PENDING.pop(chat_id, None)
    r = _redis()
    if r is not None:
        try:
            r.delete(_key(chat_id))
        except Exception:
            pass


def extract_ops_topic(text: str) -> str:
    """Short human topic for the confirm ask."""
    t = (text or "").strip()
    for pat, topic, _cmd in _TOPIC_COMMANDS:
        if pat.search(t):
            return topic
    # Fallback: first meaningful chunk, capped.
    cleaned = re.sub(r"(?i)^(can you|could you|please|hey|check|look at)\s+", "", t)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.!")
    return (cleaned[:40] or "that").strip()


def map_ops_command(text: str) -> Optional[str]:
    """Map NL ops ask → safe read command, or None if not mappable."""
    t = (text or "").strip()
    for pat, _topic, cmd in _TOPIC_COMMANDS:
        if pat.search(t):
            return cmd
    return None


def _dispatch_executor(command: str, chat_id: str, thread_id: str, user_id: int) -> None:
    """Send gated command to worker-runtime executor (policy gateway applies)."""
    try:
        from redis import Redis
        r = Redis.from_url(REDIS_URL, decode_responses=True)
        run_id = str(uuid.uuid4())
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "agent_role": "executor",
            "text": command,
            "chat_id": chat_id,
            "thread_id": thread_id or "",
            "user_id": str(user_id),
            "bypass_confirm": False,
            "correlation_id": run_id,
            "intent": "ops",
            "ops_confirmed": True,
        }
        r.xadd(DISPATCH_STREAM, {"payload": json.dumps(event)})
        logger.info("ops_dispatched command=%s chat_id=%s", command[:80], chat_id)
    except Exception as exc:
        logger.error("ops_dispatch_error: %s", exc)
        raise


def try_handle_ops(
    text: str,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    chat_type: str,
    send: SendFn,
    *,
    has_active_list: bool = False,
    is_collecting: bool = False,
) -> Optional[str]:
    """Gate ops asks. Return reason string if handled, else None."""
    if chat_type != "private":
        return None
    t = (text or "").strip()
    if not t:
        return None

    pending = get_pending(chat_id)
    if pending is not None:
        low = t.lower()
        if low in _YES:
            clear_pending(chat_id)
            original = pending.get("text") or ""
            action = pending.get("action") or "ops.shell_read"
            if is_refuse(action):
                send(chat_id, "Can't do that — out of policy.", thread_id)
                return "ops_refused"
            cmd = pending.get("command") or map_ops_command(original)
            if not cmd:
                send(
                    chat_id,
                    "Need a specific check — e.g. docker, disk, uptime.",
                    thread_id,
                )
                return "ops_need_specific"
            try:
                _dispatch_executor(cmd, chat_id, thread_id, telegram_user_id)
            except Exception:
                send(chat_id, "Couldn't queue that check — try again?", thread_id)
                return "ops_dispatch_failed"
            # Quiet ack — no brochure, no ✅
            topic = pending.get("topic") or extract_ops_topic(original)
            send(chat_id, f"Checking {topic}…", thread_id)
            return "ops_confirmed_dispatched"
        if low in _NO:
            clear_pending(chat_id)
            send(chat_id, "Okay, skipped.", thread_id)
            return "ops_cancelled"
        # Non-yes/no while pending: drop pending and fall through to re-classify
        clear_pending(chat_id)

    intent = classify_intent(
        t, has_active_list=has_active_list, is_collecting=is_collecting
    )
    if intent != "ops":
        return None

    action, policy = classify_action(intent, t)
    if policy == "refuse":
        send(chat_id, "Can't do that — out of policy.", thread_id)
        return "ops_refused"

    topic = extract_ops_topic(t)
    cmd = map_ops_command(t)
    set_pending(
        chat_id,
        {
            "text": t,
            "action": action,
            "policy": policy,
            "topic": topic,
            "command": cmd,
            "user_id": telegram_user_id,
            "thread_id": thread_id,
        },
    )
    # Short confirm — never a capability brochure
    if policy == "confirm":
        if action in ("ops.deploy", "ops.destructive", "ops.shell_write"):
            send(chat_id, f"That touches {topic} — want me to proceed?", thread_id)
        else:
            send(chat_id, f"Want me to check {topic}?", thread_id)
        return "ops_pending_confirm"

    # Shouldn't reach auto for ops given the table, but fail closed → confirm
    send(chat_id, f"Want me to check {topic}?", thread_id)
    return "ops_pending_confirm"
