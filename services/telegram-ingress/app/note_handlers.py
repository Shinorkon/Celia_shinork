"""Thin notes via memory_items kind=note (Life OS slice 4). Auto; no notes table."""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from app.side_effect_policy import policy_for
from app import memory_store as store

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, str], bool]

_NOTE_SAVE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"note\s*:\s*|"
    r"jot(?:\s+down)?\s*[:\-]?\s*|"
    r"save\s+this\s*[:\-]?\s*|"
    r"save\s+note\s*[:\-]?\s*|"
    r"quick\s+note\s*[:\-]?\s*"
    r")(.+)$"
)

_NOTE_RECALL = re.compile(
    r"(?i)^(?:"
    r"(?:what\s+)?notes?(?:\s+(?:about|on|for|re)\s+(.+))?|"
    r"recall(?:\s+notes?)?(?:\s+(?:about|on|for|re)\s+(.+))?|"
    r"show\s+notes?(?:\s+(?:about|on|for|re)\s+(.+))?|"
    r"/notes?(?:\s+(.+))?"
    r")\s*$"
)


def looks_like_note(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_NOTE_SAVE.match(t) or _NOTE_RECALL.match(t))


def _title_from_body(body: str) -> str:
    first = re.split(r"[.\n]", body.strip(), maxsplit=1)[0].strip()
    words = first.split()
    title = " ".join(words[:8]) if words else body[:60]
    return (title[:80] or "note").strip()


def _segment_for_note(body: str) -> str:
    if re.search(
        r"(?i)\b(?:today|yesterday|this\s+morning|just\s+now|earlier|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        body,
    ):
        return "episodic"
    return "semantic"


def try_handle_notes(
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
    if not t or not looks_like_note(t):
        return None

    m_save = _NOTE_SAVE.match(t)
    m_recall = _NOTE_RECALL.match(t)
    action = "note.create" if m_save else "note.read"
    if policy_for(action) == "refuse":
        send(chat_id, "Can't help with that.", thread_id)
        return "note_refused"

    # Secrets exfil tooling still refuse via classify_action upstream; storing
    # a personal note (even with a wifi password) is allowed.
    db_user_id = store.ensure_user(telegram_user_id)
    if db_user_id is None:
        send(chat_id, "Couldn't reach notes right now.", thread_id)
        return "note_store_unavailable"

    if m_save:
        body = m_save.group(1).strip()
        if not body:
            send(chat_id, "What should I note?", thread_id)
            return "note_clarify"
        title = _title_from_body(body)
        segment = _segment_for_note(body)
        new_id = store.save_item(
            db_user_id=db_user_id,
            kind="note",
            title=title,
            body=body,
            segment=segment,
            tags=["note"],
            source_chat_id=chat_id,
            importance=0.55,
            salience=0.6,
        )
        if new_id is None:
            send(chat_id, "Couldn't save that note.", thread_id)
            return "note_save_failed"
        send(chat_id, "Noted.", thread_id)
        return "note_saved"

    topic = None
    if m_recall:
        for g in m_recall.groups():
            if g and g.strip():
                topic = g.strip()
                break

    if topic:
        cands = store.find_candidates(db_user_id=db_user_id, query=topic, limit=20)
    else:
        cands = store.list_known(db_user_id=db_user_id, limit=30)

    notes = [c for c in cands if (c.get("kind") or "").lower() == "note"]
    if not notes and topic:
        notes = cands  # keyword hits still useful
    if not notes:
        send(chat_id, "No notes matched." if topic else "No notes yet.", thread_id)
        return "note_recall_empty"

    lines = [f"• {n['title']}: {(n.get('body') or '')[:140]}" for n in notes[:8]]
    send(chat_id, "Notes:\n" + "\n".join(lines), thread_id)
    try:
        store.touch_access([n["id"] for n in notes[:8]])
    except Exception:
        pass
    return "note_recalled"
