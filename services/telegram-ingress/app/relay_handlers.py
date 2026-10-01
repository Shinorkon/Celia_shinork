"""Owner confirm path for Telegram relays staged by the life/frontoffice tool.

The model only stores a pending payload. This module sends after Falulaan
replies yes. Guests never send.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Optional

from redis import Redis

from app.guest_access import is_owner
from packages.reply_guard import looks_like_message_relay

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RELAY_PENDING_PREFIX = "celia:relay:pending:"

SendFn = Callable[[str, str, str], bool]

_YES = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "yup",
        "confirm",
        "send",
        "send it",
        "do it",
        "ok",
        "okay",
    }
)
_NO = frozenset({"no", "n", "nope", "cancel", "don't", "dont", "stop"})
_BARE_ID_RE = re.compile(r"^\d{6,15}$")


def pending_key(chat_id: str) -> str:
    return f"{RELAY_PENDING_PREFIX}{chat_id}"


def _redis() -> Optional[Redis]:
    try:
        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("relay_redis_unavailable: %s", exc)
        return None


def get_pending(chat_id: str) -> Optional[dict]:
    r = _redis()
    if r is None or not chat_id:
        return None
    try:
        raw = r.get(pending_key(chat_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("relay_pending_get_error: %s", exc)
        return None


def clear_pending(chat_id: str) -> None:
    r = _redis()
    if r is None or not chat_id:
        return
    try:
        r.delete(pending_key(chat_id))
    except Exception as exc:
        logger.warning("relay_pending_clear_error: %s", exc)


def set_pending(chat_id: str, payload: dict, ttl: int = 1800) -> None:
    r = _redis()
    if r is None or not chat_id:
        return
    try:
        r.setex(pending_key(chat_id), ttl, json.dumps(payload))
    except Exception as exc:
        logger.warning("relay_pending_set_error: %s", exc)


def try_handle_relay(
    text: str,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    chat_type: str,
    send: SendFn,
) -> Optional[str]:
    """Consume yes/no/id follow-ups for a staged relay. None if not our turn."""
    if chat_type != "private":
        return None
    t = (text or "").strip()
    if not t or not chat_id:
        return None
    if not is_owner(telegram_user_id):
        if get_pending(chat_id):
            clear_pending(chat_id)
        return None

    pending = get_pending(chat_id)
    if not pending:
        return None

    low = t.lower()
    if low in _YES:
        status = pending.get("status")
        rid = pending.get("telegram_user_id")
        body = (pending.get("text") or "").strip()
        if status != "confirm" or not rid or not body:
            send(
                chat_id,
                "I still need who and the exact text before I send anything.",
                thread_id,
            )
            return "relay_incomplete"
        label = pending.get("recipient_name") or str(rid)
        clear_pending(chat_id)
        delivered = send(str(rid), body, "")
        if delivered:
            send(chat_id, f"Sent that to {label}.", thread_id)
            return "relay_sent"
        send(chat_id, "I couldn't send that just now. Say it again and I'll restage it.", thread_id)
        return "relay_send_failed"

    if low in _NO:
        clear_pending(chat_id)
        send(chat_id, "Okay, I won't send it.", thread_id)
        return "relay_cancelled"

    if _BARE_ID_RE.match(t):
        rid = int(t)
        body = (pending.get("text") or "").strip()
        name = pending.get("recipient_name") or str(rid)
        if not body:
            set_pending(
                chat_id,
                {
                    "recipient_name": name,
                    "telegram_user_id": rid,
                    "text": "",
                    "status": "need_text",
                },
            )
            send(chat_id, f"Got {rid}. What should I send {name}?", thread_id)
            return "relay_need_text"
        set_pending(
            chat_id,
            {
                "recipient_name": name,
                "telegram_user_id": rid,
                "text": body,
                "status": "confirm",
            },
        )
        send(
            chat_id,
            f"Pass this to {name} ({rid})?\n\n{body}\n\nReply yes to send.",
            thread_id,
        )
        return "relay_confirm"

    if looks_like_message_relay(t):
        return None

    clear_pending(chat_id)
    return None
