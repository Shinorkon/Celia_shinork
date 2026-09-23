"""Quiet mode for general chat / completions outbox (Phase C).

Strip unsolicited capability / health / Shnuk / Budgy / VPS / Directors Eye
narration from the completions path. Drop ✅ mood openers on chat entirely
(reserve status glyphs only for hard failures — ❌ — if at all).

List and finance handlers have their own strip helpers and stay untouched.
"""
from __future__ import annotations

import re

# Leading mood / status glyphs the outbox used to prepend.
_OPENER_RE = re.compile(
    r"^[\u2705\u274c\u2139\ufe0f✅❌ℹ️]+\s*"
)

# Unsolicited infra / stack / capability narration (sentence-level).
_BANNED_SENTENCE_RE = re.compile(
    r"(?i)\b(?:"
    r"shnuk|budgy|directors?\s*eye|"
    r"frontdesk|ops\s*team|as an ai|"
    r"i can (?:definitely )?(?:help|check|list|do)|"
    r"here'?s what i can|"
    r"capability|"
    r"humanised agent|smart sidekick|"
    r"i'?m here (?:for you|to help)|"
    r"(?:on\s+)?(?:the\s+)?vps\b|"
    r"disk\s*space|container\s+status|health\s+check|"
    r"/opt\b|/root/Celia|/home/shino|"
    r"agent_orchestration_platform"
    r")\b"
)

_BULLET_MENU_RE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+[.)])\s+.+(?:check|list|deploy|server|vps|help).*$"
)

_CHAT_ROLES = frozenset({
    "frontoffice",
    "comms",
    "planner",
    "document",
    "qa",
    "scheduler",
    "memory-writer",
    "",
})


def strip_status_opener(text: str) -> str:
    """Remove leading ✅/❌/ℹ️ mood openers."""
    return _OPENER_RE.sub("", (text or "").strip())


def quiet_strip_chat(text: str) -> str:
    """Strip unsolicited capability/health/infra narration from chat text.

    Keeps the first useful sentence(s) that do not volunteer banned topics.
    If everything is banned, returns empty (caller may skip send).
    """
    s = strip_status_opener(text)
    if not s:
        return ""

    # Drop bullet capability menus entirely.
    s = _BULLET_MENU_RE.sub("", s)

    # Split on sentence / paragraph boundaries.
    parts = re.split(r"(?<=[.!?])\s+|\n+", s)
    kept: list[str] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if _BANNED_SENTENCE_RE.search(p):
            continue
        kept.append(p)

    out = " ".join(kept).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def quiet_strip_completion(
    text: str,
    *,
    agent_role: str = "",
    status: str = "",
) -> str:
    """Apply quiet strip for conversational completions.

    Executor/coder action results are sanitized lightly (opener only) so real
    command summaries still land. Chat roles get the full quiet strip.
    """
    role = (agent_role or "").lower()
    s = strip_status_opener(text)
    if role in _CHAT_ROLES or role not in {
        "executor", "coder", "ops-monitor", "ops-reflect",
    }:
        s = quiet_strip_chat(s)
    return s


def completion_prefix(agent_role: str, status: str) -> str:
    """Outbox prefix policy (Phase C): no ✅ on chat; ❌ only on hard fail.

    Prefer dropping ✅ entirely — including completed actions. Status glyphs
    were mood openers; quiet mode kills them on the completions path.
    """
    if status == "failed":
        return "❌ "
    return ""
