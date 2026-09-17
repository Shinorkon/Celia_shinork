"""Heuristic NL parser for finance intents (regex only — no LLM)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedFinance:
    amount_mvr: float
    tx_type: str  # expense | income
    category_hint: str
    merchant: str
    note: str
    raw: str


# Word/phrase → category name (matched case-insensitive)
_CATEGORY_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(grocer(?:y|ies)|food|eat(?:ing)?|lunch|dinner|breakfast|coffee|meal|snack|restaurant|cafe)\b", re.I), "Food"),
    (re.compile(r"\b(transport|taxi|bus|ferry|fuel|petrol|gas|uber|bolt|ride|parking)\b", re.I), "Transport"),
    (re.compile(r"\b(rent|rental|lease)\b", re.I), "Rent"),
    (re.compile(r"\b(utilit(?:y|ies)|electric(?:ity)?|water|wifi|internet|bill|phone)\b", re.I), "Utilities"),
    (re.compile(r"\b(health|clinic|doctor|pharmacy|medicine|medical|hospital)\b", re.I), "Health"),
    (re.compile(r"\b(shop(?:ping)?|clothes|amazon|mall)\b", re.I), "Shopping"),
    (re.compile(r"\b(entertain(?:ment)?|movie|cinema|game|fun|netflix)\b", re.I), "Entertainment"),
    (re.compile(r"\b(salary|paycheck|wage|wages|income)\b", re.I), "Salary"),
]

# Pure category labels — strip these from leftovers; item words like coffee stay as merchant
_PURE_CATEGORY_WORDS = {
    "food",
    "groceries",
    "grocery",
    "transport",
    "rent",
    "rental",
    "lease",
    "utilities",
    "utility",
    "health",
    "shopping",
    "entertainment",
    "salary",
    "other",
    "eating",
    "expense",
    "income",
}

_INCOME_RE = re.compile(
    r"\b(income|earned|received|got paid|salary|paycheck|wage)\b", re.I
)
_EXPENSE_RE = re.compile(
    r"\b(spent|paid|pay|bought|buy|expense|cost|costs)\b", re.I
)

# Amount patterns — prefer explicit currency / keywords near number
_AMOUNT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:spent|paid|pay|bought|buy|income|earned|received)\s+"
        r"(?:rf\.?\s*|mvr\s*)?(\d+(?:[.,]\d{1,2})?)",
        re.I,
    ),
    re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:mvr|rf\.?)\b", re.I),
    re.compile(r"(?:mvr|rf\.?)\s*(\d+(?:[.,]\d{1,2})?)", re.I),
    re.compile(r"(?:^|\s)(\d+(?:[.,]\d{1,2})?)(?:\s|$)"),
]

_AT_MERCHANT = re.compile(
    r"\bat\s+([A-Za-z0-9][\w\s&'.-]{0,40}?)(?:\s+(?:for|on|—|-)\s+|$)",
    re.I,
)
_ON_NOTE = re.compile(
    r"\b(?:on|for)\s+([A-Za-z][\w\s&'.-]{0,40}?)(?:\s+at\s+|$)",
    re.I,
)

# Slash /log-style: /spent 85 food agora  OR plain finance NL
_SLASH_FINANCE = re.compile(
    r"^/(?:spent|log|income)\s+(\d+(?:[.,]\d{1,2})?)(?:\s+(.+))?$",
    re.I,
)

_STRIP_FOR_LEFTOVER = re.compile(
    r"""
    \d+(?:[.,]\d{1,2})?                         # amounts
    | \b(?:mvr|rf\.?)\b                           # currency
    | \b(?:spent|paid|pay|bought|buy|expense|cost|costs|
          income|earned|received|got\s+paid|salary|paycheck|wage|wages)\b
    | \b(?:at|on|for|from|with|about|a|an|the|my|some|of|to|in|and|or)\b
    """,
    re.I | re.VERBOSE,
)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def _guess_category(text: str) -> str:
    for pat, name in _CATEGORY_ALIASES:
        if pat.search(text):
            return name
    return "Other"


def _guess_tx_type(text: str, category_hint: str) -> str:
    if _INCOME_RE.search(text) or category_hint.lower() == "salary":
        # "spent" / "paid" wins over salary word if both present in expense phrasing
        if _EXPENSE_RE.search(text) and not re.search(
            r"\b(income|earned|received|got paid)\b", text, re.I
        ):
            return "expense"
        return "income"
    if _EXPENSE_RE.search(text):
        return "expense"
    # Default expense for amounts without clear income cue
    return "expense"


def _extract_amount(text: str) -> Optional[float]:
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                val = _to_float(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                continue
    return None


def _promote_leftovers(text: str, merchant: str, note: str) -> tuple[str, str]:
    """After amount/currency/verbs/prepositions, leftover tokens → merchant or note.

    Place-like leftovers (capitalized) become merchant; item words like 'coffee'
    become note. Pure category words like 'food' are dropped.
    """
    if merchant or note:
        return merchant, note

    scrubbed = _STRIP_FOR_LEFTOVER.sub(" ", text)
    tokens: list[str] = []
    for tok in scrubbed.split():
        clean = tok.strip(" .,!-")
        if not clean:
            continue
        if clean.lower() in _PURE_CATEGORY_WORDS:
            continue
        tokens.append(clean)

    if not tokens:
        return merchant, note

    leftover = " ".join(tokens)
    words = leftover.split()
    # Capitalized / Title Case → merchant ("at Agora"); else note ("for coffee")
    place_like = any(w[:1].isupper() for w in words if w)
    if place_like and len(words) <= 3:
        return leftover, note
    return merchant, leftover


def _extract_merchant_note(text: str, category_hint: str) -> tuple[str, str]:
    merchant = ""
    note = ""
    at_m = _AT_MERCHANT.search(text)
    if at_m:
        merchant = at_m.group(1).strip(" .,!-")
    on_m = _ON_NOTE.search(text)
    if on_m:
        note = on_m.group(1).strip(" .,!-")
        # Don't treat category name alone as note
        if note.lower() in {
            "food",
            "groceries",
            "grocery",
            "rent",
            "transport",
            "utilities",
            "health",
            "shopping",
            "entertainment",
            "salary",
            "other",
        }:
            if not category_hint or category_hint == "Other":
                pass
            else:
                # keep as light note only if different from category
                if note.lower() == category_hint.lower():
                    note = ""

    merchant, note = _promote_leftovers(text, merchant, note)
    return merchant, note


def looks_like_finance(text: str) -> bool:
    """True if text is a finance slash command or NL that parses to a transaction."""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if low.startswith(("/spent", "/budget", "/finance", "/log", "/income")):
        # /spent alone or with period args handled elsewhere; /spent with amount is parseable
        if low.startswith("/budget") or low.startswith("/finance"):
            return True
        if re.match(r"^/spent\s*(today|week|month)?\s*$", low):
            return True
        if low.startswith("/log") and "help" in low:
            return True
    return parse_finance(t) is not None


def parse_finance(text: str) -> Optional[ParsedFinance]:
    """Parse free-text or slash amount forms into a finance intent.

    Returns None if no positive amount found.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # /spent 85 food agora  |  /income 5000 salary
    slash = _SLASH_FINANCE.match(raw)
    if slash:
        amount = _to_float(slash.group(1))
        rest = (slash.group(2) or "").strip()
        cmd = raw.split()[0].lower()
        tx_type = "income" if cmd == "/income" else "expense"
        category_hint = _guess_category(rest) if rest else "Other"
        if cmd == "/income" and category_hint == "Other":
            category_hint = "Salary"
        merchant = ""
        note = ""
        if rest:
            parts = rest.split()
            # first token often category; remainder merchant/note
            cat_names = {
                "food",
                "transport",
                "rent",
                "utilities",
                "health",
                "shopping",
                "entertainment",
                "other",
                "salary",
                "groceries",
            }
            if parts and parts[0].lower() in cat_names:
                category_hint = _guess_category(parts[0])
                leftover = " ".join(parts[1:]).strip()
            else:
                leftover = rest
                category_hint = _guess_category(rest) if category_hint == "Other" else category_hint
            if leftover:
                # prefer merchant if looks like a place name (capitalized / single token)
                merchant = leftover
                note = leftover
        return ParsedFinance(
            amount_mvr=amount,
            tx_type=tx_type,
            category_hint=category_hint,
            merchant=merchant,
            note=note if note != merchant else "",
            raw=raw,
        )

    # Skip pure summary commands without amounts
    if re.match(r"^/(spent|budget|finance|log)(\s|$)", raw, re.I):
        return None

    amount = _extract_amount(raw)
    if amount is None:
        return None

    # Avoid hijacking unrelated messages that merely contain a number
    # unless they have finance cues or currency markers
    has_cue = bool(
        _EXPENSE_RE.search(raw)
        or _INCOME_RE.search(raw)
        or re.search(r"\b(mvr|rf\.?)\b", raw, re.I)
        or any(p.search(raw) for p, _ in _CATEGORY_ALIASES)
    )
    if not has_cue:
        return None

    category_hint = _guess_category(raw)
    tx_type = _guess_tx_type(raw, category_hint)
    merchant, note = _extract_merchant_note(raw, category_hint)

    return ParsedFinance(
        amount_mvr=amount,
        tx_type=tx_type,
        category_hint=category_hint,
        merchant=merchant,
        note=note,
        raw=raw,
    )
