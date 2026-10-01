"""Owner-side Telegram relay.

Resolve a known contact or an explicit chat id, stage a confirm, then send.
Does not grant access: a recipient id is a destination, never an allowlist add.
Guests cannot relay. Unknown names ask for an id instead of inventing one.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

PENDING_TTL_SEC = 10 * 60
_PENDING_PREFIX = "celia:relay:pending:"
_ID_RE = re.compile(r"^-?\d{5,}$")


def parse_known_contacts(raw: str | None = None) -> dict[str, int]:
    """Parse TELEGRAM_KNOWN_CONTACTS like 'Raaish:1210484792, Sam=55555'."""
    if raw is None:
        raw = os.getenv("TELEGRAM_KNOWN_CONTACTS", "")
    out: dict[str, int] = {}
    for part in re.split(r"[,;\n]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        match = re.match(r"^(.+?)[:=]\s*(-?\d+)\s*$", part)
        if not match:
            continue
        name = match.group(1).strip().lower()
        if not name:
            continue
        out[name] = int(match.group(2))
    return out


def resolve_recipient(
    query: str,
    contacts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Resolve a name or numeric chat/user id.

    Numeric ids are taken as given. Names match the known-contact map only.
    Nothing here writes users, roles, or allowlists.
    """
    book = contacts if contacts is not None else parse_known_contacts()
    q = (query or "").strip().strip("@")
    if not q:
        return {"ok": False, "reason": "need_recipient", "query": ""}
    if _ID_RE.match(q):
        return {
            "ok": True,
            "chat_id": q,
            "label": q,
            "reason": "explicit_id",
        }
    key = q.lower()
    if key in book:
        cid = str(book[key])
        return {"ok": True, "chat_id": cid, "label": q, "reason": "known_name"}
    # Single-token case-insensitive match (ignore extra words the model adds).
    token = re.split(r"\s+", key)[0]
    if token in book:
        cid = str(book[token])
        return {"ok": True, "chat_id": cid, "label": token, "reason": "known_name"}
    known = sorted({name for name in book})
    return {
        "ok": False,
        "reason": "need_recipient",
        "query": q,
        "known_names": known,
    }


def plan_relay(
    *,
    audience: str,
    recipient: str,
    text: str,
    confirmed: bool = False,
    pending: dict | None = None,
    contacts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Decide the next relay step. Never sends by itself.

    status:
      refused — guest / locked caller
      need_text — no message body
      need_recipient — ask for a chat or user id
      pending_confirm — stage and ask for yes
      send — pending matches and the user already confirmed
    """
    who = (audience or "guest").strip().lower()
    if who != "owner":
        return {
            "status": "refused",
            "tool_message": (
                "REFUSED: This chat cannot message other people. "
                "Say so in one sentence and offer to draft the words here. "
                "Do not list capabilities and do not claim you can only chat."
            ),
        }

    body = (text or "").strip()
    if not body:
        return {
            "status": "need_text",
            "tool_message": (
                "NEED_TEXT: Ask what the message should say. "
                "You can draft it and confirm before sending. "
                "Do not refuse with a capability menu."
            ),
        }

    resolved = resolve_recipient(recipient, contacts)
    if not resolved.get("ok"):
        names = resolved.get("known_names") or []
        hint = ""
        if names:
            hint = " Known names: " + ", ".join(names) + "."
        label = resolved.get("query") or (recipient or "").strip() or "them"
        return {
            "status": "need_recipient",
            "tool_message": (
                f"NEED_RECIPIENT: No Telegram chat id on file for {label}.{hint} "
                "Ask which chat or user id to use. You may draft the message "
                "in the same reply. Do not invent an id. Do not claim you are "
                "limited to conversation. Do not list capabilities."
            ),
        }

    chat_id = str(resolved["chat_id"])
    label = str(resolved.get("label") or chat_id)
    staged = {
        "chat_id": chat_id,
        "label": label,
        "text": body,
    }
    pending = pending or {}
    same = (
        str(pending.get("chat_id") or "") == chat_id
        and (pending.get("text") or "").strip() == body
    )
    if confirmed and same:
        return {
            "status": "send",
            "chat_id": chat_id,
            "label": label,
            "text": body,
            "tool_message": (
                f"SENT: Delivered to {label} (chat {chat_id}). "
                "One short confirmation. Do not recap capabilities."
            ),
        }
    preview = body if len(body) <= 280 else body[:277] + "..."
    return {
        "status": "pending_confirm",
        "pending": staged,
        "tool_message": (
            f"PENDING_CONFIRM: Ready to send to {label} (chat {chat_id}): "
            f"\"{preview}\". Ask them to reply yes to send, or no to cancel. "
            "Do not send yet. Do not list capabilities."
        ),
    }


def pending_key(chat_id: str) -> str:
    return f"{_PENDING_PREFIX}{chat_id}"


def save_pending(chat_id: str, payload: dict, redis_client: Any) -> None:
    redis_client.setex(pending_key(chat_id), PENDING_TTL_SEC, json.dumps(payload))


def load_pending(chat_id: str, redis_client: Any) -> Optional[dict]:
    raw = redis_client.get(pending_key(chat_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def clear_pending(chat_id: str, redis_client: Any) -> None:
    redis_client.delete(pending_key(chat_id))
