"""Intent router ahead of frontoffice (Phase A + B).

Classifies a (possibly debounce-merged) turn into:
  list | finance | ops | memory | task | reminder | chat | clarify

Finance heuristics stay authoritative — this module imports looks_like_finance
as reference and never steals receipt/spend paths.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from app.finance_parse import (
    looks_like_amount_preference_rule,
    looks_like_finance,
    looks_like_receipt_recalculate,
    parse_finance,
)
from app.calendar_parse import looks_like_calendar, is_agenda_query
from app.reminder_parse import has_remind_verb, looks_like_reminder, looks_like_task

Intent = Literal["list", "finance", "ops", "memory", "task", "reminder", "calendar", "note", "life", "chat", "clarify", "help"]

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
    r"(?i)^(?:(?:remove|delete|drop|strike)\s+)(?!everything\b|all\b)(.+?)(?:\s+from\s+(?:the\s+|my\s+)?list)?\s*$"
)

# Bulk wipe list (before item-remove)
LIST_CLEAR_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"(?:clear|wipe|empty|reset)\s+(?:(?:the\s+|my\s+)?(?:shopping\s+|grocery\s+)?)?list\b"
    r"|(?:clear|wipe|empty|remove|delete)\s+(?:everything|all(?:\s+items)?)\s+(?:from\s+)?(?:(?:the\s+|my\s+)?(?:shopping\s+|grocery\s+)?)?list\b"
    r"|list\s+(?:clear|wipe|empty|reset)\b"
    r"|/?clear[_-]?list"
    r")\s*[!.]*$"
)

# Bulk forget / wipe memory
MEMORY_CLEAR_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"(?:forget|clear|wipe|erase|delete|remove)\s+(?:everything|all)\s+(?:from\s+)?(?:(?:your\s+|the\s+|my\s+)?)?memory\b"
    r"|(?:clear|wipe|erase|reset)\s+(?:(?:your\s+|the\s+|my\s+)?)?memory\b"
    r"|(?:forget|clear)\s+all(?:\s+(?:memories|memory))?\b"
    r"|/?clear[_-]?memory"
    r")\s*[!.]*$"
)

# Combined memory+list wipe (handle both in one turn)
BULK_CLEAR_BOTH_RE = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"(?:remove|clear|wipe|delete|forget|erase)\s+everything\s+from\s+memory\s+and\s+(?:(?:the\s+|my\s+)?)?list"
    r"|(?:clear|wipe|reset)\s+(?:memory\s+and\s+list|list\s+and\s+memory)"
    r"|(?:remove|clear|wipe)\s+everything\s+from\s+(?:(?:the\s+|my\s+)?)?list\s+and\s+memory"
    r").*?$"
)

# Slash commands /start /help — never list items
SLASH_CMD_RE = re.compile(r"(?i)^/[a-z][a-z0-9_]*(?:@[\w]+)?(?:\s|$)")
START_HELP_RE = re.compile(r"(?i)^/(?:start|help)(?:@[\w]+)?\s*$")

# Action-ish text for life agent (when not finance/list-collect/pending)
LIFE_ACTION_RE = re.compile(
    r"(?i)\b(?:"
    r"remind(?:ers?|r)?|remaind|remnd|"
    r"(?:add\s+)?(?:a\s+)?(?:task|todo|to-do)\b|"
    r"\b(?:list|show|my)\s+(?:tasks?|todos?|reminders?)\b|"
    r"cancel\s+reminder|snooze\b|"
    r"what(?:'s|\s+is)\s+due\b"
    r")"
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




def is_slash_command(text: str) -> bool:
    return bool(SLASH_CMD_RE.match((text or "").strip()))


def is_start_or_help(text: str) -> bool:
    return bool(START_HELP_RE.match((text or "").strip()))


def looks_like_list_clear(text: str) -> bool:
    t = (text or "").strip()
    return bool(LIST_CLEAR_RE.match(t) or BULK_CLEAR_BOTH_RE.match(t))


def looks_like_memory_clear(text: str) -> bool:
    t = (text or "").strip()
    return bool(MEMORY_CLEAR_RE.match(t) or BULK_CLEAR_BOTH_RE.match(t))


def looks_like_bulk_clear_both(text: str) -> bool:
    return bool(BULK_CLEAR_BOTH_RE.match((text or "").strip()))


def looks_like_life_action(text: str) -> bool:
    t = (text or "").strip()
    if not t or is_slash_command(t):
        return False
    return bool(LIFE_ACTION_RE.search(t)) or is_compound_life_request(t)


def life_domain_flags(text: str) -> set[str]:
    """Which life domains a turn touches (for compound → life agent routing)."""
    t = (text or "").strip()
    if not t:
        return set()
    flags: set[str] = set()
    low = t.lower()
    # list
    if (
        LIST_MAKE_RE.search(t)
        or LIST_SHOW_RE.search(t)
        or LIST_ADD_RE.search(t)
        or LIST_CLEAR_RE.match(t)
        or re.search(r"(?i)\b(?:shopping|grocery|groceries)\s+list\b|\badd\b.+\bto\s+(?:the\s+|my\s+)?list\b", t)
        or re.search(r"(?i)\b(?:milk|eggs|bread)\b.+(?:list|remind)|\blist\b.+(?:milk|eggs|bread)", t)
    ):
        flags.add("list")
    # reminder / task
    if LIFE_ACTION_RE.search(t) or has_remind_verb(t) or looks_like_reminder(t) or looks_like_task(t):
        flags.add("reminder_task")
    # calendar
    if looks_like_calendar(t) or is_agenda_query(t) or re.search(
        r"(?i)\b(?:calendar|agenda|schedule\s+(?:a|an|me)|book\s+(?:a|an)|dentist|appointment)\b",
        t,
    ):
        flags.add("calendar")
    # notes
    if looks_like_note_intent(t) or re.search(r"(?i)\b(?:jot|quick\s+note|save\s+note)\b", t):
        flags.add("note")
    # memory (remember/forget/correct/know) — not bulk-wipe alone
    if (
        _MEMORY_RE.search(t)
        or re.search(r"(?i)\b(?:remember(?:\s+that)?|forget(?:\s+that)?|correct(?:\s+that)?)\b", t)
        or re.search(r"(?i)what\s+do\s+you\s+(?:know|remember)\b", t)
    ) and not looks_like_memory_clear(t) and not looks_like_bulk_clear_both(t):
        flags.add("memory")
    # finance session prefs / recalc (compound with life domains; finance-only still fast-path)
    try:
        from app.finance_parse import (
            looks_like_amount_preference_rule,
            looks_like_receipt_recalculate,
        )
        if looks_like_amount_preference_rule(t) or looks_like_receipt_recalculate(t):
            flags.add("finance_session")
    except Exception:
        if re.search(r"(?i)\blower\b.+\b(?:amount|text)\b|\brecalculat", t):
            flags.add("finance_session")
    return flags


def is_compound_life_request(text: str) -> bool:
    """True when two+ life domains appear — route to life agent tool loop."""
    return len(life_domain_flags(text)) >= 2


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
    # Slash commands and memory wipes never become list items.
    if is_slash_command(t) or looks_like_memory_clear(t) or looks_like_bulk_clear_both(t):
        return False
    # Explicit list clear/wipe/empty is a list intent (handled by list_handlers).
    if LIST_CLEAR_RE.match(t):
        return True
    # Finance receipt prefs / recalc must never become shopping-list items.
    if (
        looks_like_finance(t)
        or looks_like_receipt_recalculate(t)
        or looks_like_amount_preference_rule(t)
        or parse_finance(t) is not None
    ):
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
    if is_slash_command(line):
        return False
    if looks_like_list_clear(line) or looks_like_memory_clear(line):
        return False
    if (
        looks_like_finance(line)
        or looks_like_receipt_recalculate(line)
        or looks_like_amount_preference_rule(line)
        or _OPS_RE.search(line)
        or _GREETING_RE.match(line)
    ):
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

    # /start /help — never shopping list
    if is_start_or_help(t):
        return "help"

    if looks_like_finance(t) or parse_finance(t) is not None:
        return "finance"

    # Bulk clear memory/list BEFORE list item-remove / append
    if looks_like_bulk_clear_both(t):
        return "memory"  # handler clears both
    if looks_like_memory_clear(t):
        return "memory"
    if looks_like_list_clear(t):
        return "list"

    # Multi-domain life ("add milk to the list and remind me at 5") → life agent
    if is_compound_life_request(t):
        return "life"

    if looks_like_list_intent(
        t, has_active_list=has_active_list, is_collecting=is_collecting
    ):
        return "list"

    if _CLARIFY_RE.match(t):
        return "clarify"

    # Reminders / dated tasks (slice 2) — BEFORE pure greeting so
    # "Hey man Remaind me…" does not fall through to AOP chat.
    if looks_like_reminder(t) or t.strip().lower() in ("/reminders", "/reminder") or has_remind_verb(t):
        return "reminder"
    if looks_like_task(t) or re.search(
        r"(?i)^(?:todo|to-do|task)[:\s]|^add\s+(?:a\s+)?(?:task|todo)\b|"
        r"\b(?:list|show|my)\s+(?:tasks?|todos?)\b|^/(?:tasks?|todos?)\s*$|"
        r"\bdue\b.+(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|in\s+\d+)",
        t,
    ):
        return "task"

    if _GREETING_RE.match(t):
        return "chat"

    # Calendar / notes (slice 4) — after tasks/reminders, before memory
    if looks_like_calendar(t) or is_agenda_query(t):
        return "calendar"
    if looks_like_note_intent(t):
        return "note"

    if _MEMORY_RE.search(t) or looks_like_memory_clear(t):
        return "memory"

    if _OPS_RE.search(t):
        return "ops"

    # Action-ish life text → life agent (not bare chat greeting)
    if looks_like_life_action(t):
        return "life"

    if is_slash_command(t):
        return "help"

    if len(t.split()) <= 2 and not re.search(r"[a-zA-Z]{4,}", t):
        return "clarify"

    return "chat"
