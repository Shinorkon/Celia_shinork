"""Ingress-local list handlers (Phase A) — short replies, no stack tour / ✅."""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from app.intent_router import (
    looks_like_list_intent,
    parse_item_line,
    LIST_MAKE_RE,
    LIST_SHOW_RE,
    LIST_ADD_RE,
)
from app import list_store as store

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, str], bool]  # chat_id, text, thread_id


def _strip_list_opener(msg: str) -> str:
    """Never ship ✅/capability openers on list replies (same rule as finance)."""
    s = (msg or "").strip()
    s = re.sub(r"^[\u2705\u274c\u2139\ufe0f]+\s*", "", s)
    s = re.sub(r"^✅\s*", "", s)
    banned = (
        "shnuk",
        "budgy",
        "vps",
        "i can help with",
        "here's what i can",
        "capability",
    )
    low = s.lower()
    if any(b in low for b in banned):
        s = re.split(r"[.\n]", s, maxsplit=1)[0].strip()
        if not s or any(b in s.lower() for b in banned):
            s = "Updated your list."
    return s


def _fmt_item(name: str, qty: int) -> str:
    q = int(qty or 1)
    if q == 1:
        return name
    return f"{name} x{q}"


def _fmt_items_short(items: list[dict], *, limit: int = 6) -> str:
    parts = [_fmt_item(i.get("name") or "?", int(i.get("qty") or 1)) for i in items[:limit]]
    extra = len(items) - limit
    body = ", ".join(parts)
    if extra > 0:
        body += f" (+{extra} more)"
    return body


def _extract_items_from_text(text: str) -> list[dict]:
    """Parse item lines; skip make/show meta lines."""
    items: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if LIST_MAKE_RE.search(line) or LIST_SHOW_RE.search(line):
            continue
        add_m = re.match(
            r"(?i)^(?:add|put|append)\s+(.+?)(?:\s+(?:to|on)\s+(?:the\s+|my\s+)?list)?\s*$",
            line,
        )
        candidate = add_m.group(1).strip() if add_m else line
        if candidate.lower() in {"to list", "on list", "the list", "my list", "list"}:
            continue
        parsed = parse_item_line(candidate)
        if parsed:
            name, qty = parsed
            items.append({"name": name, "qty": qty})
            continue
        # Plain item phrase
        if add_m or (not LIST_ADD_RE.search(line) and "?" not in candidate):
            if 1 <= len(candidate.split()) <= 8:
                items.append({"name": candidate, "qty": 1})
    return items


def try_handle_list(
    *,
    text: str,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    chat_type: str,
    send: SendFn,
) -> Optional[str]:
    """Handle list intents. Return reason if handled (do NOT publish to AOP)."""
    if chat_type != "private":
        return None
    text = (text or "").strip()
    if not text:
        return None

    active = store.has_active_list(chat_id)
    if not looks_like_list_intent(text, has_active_list=active):
        return None

    if LIST_SHOW_RE.search(text) and not LIST_MAKE_RE.search(text):
        doc = store.get_active_list(chat_id)
        if doc is None or not doc.get("items"):
            send(
                chat_id,
                _strip_list_opener("No list yet — say make a list and add items."),
                thread_id,
            )
            return "list_empty"
        body = _fmt_items_short(doc["items"])
        send(
            chat_id,
            _strip_list_opener(f"{doc.get('title') or 'List'}: {body}."),
            thread_id,
        )
        return "list_shown"

    items = _extract_items_from_text(text)
    making = bool(LIST_MAKE_RE.search(text))

    if making:
        doc = store.create_list(chat_id, title="List", items=items)
        if items:
            body = _fmt_items_short(doc["items"])
            msg = _strip_list_opener(f"Got it — {body} on your list.")
        else:
            msg = _strip_list_opener("List started. Send items whenever.")
        send(chat_id, msg, thread_id)
        return "list_created"

    doc = store.get_active_list(chat_id)
    if doc is None:
        if items:
            doc = store.create_list(chat_id, title="List", items=items)
            body = _fmt_items_short(doc["items"])
            send(
                chat_id,
                _strip_list_opener(f"Got it — {body} on your list."),
                thread_id,
            )
            return "list_created"
        send(
            chat_id,
            _strip_list_opener("No list yet — say make a list first."),
            thread_id,
        )
        return "list_missing"

    if not items:
        send(chat_id, _strip_list_opener("What should I add?"), thread_id)
        return "list_clarify"

    updated = store.append_items(doc["list_id"], items)
    if updated is None:
        send(
            chat_id,
            _strip_list_opener("Couldn't update the list — try again?"),
            thread_id,
        )
        return "list_error"
    body = _fmt_items_short(items)
    send(chat_id, _strip_list_opener(f"Added {body}."), thread_id)
    return "list_appended"
