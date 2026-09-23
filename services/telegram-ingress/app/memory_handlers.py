"""Ingress memory intents: remember / forget / correct / what do you know.

Forget and correct require confirm (POLICY_TABLE memory.forget / memory.correct).
Explicit remember is auto. Quiet Carlia voice — no capability brochures.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Callable, Optional

from app.side_effect_policy import policy_for
from app import memory_store as store

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, str], bool]

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_PENDING_TTL_SEC = int(os.getenv("MEMORY_PENDING_TTL_SEC", "300"))
_PENDING_KEY = "celia:memory:pending:{chat_id}"

_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "do it", "go ahead"}
_NO = {"no", "n", "nope", "nah", "cancel", "stop", "don't", "dont"}

_MEM_PENDING: dict[str, dict] = {}

_REMEMBER_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:remember(?:\s+that)?|note\s+that|keep\s+in\s+mind(?:\s+that)?)\s*[,:]?\s*(.+)$"
)
_FORGET_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:forget(?:\s+that)?|don't\s+remember|do\s+not\s+remember|"
    r"stop\s+remembering)\s*[,:]?\s*(.+)$"
)
_CORRECT_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:correct(?:\s+that)?|actually|update\s+memory|fix\s+memory)\s*[,:]?\s*(.+)$"
)
_KNOW_RE = re.compile(
    r"(?i)^(?:what\s+do\s+you\s+know\s+about\s+me\??|"
    r"what\s+do\s+you\s+remember(?:\s+about\s+me)?\??|"
    r"what\s+have\s+you\s+got\s+on\s+me\??|"
    r"/memory)\s*$"
)
# "Correct: X is Y" / "correct that budget for groceries is 3000"
_CORRECT_IS_RE = re.compile(
    r"(?i)^(?:correct(?:\s+that)?|actually)[:\s]+(.+?)\s+(?:is|are|=|:)\s+(.+)$"
)


def clear_memory_for_tests() -> None:
    _MEM_PENDING.clear()


def _redis():
    try:
        from redis import Redis

        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("memory_pending_redis_unavailable: %s", exc)
        return None


def _key(chat_id: str) -> str:
    return _PENDING_KEY.format(chat_id=chat_id)


def get_pending(chat_id: str) -> Optional[dict]:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_key(chat_id))
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("memory_pending_get_error: %s", exc)
    data = _MEM_PENDING.get(chat_id)
    if data and data.get("expires_at", 0) < time.time():
        _MEM_PENDING.pop(chat_id, None)
        return None
    return data


def set_pending(chat_id: str, payload: dict) -> None:
    payload = {**payload, "expires_at": time.time() + _PENDING_TTL_SEC}
    _MEM_PENDING[chat_id] = payload
    r = _redis()
    if r is not None:
        try:
            r.setex(_key(chat_id), _PENDING_TTL_SEC, json.dumps(payload))
        except Exception as exc:
            logger.warning("memory_pending_set_error: %s", exc)


def clear_pending(chat_id: str) -> None:
    _MEM_PENDING.pop(chat_id, None)
    r = _redis()
    if r is not None:
        try:
            r.delete(_key(chat_id))
        except Exception:
            pass


def looks_like_memory(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _KNOW_RE.match(t):
        return True
    if _REMEMBER_RE.match(t) or _FORGET_RE.match(t) or _CORRECT_RE.match(t):
        return True
    return False


def _short_label(item: dict) -> str:
    title = (item.get("title") or "").strip()
    body = (item.get("body") or "").strip()
    if title:
        return title
    return body[:60] + ("…" if len(body) > 60 else "")


def _parse_remember_payload(rest: str) -> tuple[str, str, str]:
    """Return (kind, title, body) for an explicit remember utterance."""
    rest = rest.strip().rstrip(".")
    low = rest.lower()
    kind = "fact"
    if re.search(r"(?i)\b(?:prefer|preference|like(?:s)?\s+to|want(?:s)?\s+me\s+to)\b", rest):
        kind = "preference"
    elif re.search(r"(?i)\b(?:goal|want\s+to|planning\s+to|aim)\b", rest):
        kind = "goal"
    elif re.search(r"(?i)\b(?:decided|decision|chose|going\s+with)\b", rest):
        kind = "decision"
    # Title: first ~8 words
    words = rest.split()
    title = " ".join(words[:8]) if words else rest[:40]
    if kind == "preference" and "prefer" in low:
        title = rest[:60]
    return kind, title[:80], rest


def _parse_correct_payload(rest: str) -> tuple[Optional[str], str, str, str]:
    """Return (match_query, kind, title, body)."""
    m = _CORRECT_IS_RE.match("correct " + rest) if not rest.lower().startswith("correct") else _CORRECT_IS_RE.match(
        "correct: " + rest if ":" not in rest[:20] else rest
    )
    # Try full text forms
    full = rest
    m = re.match(
        r"(?i)^(?:that\s+)?(.+?)\s+(?:is|are|=)\s+(.+)$",
        full.strip(),
    )
    if m:
        subject = m.group(1).strip()
        value = m.group(2).strip().rstrip(".")
        kind = "preference" if re.search(r"(?i)\b(?:prefer|budget|limit)\b", subject + " " + value) else "fact"
        if re.search(r"(?i)\bbudget|limit\b", subject):
            kind = "fact"
        title = subject[:80]
        body = f"{subject} is {value}"
        return subject, kind, title, body
    kind, title, body = _parse_remember_payload(full)
    return full, kind, title, body


def try_handle_memory(
    text: str,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    chat_type: str,
    send: SendFn,
) -> Optional[str]:
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
            return _apply_pending(pending, chat_id, telegram_user_id, thread_id, send)
        if low in _NO:
            clear_pending(chat_id)
            send(chat_id, "Okay, left it.", thread_id)
            return "memory_cancelled"
        # Non-yes/no: drop pending and re-classify this turn
        clear_pending(chat_id)

    if not looks_like_memory(t):
        return None

    db_user_id = store.ensure_user(telegram_user_id)
    if db_user_id is None:
        send(chat_id, "Couldn't reach memory right now.", thread_id)
        return "memory_db_error"

    if _KNOW_RE.match(t):
        items = store.list_known(db_user_id=db_user_id, limit=10)
        if not items:
            send(chat_id, "Not much stored yet — tell me something to remember.", thread_id)
            return "memory_know_empty"
        lines = []
        for it in items[:8]:
            lines.append(f"• {it['title']}: {it['body'][:120]}")
        send(chat_id, "Here's what I've got:\n" + "\n".join(lines), thread_id)
        store.touch_access([it["id"] for it in items])
        return "memory_know"

    m = _REMEMBER_RE.match(t)
    if m:
        if policy_for("memory.write") != "auto":
            # Table says auto for explicit remember; fail closed → still save if auto missing
            pass
        kind, title, body = _parse_remember_payload(m.group(1))
        if store.is_junk_memory_item(kind, title, body):
            send(chat_id, "That looks ephemeral — not storing it as a preference.", thread_id)
            return "memory_junk_skipped"
        new_id = store.save_item(
            db_user_id=db_user_id,
            kind=kind,
            title=title,
            body=body,
            source_chat_id=chat_id,
            importance=0.7,
            salience=0.7,
        )
        if new_id is None:
            send(chat_id, "Couldn't save that — try again?", thread_id)
            return "memory_save_failed"
        send(chat_id, f"Got it — I'll remember that.", thread_id)
        return "memory_remembered"

    m = _FORGET_RE.match(t)
    if m:
        rest = m.group(1).strip()
        # Strip leading "the" / trailing "thing"
        rest_clean = re.sub(r"(?i)^(the\s+)?", "", rest)
        rest_clean = re.sub(r"(?i)\s+thing\.?$", "", rest_clean).strip()
        cands = store.find_candidates(db_user_id=db_user_id, query=rest_clean, limit=3)
        if not cands:
            send(chat_id, "Don't think I have that stored.", thread_id)
            return "memory_forget_miss"
        if policy_for("memory.forget") == "confirm":
            top = cands[0]
            set_pending(
                chat_id,
                {
                    "action": "forget",
                    "target_id": top["id"],
                    "label": _short_label(top),
                    "reason": rest_clean,
                    "user_id": telegram_user_id,
                },
            )
            send(
                chat_id,
                f"Forget “{_short_label(top)}”?",
                thread_id,
            )
            return "memory_forget_pending"
        ok = store.soft_forget(db_user_id=db_user_id, target_id=cands[0]["id"], reason=rest_clean)
        send(chat_id, "Forgotten." if ok else "Couldn't forget that.", thread_id)
        return "memory_forgotten" if ok else "memory_forget_failed"

    m = _CORRECT_RE.match(t)
    if m:
        rest = m.group(1).strip()
        match_q, kind, title, body = _parse_correct_payload(rest)
        cands = store.find_candidates(db_user_id=db_user_id, query=match_q or rest, limit=3)
        if not cands:
            # No prior row — treat as remember
            new_id = store.save_item(
                db_user_id=db_user_id,
                kind=kind,
                title=title,
                body=body,
                source_chat_id=chat_id,
            )
            send(
                chat_id,
                "Got it — stored that." if new_id else "Couldn't save the correction.",
                thread_id,
            )
            return "memory_correct_as_new" if new_id else "memory_correct_failed"
        top = cands[0]
        if policy_for("memory.correct") == "confirm":
            set_pending(
                chat_id,
                {
                    "action": "correct",
                    "target_id": top["id"],
                    "label": _short_label(top),
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "reason": rest,
                    "user_id": telegram_user_id,
                },
            )
            send(
                chat_id,
                f"Replace “{_short_label(top)}” with “{title}”?",
                thread_id,
            )
            return "memory_correct_pending"
        new_id = store.correct_item(
            db_user_id=db_user_id,
            target_id=top["id"],
            new_kind=kind,
            new_title=title,
            new_body=body,
            reason=rest,
        )
        send(chat_id, "Updated." if new_id else "Couldn't update that.", thread_id)
        return "memory_corrected" if new_id else "memory_correct_failed"

    return None


def _apply_pending(
    pending: dict,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    send: SendFn,
) -> str:
    db_user_id = store.ensure_user(telegram_user_id)
    if db_user_id is None:
        send(chat_id, "Couldn't reach memory right now.", thread_id)
        return "memory_db_error"
    action = pending.get("action")
    if action == "forget":
        ok = store.soft_forget(
            db_user_id=db_user_id,
            target_id=int(pending["target_id"]),
            reason=pending.get("reason"),
        )
        send(chat_id, "Forgotten." if ok else "Couldn't forget that.", thread_id)
        return "memory_forgotten" if ok else "memory_forget_failed"
    if action == "correct":
        new_id = store.correct_item(
            db_user_id=db_user_id,
            target_id=int(pending["target_id"]),
            new_kind=pending.get("kind") or "fact",
            new_title=pending.get("title") or "correction",
            new_body=pending.get("body") or "",
            reason=pending.get("reason"),
        )
        send(chat_id, "Updated." if new_id else "Couldn't update that.", thread_id)
        return "memory_corrected" if new_id else "memory_correct_failed"
    send(chat_id, "Okay, skipped.", thread_id)
    return "memory_pending_unknown"
