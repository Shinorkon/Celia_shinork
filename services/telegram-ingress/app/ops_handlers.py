"""Ops confirm/auto gate (Phase C + Phase D multi-step).

Read/status (ops.shell_read) → auto-dispatch the `ops` agent (multi-step
Celia self-ops tool loop). Mutating/deploy/destructive → confirm first,
then dispatch `ops` with mutate_confirmed so restart tools may run.

Other apps (Shnuk/Oreuda/Budgy/…) stay refuse. Chat stays on the LLM path;
lists/finance are handled earlier and untouched.
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

# NL topic → short label for the confirm/ack line (command map kept for tests).
_TOPIC_COMMANDS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"(?i)\bdocker\b|\bcontainer"), "docker", "docker ps --format 'table {{.Names}}\t{{.Status}}'"),
    (re.compile(r"(?i)\bdisk\b|\bdf\b|\bspace\b"), "disk space", "df -h"),
    (re.compile(r"(?i)\buptime\b|\bload\b"), "uptime", "uptime"),
    (re.compile(r"(?i)\bmem(?:ory)?\b|\bfree\b"), "memory", "free -h"),
    (re.compile(r"(?i)\bnginx\b|\bcaddy\b|\bedge\b"), "edge", "systemctl is-active nginx || true"),
    (re.compile(r"(?i)\bcelia\b|\baop\b|\bhealth\b"), "Celia", "uptime"),
    (re.compile(r"(?i)\bsystemd\b|\bservice"), "services", "systemctl list-units --type=service --state=running --no-pager | head -30"),
    (re.compile(r"(?i)\bcron\b"), "cron", "crontab -l 2>/dev/null || echo '(no crontab)'"),
    (re.compile(r"(?i)\bssh\b|\bvps\b|\bserver\b"), "server status", "uptime && df -h / | tail -1"),
]

# Prefer concrete aop-*/celia-* names over the generic "Celia" topic when
# the user names a container (e.g. "restart aop-worker" → "aop-worker").
_NAMED_CONTAINER_RE = re.compile(r"(?i)\b((?:aop|celia)-[a-z0-9][a-z0-9_.-]*)\b")


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
    named = _NAMED_CONTAINER_RE.findall(t)
    if named:
        seen: list[str] = []
        for n in named:
            low = n.lower()
            if low not in {x.lower() for x in seen}:
                seen.append(n)
        return ", ".join(seen[:3])
    for pat, topic, _cmd in _TOPIC_COMMANDS:
        if pat.search(t):
            return topic
    cleaned = re.sub(r"(?i)^(can you|could you|please|hey|check|look at)\s+", "", t)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.!")
    return (cleaned[:40] or "that").strip()


def map_ops_command(text: str) -> Optional[str]:
    """Map NL ops ask → safe read command (legacy / tests). Multi-step ops agent preferred."""
    t = (text or "").strip()
    for pat, _topic, cmd in _TOPIC_COMMANDS:
        if pat.search(t):
            return cmd
    return None


def _dispatch_ops_agent(
    text: str,
    chat_id: str,
    thread_id: str,
    user_id: int,
    mutate_confirmed: bool = False,
) -> None:
    """Send NL ask to worker `ops` role (multi-step Celia self-ops tools)."""
    try:
        from redis import Redis
        r = Redis.from_url(REDIS_URL, decode_responses=True)
        run_id = str(uuid.uuid4())
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "agent_role": "ops",
            "text": text,
            "chat_id": chat_id,
            "thread_id": thread_id or "",
            "user_id": str(user_id),
            "bypass_confirm": False,
            "ops_confirmed": True,
            "ops_mutate_confirmed": bool(mutate_confirmed),
            "correlation_id": run_id,
            "intent": "ops",
        }
        r.xadd(DISPATCH_STREAM, {"payload": json.dumps(event)})
        logger.info(
            "ops_agent_dispatched chat_id=%s mutate=%s text=%s",
            chat_id,
            mutate_confirmed,
            (text or "")[:80],
        )
    except Exception as exc:
        logger.error("ops_dispatch_error: %s", exc)
        raise


# Back-compat alias for older tests / imports
def _dispatch_executor(command: str, chat_id: str, thread_id: str, user_id: int) -> None:
    _dispatch_ops_agent(command, chat_id, thread_id, user_id, mutate_confirmed=False)


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
            mutate = action in ("ops.deploy", "ops.destructive", "ops.shell_write")
            try:
                _dispatch_ops_agent(
                    original, chat_id, thread_id, telegram_user_id, mutate_confirmed=mutate
                )
            except Exception:
                send(chat_id, "Couldn't queue that — try again?", thread_id)
                return "ops_dispatch_failed"
            topic = pending.get("topic") or extract_ops_topic(original)
            if mutate:
                send(chat_id, f"On it — {topic}…", thread_id)
            else:
                send(chat_id, f"Checking {topic}…", thread_id)
            return "ops_confirmed_dispatched"
        if low in _NO:
            clear_pending(chat_id)
            send(chat_id, "Okay, skipped.", thread_id)
            return "ops_cancelled"
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

    if policy == "auto":
        try:
            _dispatch_ops_agent(
                t, chat_id, thread_id, telegram_user_id, mutate_confirmed=False
            )
        except Exception:
            send(chat_id, "Couldn't queue that check — try again?", thread_id)
            return "ops_dispatch_failed"
        send(chat_id, f"Checking {topic}…", thread_id)
        return "ops_auto_dispatched"

    # confirm — mutating / deploy / destructive
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
    if action in ("ops.deploy", "ops.destructive", "ops.shell_write"):
        send(chat_id, f"That touches {topic} — want me to proceed?", thread_id)
    else:
        send(chat_id, f"Want me to check {topic}?", thread_id)
    return "ops_pending_confirm"
