"""Intent router ahead of frontoffice (Phase A + B).

Classifies a (possibly debounce-merged) turn into:
  list | finance | ops | memory | task | reminder | chat | clarify

Finance heuristics stay authoritative — this module imports looks_like_finance
as reference and never steals receipt/spend paths.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from app.finance_parse import looks_like_finance, parse_finance
from app.calendar_parse import looks_like_calendar, is_agenda_query

Intent = Literal["list", "finance", "ops", "memory", "task", "reminder", "calendar", "note", "chat", "clarify"]

LIST_MAKE_RE = re.compile(
    r"(?i)(?:^|\b)(?:make|create|start|new|begin|open)\s+"
    r"(?:(?:a|me|us|my)\s+)*(?:shopping\s+|grocery\s+|todo\s+|to-?do\s+)?"
    r"list\b"
    r"|(?:^|\b)(?:shopping|grocery|todo|to-?do)\s+list\b"
    r"|(?:^|\b)list\s+(?:please|for\s+me)\b"
)

LIST_SHOW_RE = re.compile(
    r"(?i)^(?:/?list|show|view|open|get|what's\s+on|whats\s+on)\s+"
    r"(?:my\s+|the\s+)?list\b|^/list\s*$|^show\s+list\s*$"
    r"|^(?:show|view|open)\s+(?:my\s+|the\s+)?(?:grocer(?:y|ies)|shopping|todo|to-?do)\b"
)

LIST_ADD_RE = re.compile(
    r"(?i)\b(?:add|put|append)\b.+\b(?:to|on)\s+(?:the\s+|my\s+)?list\b"
    r"|\badd\s+to\s+list\b"
)

# Phase B: done / stop collecting
LIST_DONE_RE = re.compile(
    r"(?i)^(?:that's\s+all|thats\s+all|done|finished|finish|stop|"
    r"end(?:\s+list)?|no\s+more|complete|list\s+done)\s*[!.]*$"
)

# Phase B: check-off / bought / remove
LIST_CHECK_RE = re.compile(
    r"(?i)^(?:(?:check(?:\s*-?\s*off)?|tick|mark|got|bought|picked\s*up|"
    r"crossed?\s*off|done\s+with)\s+)(.+?)\s*$"
    r"|^(?:mark\s+)?(.+?)\s+(?:as\s+)?(?:bought|done|checked|got)\s*$"
)

LIST_REMOVE_RE = re.compile(
    r"(?i)^(?:(?:remove|delete|drop|strike)\s+)(.+?)(?:\s+from\s+(?:the\s+|my\s+)?list)?\s*$"
)

LIST_RENAME_RE = re.compile(
    r"(?i)^(?:"
    r"call\s+(?:it|this|the\s+list)\s+(.+)"
    r"|rename\s+(?:(?:the\s+|my\s+)?list\s+)?(?:to\s+|as\s+)?(.+)"
    r"|(?:title|name)\s+(?:(?:the\s+|my\s+)?list\s+)?(?:to\s+|as\s+)?(.+)"
    r")\s*$"
)

# "make a groceries list" / "make a list called Groceries" / "shopping list"
_TITLE_FROM_MAKE_RE = re.compile(
    r"(?i)(?:make|create|start|new|begin|open)\s+(?:(?:a|me|us|my)\s+)*"
    r"(?:list\s+(?:called|named|titled)\s+([A-Za-z][\w\s'-]{0,40})"
    r"|(?:(shopping|grocery|groceries|todo|to-?do)\s+)?list"
    r"|list\s+for\s+([A-Za-z][\w\s'-]{0,40}))"
    r"|(?:^|\b)(shopping|grocery|groceries|todo|to-?do)\s+list\b"
)

# Item lines: "Condensed milk x4", "4x milk", "milk × 2", "eggs * 6"
_ITEM_QTY_RE = re.compile(
    r"(?i)^\s*(?:(\d+)\s*[x×*]\s+(.+)|(.+?)\s*[x×*]\s*(\d+))\s*$"
)

_OPS_RE = re.compile(
    r"(?i)\b(?:deploy|ssh|server|vps|docker|container|restart|"
    r"nginx|systemd|disk\s*space|uptime|cron)\b"
)

_GREETING_RE = re.compile(
    r"(?i)^(hey|hi|hello|yo|sup|good\s+(?:morning|afternoon|evening)|howdy)[\s!.]*$"
)

_CLARIFY_RE = re.compile(r"(?i)^(what|huh|hmm+|idk|dunno|\?+)\s*$")

_MEMORY_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"remember(?:\s+that)?|"
    r"forget(?:\s+that)?|"
    r"correct(?:\s+that)?|"
    r"note\s+that|keep\s+in\s+mind(?:\s+that)?|"
    r"what\s+do\s+you\s+know\s+about\s+me\??|"
    r"what\s+do\s+you\s+remember(?:\s+about\s+me)?\??|"
    r"/memory"
    r")"
)

_NOTE_INTENT_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"note\s*:|"
    r"jot(?:\s+down)?\s*[:\-]?|"
    r"save\s+this\s*[:\-]?|"
    r"save\s+note\s*[:\-]?|"
    r"quick\s+note\s*[:\-]?|"
    r"(?:what\s+)?notes?(?:\s+(?:about|on|for|re)\b)?|"
    r"recall(?:\s+notes?)?|"
    r"show\s+notes?|"
    r"/notes?"
    r")"
)


def looks_like_note_intent(text: str) -> bool:
    return bool(_NOTE_INTENT_RE.match((text or "").strip()))



_TITLE_ALIASES = {
    "shopping": "Shopping",
    "grocery": "Groceries",
    "groceries": "Groceries",
    "todo": "Todo",
    "to-do": "Todo",
    "to do": "Todo",
}


def extract_list_title(text: str) -> Optional[str]:
    """Pull an optional title from a make-list utterance."""
    t = (text or "").strip()
    if not t:
        return None
    m = _TITLE_FROM_MAKE_RE.search(t)
    if not m:
        return None
    for g in m.groups():
        if not g:
            continue
        key = g.strip().lower()
        if key in _TITLE_ALIASES:
            return _TITLE_ALIASES[key]
        title = re.sub(r"[.!,]+$", "", g.strip())
        title = re.sub(r"(?i)\s+(?:please|and|with|:).*$", "", title).strip()
        if title and title.lower() not in {"a", "the", "my", "list"}:
            return title[:40].strip().title() if title.islower() else title[:40].strip()
    return None


def looks_like_list_intent(
    text: str,
    *,
    has_active_list: bool = False,
    is_collecting: bool = False,
) -> bool:
    """True when this turn should take the list artifact path."""
    t = (text or "").strip()
    if not t:
        return False
    if looks_like_finance(t) or parse_finance(t) is not None:
        return False
    if LIST_MAKE_RE.search(t) or LIST_SHOW_RE.search(t) or LIST_ADD_RE.search(t):
        return True
    if LIST_DONE_RE.match(t) and (has_active_list or is_collecting):
        return True
    if (has_active_list or is_collecting) and (
        LIST_CHECK_RE.match(t) or LIST_REMOVE_RE.match(t) or LIST_RENAME_RE.match(t)
    ):
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if any(LIST_MAKE_RE.search(ln) for ln in lines):
        return True
    if (has_active_list or is_collecting) and _looks_like_item_lines(t):
        return True
    return False


def _looks_like_item_lines(text: str) -> bool:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    return all(_ITEM_QTY_RE.match(ln) or _loose_item_line(ln) for ln in lines)


def _loose_item_line(line: str) -> bool:
    if len(line) > 80:
        return False
    if looks_like_finance(line) or _OPS_RE.search(line) or _GREETING_RE.match(line):
        return False
    if LIST_MAKE_RE.search(line) or LIST_SHOW_RE.search(line):
        return False
    if LIST_DONE_RE.match(line) or LIST_CHECK_RE.match(line) or LIST_REMOVE_RE.match(line):
        return False
    if "?" in line:
        return False
    words = line.split()
    return 1 <= len(words) <= 8


def parse_item_line(line: str) -> tuple[str, int] | None:
    """Return (name, qty) or None."""
    m = _ITEM_QTY_RE.match((line or "").strip())
    if not m:
        return None
    if m.group(1) and m.group(2):
        return m.group(2).strip(), int(m.group(1))
    if m.group(3) and m.group(4):
        return m.group(3).strip(), int(m.group(4))
    return None


def parse_check_query(text: str) -> Optional[str]:
    m = LIST_CHECK_RE.match((text or "").strip())
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def parse_remove_query(text: str) -> Optional[str]:
    m = LIST_REMOVE_RE.match((text or "").strip())
    if not m:
        return None
    return (m.group(1) or "").strip() or None


def parse_rename_title(text: str) -> Optional[str]:
    m = LIST_RENAME_RE.match((text or "").strip())
    if not m:
        return None
    for g in m.groups():
        if g and g.strip():
            title = g.strip().strip("\"'")
            title = re.sub(r"[.!,]+$", "", title).strip()
            if title:
                return title[:40]
    return None


def classify_intent(
    text: str,
    *,
    has_active_list: bool = False,
    is_collecting: bool = False,
) -> Intent:
    """Classify turn intent. Finance wins over list; list over ops/chat."""
    t = (text or "").strip()
    if not t:
        return "clarify"

    if looks_like_finance(t) or parse_finance(t) is not None:
        return "finance"

    if looks_like_list_intent(
        t, has_active_list=has_active_list, is_collecting=is_collecting
    ):
        return "list"

    if _CLARIFY_RE.match(t):
        return "clarify"

    if _GREETING_RE.match(t):
        return "chat"

    # Reminders / dated tasks (slice 2) — after list so shopping collecting wins
    if re.search(r"(?i)\bremind(?:\s+me|er)?\b", t) or t.strip().lower() in ("/reminders", "/reminder"):
        return "reminder"
    if re.search(
        r"(?i)^(?:todo|to-do|task)[:\s]|^add\s+(?:a\s+)?(?:task|todo)\b|"
        r"\b(?:list|show|my)\s+(?:tasks?|todos?)\b|^/(?:tasks?|todos?)\s*$|"
        r"\bdue\b.+(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|in\s+\d+)",
        t,
    ):
        return "task"

    # Calendar / notes (slice 4) — after tasks/reminders, before memory
    if looks_like_calendar(t) or is_agenda_query(t):
        return "calendar"
    if looks_like_note_intent(t):
        return "note"

    if _MEMORY_RE.search(t):
        return "memory"

    if _OPS_RE.search(t):
        return "ops"

    if len(t.split()) <= 2 and not re.search(r"[a-zA-Z]{4,}", t):
        return "clarify"

    return "chat"
