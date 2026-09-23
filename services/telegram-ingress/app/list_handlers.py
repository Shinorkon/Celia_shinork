"""Ingress-local list handlers (Phase A + B) — short replies, no stack tour / ✅."""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from app.intent_router import (
    looks_like_list_intent,
    parse_item_line,
    extract_list_title,
    parse_check_query,
    parse_remove_query,
    parse_rename_title,
    LIST_MAKE_RE,
    LIST_SHOW_RE,
    LIST_ADD_RE,
    LIST_DONE_RE,
    LIST_CHECK_RE,
    LIST_REMOVE_RE,
    LIST_RENAME_RE,
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
    return f"{name} ×{q}"


def _fmt_items_short(items: list[dict], *, limit: int = 6) -> str:
    parts = [_fmt_item(i.get("name") or "?", int(i.get("qty") or 1)) for i in items[:limit]]
    extra = len(items) - limit
    body = ", ".join(parts)
    if extra > 0:
        body += f" (+{extra} more)"
    return body


def _title_of(doc: dict) -> str:
    return (doc.get("title") or "List").strip() or "List"


def _count_phrase(doc: dict) -> str:
    n = store.total_item_count(doc)
    return f"{n} item" if n == 1 else f"{n} items"


def _fmt_show(doc: dict) -> str:
    title = _title_of(doc)
    items = doc.get("items") or []
    if not items:
        return f"{title} is empty."
    lines = []
    for it in items:
        mark = "x" if it.get("done") else " "
        lines.append(f"[{mark}] {_fmt_item(it.get('name') or '?', int(it.get('qty') or 1))}")
    # Keep reply short: inline if ≤4, else newline list
    if len(lines) <= 4:
        body = "; ".join(lines)
        return f"{title} ({_count_phrase(doc)}): {body}."
    body = "\n".join(lines)
    return f"{title} ({_count_phrase(doc)}):\n{body}"


def _extract_items_from_text(text: str) -> list[dict]:
    """Parse item lines; skip make/show/done/meta lines."""
    items: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if LIST_MAKE_RE.search(line) or LIST_SHOW_RE.search(line):
            continue
        if LIST_DONE_RE.match(line) or LIST_RENAME_RE.match(line):
            continue
        if LIST_CHECK_RE.match(line) or LIST_REMOVE_RE.match(line):
            continue
        add_m = re.match(
            r"(?i)^(?:add|put|append)\s+(.+?)(?:\s+(?:to|on)\s+(?:the\s+|my\s+)?list)?\s*$",
            line,
        )
        candidate = add_m.group(1).strip() if add_m else line
        if candidate.lower() in {"to list", "on list", "the list", "my list", "list"}:
            continue
        # "add X to Groceries" — strip trailing "to <title>"
        candidate = re.sub(
            r"(?i)\s+to\s+(?:the\s+|my\s+)?(?:list|grocer(?:y|ies)|shopping|todo)\s*$",
            "",
            candidate,
        ).strip()
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
    collecting = store.is_collecting(chat_id)
    if not looks_like_list_intent(
        text, has_active_list=active, is_collecting=collecting
    ):
        return None

    # --- show ---
    if LIST_SHOW_RE.search(text) and not LIST_MAKE_RE.search(text):
        doc = store.get_active_list(chat_id)
        if doc is None or not doc.get("items"):
            send(
                chat_id,
                _strip_list_opener("No list yet — say make a list and add items."),
                thread_id,
            )
            return "list_empty"
        send(chat_id, _strip_list_opener(_fmt_show(doc)), thread_id)
        return "list_shown"

    # --- done / end collecting ---
    if LIST_DONE_RE.match(text) and (active or collecting):
        doc = store.get_active_list(chat_id)
        store.end_session(chat_id)
        if doc is None:
            send(chat_id, _strip_list_opener("No active list."), thread_id)
            return "list_done_empty"
        send(
            chat_id,
            _strip_list_opener(
                f"Okay — {_title_of(doc)} saved ({_count_phrase(doc)})."
            ),
            thread_id,
        )
        return "list_done"

    # --- rename / title ---
    rename = parse_rename_title(text)
    if rename and active:
        doc = store.get_active_list(chat_id)
        if doc is None:
            send(chat_id, _strip_list_opener("No list yet — say make a list first."), thread_id)
            return "list_missing"
        updated = store.set_title(doc["list_id"], rename)
        assert updated is not None
        send(
            chat_id,
            _strip_list_opener(f"Renamed to {_title_of(updated)}."),
            thread_id,
        )
        return "list_renamed"

    # --- check-off / bought ---
    check_q = parse_check_query(text)
    if check_q and active:
        doc = store.get_active_list(chat_id)
        if doc is None:
            send(chat_id, _strip_list_opener("No list yet — say make a list first."), thread_id)
            return "list_missing"
        updated, matched = store.mark_items(doc["list_id"], check_q, done=True)
        if matched is None:
            send(
                chat_id,
                _strip_list_opener(f"Couldn't find “{check_q}” on {_title_of(doc)}."),
                thread_id,
            )
            return "list_item_missing"
        assert updated is not None
        send(
            chat_id,
            _strip_list_opener(
                f"Marked {_fmt_item(matched['name'], matched['qty'])} bought "
                f"on {_title_of(updated)} ({_count_phrase(updated)})."
            ),
            thread_id,
        )
        return "list_checked"

    # --- remove ---
    rem_q = parse_remove_query(text)
    if rem_q and active:
        doc = store.get_active_list(chat_id)
        if doc is None:
            send(chat_id, _strip_list_opener("No list yet — say make a list first."), thread_id)
            return "list_missing"
        updated, matched = store.remove_item(doc["list_id"], rem_q)
        if matched is None:
            send(
                chat_id,
                _strip_list_opener(f"Couldn't find “{rem_q}” on {_title_of(doc)}."),
                thread_id,
            )
            return "list_item_missing"
        assert updated is not None
        send(
            chat_id,
            _strip_list_opener(
                f"Removed {_fmt_item(matched['name'], matched['qty'])} "
                f"from {_title_of(updated)} ({_count_phrase(updated)})."
            ),
            thread_id,
        )
        return "list_removed"

    items = _extract_items_from_text(text)
    making = bool(LIST_MAKE_RE.search(text))
    title = extract_list_title(text) or "List"

    if making:
        doc = store.create_list(chat_id, title=title, items=items, collecting=True)
        if items:
            body = _fmt_items_short(items)
            msg = (
                f"Added {body} to {_title_of(doc)} "
                f"({_count_phrase(doc)})."
            )
        else:
            msg = f"{_title_of(doc)} started. Send items whenever."
        send(chat_id, _strip_list_opener(msg), thread_id)
        return "list_created"

    doc = store.get_active_list(chat_id)
    if doc is None:
        if items:
            # Open collecting implicitly when items arrive without make
            doc = store.create_list(chat_id, title="List", items=items, collecting=True)
            body = _fmt_items_short(items)
            send(
                chat_id,
                _strip_list_opener(
                    f"Added {body} to {_title_of(doc)} ({_count_phrase(doc)})."
                ),
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

    # Collecting session or active list: append
    if not collecting:
        store.start_session(chat_id, doc["list_id"])
    updated = store.append_items(doc["list_id"], items)
    if updated is None:
        send(
            chat_id,
            _strip_list_opener("Couldn't update the list — try again?"),
            thread_id,
        )
        return "list_error"
    body = _fmt_items_short(items)
    send(
        chat_id,
        _strip_list_opener(
            f"Added {body} to {_title_of(updated)} ({_count_phrase(updated)})."
        ),
        thread_id,
    )
    return "list_appended"
