"""Stop cramped chatbot refusals from reaching Telegram.

Ordinary help requests must not ship as "I'm only conversational" scripts
or a canned greeting when the person already asked for something.
"""
from __future__ import annotations

import re

# Pass / relay / "reply to the other person" — not "message me" or "text me".
_RELAY_RE = re.compile(
    r"(?i)(?:"
    r"\bpass(?:\s+on)?\s+(?:a\s+)?(?:message|note)\b"
    r"|\brelay\s+(?:a\s+)?(?:message|this|that)\b"
    r"|\bsend\s+(?:a\s+)?message\s+to\b"
    r"|\bpass\s+(?:this|that|it)\s+(?:on\s+)?to\b"
    r"|\brepl(?:y|ies)\s+to\s+(?:the\s+)?(?:guy|girl|person|dude|him|her|them)\b"
    r"|\bwho(?:'s|s|\s+is)\s+texting\b"
    r"|\btexting\s+(?:u|you)\b"
    r")"
)

_CRAMPED_RE = re.compile(
    r"(?i)(?:"
    r"conversational\s+side"
    r"|only\s+conversational"
    r"|my\s+capabilities"
    r"|capabilities\s+are\s+more"
    r"|i(?:'m| am)\s+(?:just|only)\s+(?:a\s+)?(?:chat\s*bot|conversational)"
    r"|more\s+on\s+the\s+conversational"
    r"|i\s+can(?:not|'t)\s+(?:really\s+)?(?:do|help).{0,80}(?:only|just)\s+(?:chat|talk)"
    r")"
)

_CANNED_GREETING_RE = re.compile(
    r"(?i)^(?:"
    r"(?:hey|hi|hello|hey there|yo)[!,.\s]*(?:what'?s up\??)?"
    r"|(?:what'?s up\??)"
    r")[!,.\s]*$"
)

_USER_GREETING_RE = re.compile(
    r"(?i)^(?:hey|hi|hello|yo|sup|howdy|good\s+(?:morning|afternoon|evening))"
    r"(?:\s+there)?[\s!.]*$"
)

_INFRA_SENTENCE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:"
    r"shnuk|budgy|oreuda|directors?[\s-]*eye|"
    r"frontdesk|ops\s*team|as an ai|"
    r"i can (?:definitely )?(?:help|check|list|do)|"
    r"here'?s what i can|"
    r"capabilit(?:y|ies)|"
    r"humanised agent|smart sidekick|"
    r"i'?m here (?:for you|to help)|"
    r"conversational\s+side|only\s+conversational|"
    r"(?:on\s+)?(?:the\s+)?vps|"
    r"disk\s*space|container\s+status|health\s+check|"
    r"agent_orchestration_platform"
    r")\b"
    r"|(?:/srv|/opt|/root/Celia|/home/shino)(?!\w)"
    r")"
)

_RELAY_NEXT = (
    "Yeah — I can pass that on. I need who it's for "
    "(name, and their Telegram id if I don't already have them) "
    "and the exact text. I'll check with you before it sends."
)

_GENERIC_NEXT = (
    "I can take that on. Tell me the specific piece — who, what, or when — "
    "and I'll do it or say exactly what's missing."
)

_CONTEXT_HELLO = "Hey — still here. What do you want to pick up?"


def looks_like_message_relay(text: str) -> bool:
    """True when the user wants a message passed to someone else."""
    return bool(_RELAY_RE.search(text or ""))


def _is_canned_greeting(text: str) -> bool:
    return bool(_CANNED_GREETING_RE.match((text or "").strip()))


def _is_bare_user_greeting(text: str) -> bool:
    return bool(_USER_GREETING_RE.match((text or "").strip()))


def _drop_infra_sentences(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    kept = [p.strip() for p in parts if p.strip() and not _INFRA_SENTENCE_RE.search(p)]
    return " ".join(kept).strip()


def _next_step(user_text: str) -> str:
    if looks_like_message_relay(user_text):
        return _RELAY_NEXT
    return _GENERIC_NEXT


def guard_cramped_reply(
    user_text: str,
    assistant_text: str,
    *,
    has_context: bool = False,
) -> str:
    """Replace capability-refusal scripts and context-blind canned greetings.

    A first hello with no history is left alone. Anything else that is only
    a greeting, or that claims she is conversational-only, becomes a concrete
    next step.
    """
    raw = (assistant_text or "").strip()
    if not raw:
        return raw
    user = user_text or ""
    if _CRAMPED_RE.search(raw):
        return _next_step(user)

    stripped = _drop_infra_sentences(raw)
    canned_source = stripped or raw
    if _is_canned_greeting(canned_source) or (not stripped and _INFRA_SENTENCE_RE.search(raw)):
        if _is_bare_user_greeting(user) and not has_context and not _INFRA_SENTENCE_RE.search(raw):
            return raw
        if _is_bare_user_greeting(user) and has_context:
            return _CONTEXT_HELLO
        return _next_step(user)

    if stripped and stripped != raw:
        return stripped
    return raw
