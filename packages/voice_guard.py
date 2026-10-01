"""Last-line guard for cramped chatbot replies.

The model should not emit these. If it does, replace the whole reply so a
message-relay ask never ships as a capability menu, and a canned greeting
does not erase a real ask once the chat already has context.
"""
from __future__ import annotations

import re

_CAPABILITY_REFUSAL_RE = re.compile(
    r"(?i)("
    r"capabilities are|"
    r"my capabilities|"
    r"conversational side|"
    r"only conversational|"
    r"more on the conversational|"
    r"discussing ideas, giving advice|"
    r"i can (?:only |just )?(?:chat|talk)(?:\b|,)"
    r")"
)

_CANNED_GREETING_RE = re.compile(
    r"(?i)^\s*(?:"
    r"hey(?: there)?(?:!|\.|,)?\s*(?:what'?s up\??)?"
    r"|hi(?: there)?[!.]*"
    r"|hello[!.]*"
    r"|yo[!.]*"
    r"|what'?s up\??"
    r")\s*$"
)

_RELAY_ASK_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:pass(?:\s+on)?|relay|forward)\b.{0,80}\b(?:message|note|text)\b"
    r"|\b(?:send|text|dm)\b.{0,40}\b(?:message|note)\b.{0,40}\bto\b"
    r"|\breply\s+to\s+(?:the\s+)?(?:guy|girl|person|him|her|them)\b"
    r")"
)

RELAY_NEXT_STEP = (
    "I can pass that on. Who should get it, and what should I say? "
    "If I don't already have their Telegram id, send it and I'll confirm before it goes."
)

CONTEXT_NEXT_STEP = (
    "Say what you want done and I'll do it. "
    "If it's a message for someone else, tell me who and the words — "
    "I'll ask for a Telegram id if I don't have one, then confirm before sending."
)


def is_capability_refusal(text: str) -> bool:
    return bool(_CAPABILITY_REFUSAL_RE.search(text or ""))


def is_canned_greeting(text: str) -> bool:
    return bool(_CANNED_GREETING_RE.match((text or "").strip()))


def looks_like_relay_ask(text: str) -> bool:
    return bool(_RELAY_ASK_RE.search(text or ""))


def guard_reply(
    text: str,
    *,
    user_text: str = "",
    has_context: bool = False,
) -> str:
    """Rewrite cramped refusals and context-blind canned greetings."""
    raw = text or ""
    if is_capability_refusal(raw):
        return RELAY_NEXT_STEP if looks_like_relay_ask(user_text) or not (user_text or "").strip() else (
            "I can do that — tell me the missing piece and I'll take the next step. "
            "If it's a message for someone, I need who and the words, then I'll confirm before sending."
        )
    if (
        has_context
        and is_canned_greeting(raw)
        and not is_canned_greeting(user_text)
        and len((user_text or "").split()) >= 3
    ):
        if looks_like_relay_ask(user_text):
            return RELAY_NEXT_STEP
        return CONTEXT_NEXT_STEP
    return raw
