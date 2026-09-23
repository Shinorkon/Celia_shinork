"""Intent router ahead of frontoffice (Phase A).

Classifies a (possibly debounce-merged) turn into:
  list | finance | ops | chat | clarify

Finance heuristics stay authoritative — this module imports looks_like_finance
as reference and never steals receipt/spend paths.
"""
from __future__ import annotations

import re
from typing import Literal

from app.finance_parse import looks_like_finance, parse_finance

Intent = Literal["list", "finance", "ops", "chat", "clarify"]

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
)

LIST_ADD_RE = re.compile(
    r"(?i)\b(?:add|put|append)\b.+\b(?:to|on)\s+(?:the\s+|my\s+)?list\b"
    r"|\badd\s+to\s+list\b"
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


def looks_like_list_intent(text: str, *, has_active_list: bool = False) -> bool:
    """True when this turn should take the list artifact path."""
    t = (text or "").strip()
    if not t:
        return False
    if looks_like_finance(t) or parse_finance(t) is not None:
        return False
    if LIST_MAKE_RE.search(t) or LIST_SHOW_RE.search(t) or LIST_ADD_RE.search(t):
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if any(LIST_MAKE_RE.search(ln) for ln in lines):
        return True
    if has_active_list and _looks_like_item_lines(t):
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


def classify_intent(text: str, *, has_active_list: bool = False) -> Intent:
    """Classify turn intent. Finance wins over list; list over ops/chat."""
    t = (text or "").strip()
    if not t:
        return "clarify"

    if looks_like_finance(t) or parse_finance(t) is not None:
        return "finance"

    if looks_like_list_intent(t, has_active_list=has_active_list):
        return "list"

    if _CLARIFY_RE.match(t):
        return "clarify"

    if _GREETING_RE.match(t):
        return "chat"

    if _OPS_RE.search(t):
        return "ops"

    if len(t.split()) <= 2 and not re.search(r"[a-zA-Z]{4,}", t):
        return "clarify"

    return "chat"
