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


# Word/phrase → category name (matched case-insensitive; first match wins)
_CATEGORY_ALIASES: list[tuple[re.Pattern[str], str]] = [
    # Food — groceries + cafes (Agora/STO = supermarket)
    (re.compile(
        r"\b(grocer(?:y|ies)|food|foods|eat(?:ing)?|lunch|dinners?|breakfasts?|"
        r"coffees?|meals?|snacks?|restaurants?|cafes?|tea|juice|soda|"
        r"burgers?|pizzas?|noodles?|agora|sto)\b",
        re.I,
    ), "Food"),
    # Utilities before Transport so stealgas/fenaka/mwsc win over bare "gas"
    (re.compile(
        r"\b(utilit(?:y|ies)|electric(?:ity)?|powers?|waters?|wifis?|internets?|"
        r"phones?|bills?|stealgas|fenaka|mwsc|dhiraagu|ooredoo|"
        r"cooking\s*gas|gas\s*cylinder|bml\s*fees?|bank\s*fees?|atm\s*fees?|"
        r"recharges?|prepaid|postpaid|data\s*packs?|cable\s*tvs?)\b",
        re.I,
    ), "Utilities"),
    (re.compile(
        r"\b(transport|taxis?|cabs?|buses?|ferries|ferry|fuel|petrol|diesels?|"
        r"uber|grab|bolts?|rides?|fares?|parking|commutes?|"
        r"scooters?|motorbikes?|bikes?|boats?|seaplanes?|seaplane|"
        r"speedboats?|speed\s*boat|island\s*hoppers?|island\s*hopper|"
        r"flights?|airfares?|airport\s*transfers?)\b",
        re.I,
    ), "Transport"),
    (re.compile(
        r"\b(rent|rentals?|leases?|housing|apartments?|flats?|"
        r"landlords?|room\s*rents?)\b",
        re.I,
    ), "Rent"),
    (re.compile(
        r"\b(health|clinics?|doctors?|dentists?|dental|"
        r"pharmac(?:y|ies)|medicines?|medical|hospitals?|"
        r"vitamins?|check\s*-?ups?|prescriptions?|lab\s*tests?)\b",
        re.I,
    ), "Health"),
    (re.compile(
        r"\b(shop(?:ping)?|clothes?|shoes?|sneakers?|amazons?|malls?|fantasia|"
        r"apparel|outfits?|gadgets?|electronics?|gifts?|presents?|"
        r"daraz|shein|online\s*orders?)\b",
        re.I,
    ), "Shopping"),
    (re.compile(
        r"\b(entertain(?:ment)?|movies?|cinemas?|games?|gaming|fun|"
        r"netflix|spotify|youtube|disney\+?|subscriptions?|"
        r"concerts?|tickets?|steam|outings?|karaoke|bowling)\b",
        re.I,
    ), "Entertainment"),
    (re.compile(
        r"\b(salary|paycheck|wages?|income|bonus|bonuses|stipend|"
        r"payday|got\s+paid)\b",
        re.I,
    ), "Salary"),
]



# Pure category labels — strip these from leftovers; item words like coffee stay as merchant
_PURE_CATEGORY_WORDS = {
    "food",
    "foods",
    "groceries",
    "grocery",
    "transport",
    "rent",
    "rental",
    "rentals",
    "lease",
    "leases",
    "housing",
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
    "bonus",
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



# Receipt / spending-total asks (no amount) — keep on finance path, not chat LLM.
_RECEIPT_SPEND_ASK_RE = re.compile(
    r"(?:"
    r"(?:total|sum|add(?:\s*up)?|calculate|calc)\s+(?:my\s+)?(?:spending|spend|expenses?|receipts?)"
    r"|(?:spending|spend|expenses?)\s+(?:from\s+|with\s+)?(?:my\s+)?(?:receipts?|bills?)"
    r"|(?:if\s+i\s+)?(?:sent|send|sending)\s+(?:u\s+|you\s+)?(?:my\s+)?receipts?"
    r"|receipts?.{0,48}(?:total|sum|spending|add|calculate)"
    r"|(?:total|sum|calculate|calc).{0,48}receipts?"
    r"|\bread\s+(?:my\s+)?receipts?\b"
    r")",
    re.I,
)

_TOTAL_ONLY_RE = re.compile(
    r"(?:"
    r"\bjust\s+(?:need\s+)?(?:the\s+)?totals?\b"
    r"|\bonly\s+(?:the\s+)?totals?\b"
    r"|\btotals?\s+only\b"
    r"|\bi\s+just\s+need\s+the\s+totals?\b"
    r"|\bdon'?t\s+(?:need\s+)?(?:items?|itemize|line\s*items?|breakdown)\b"
    r"|\bno\s+(?:items?|itemize|breakdown|line\s*items?)\b"
    r"|\bscan\s+all(?:\s+of\s+(?:them|em|'?em))?\b"
    r"|\badd\s+(?:them|em|'?em|it)\s+up\b"
    r"|\bwhat'?s\s+the\s+totals?\b"
    r"|\bwhat'?s\s+the\s+sum\b"
    r"|\bgive\s+me\s+the\s+totals?\b"
    r"|\bhow\s+much\s+(?:in\s+)?totals?\b"
    r"|\bhow\s+much\s+(?:is\s+)?(?:that|it|all)\b"
    r"|^(?:just\s+)?(?:the\s+)?totals?\s*[.?]?$"
    r"|^(?:just\s+)?(?:the\s+)?sum\s*[.?]?$"
    r")",
    re.I,
)

# Explicit long-form list — only then dump merchant lines.
_BREAKDOWN_RE = re.compile(
    r"(?:"
    r"\b(?:show|list|give)\s+(?:me\s+)?(?:the\s+)?(?:full\s+)?(?:list|breakdown|itemi[sz]ation)\b"
    r"|\bbreak\s+(?:it|them|em|'?em)\s+down\b"
    r"|\bitemi[sz]e\b"
    r"|\bone\s+per\s+line\b"
    r"|\beach\s+receipt\b"
    r"|\bmerchant\s+list\b"
    r"|\bline\s+by\s+line\b"
    r")",
    re.I,
)

# Prefer lower separate text amount over receipt OCR when both present.
_AMOUNT_PREF_RULE_RE = re.compile(
    r"(?:"
    r"(?:use|count|take|prefer|keep)\s+(?:the\s+)?lower\s+(?:text\s+)?amount"
    r"|lower\s+(?:text\s+)?amount"
    r"|(?:text|typed|separate)\s+(?:with\s+a?\s*)?lower\s+amount"
    r"|if\s+(?:a\s+)?receipt\s+has\s+(?:an\s+)?amount.{0,80}lower"
    r"|count\s+the\s+lower\s+amount"
    r"|prefer\s+(?:the\s+)?(?:text|typed|lower)"
    r")",
    re.I,
)


def looks_like_receipt_spend_ask(text: str) -> bool:
    """True for 'calculate spending from receipts' style asks (no amount required)."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_RECEIPT_SPEND_ASK_RE.search(t))


def wants_total_only(text: str) -> bool:
    """True when user wants totals only — no itemization / vision essays."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_TOTAL_ONLY_RE.search(t))


def wants_breakdown(text: str) -> bool:
    """True when user explicitly asks for the long merchant list."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_BREAKDOWN_RE.search(t))


def looks_like_amount_preference_rule(text: str) -> bool:
    """True for 'if receipt amount vs lower text amount, use lower' style rules."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_AMOUNT_PREF_RULE_RE.search(t))


def looks_like_receipt_flow_text(text: str) -> bool:
    """Spending-from-receipts, total-only, breakdown, or amount-pref rules — stay in finance."""
    return (
        looks_like_receipt_spend_ask(text)
        or wants_total_only(text)
        or wants_breakdown(text)
        or looks_like_amount_preference_rule(text)
    )


def looks_like_finance(text: str) -> bool:
    """True if text is a finance slash command or NL that parses to a transaction."""
    t = (text or "").strip()
    if not t:
        return False
    if looks_like_receipt_flow_text(t):
        return True
    if looks_like_phase3_finance(t):
        return True
    low = t.lower()
    if low.startswith(("/spent", "/budget", "/finance", "/log", "/income", "/goals", "/fixed", "/flex", "/digest", "/save", "/contribute", "/goal", "/savings")):
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
    if parse_set_budget(raw) is not None:
        return None
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


# ---------------------------------------------------------------------------
# Phase 3 — savings / fixed / flex / digest intents
# ---------------------------------------------------------------------------


@dataclass
class ParsedSavingsContribute:
    amount_mvr: float
    goal_hint: str
    raw: str


@dataclass
class ParsedNewGoal:
    name: str
    target_mvr: float
    monthly_target_mvr: float
    raw: str


@dataclass
class ParsedFixed:
    name: str
    amount_mvr: float
    raw: str


_CONTRIBUTE_RE = re.compile(
    r"^(?:save|contribute|/save|/contribute)\s+"
    r"(?:rf\.?\s*|mvr\s*)?(\d+(?:[.,]\d{1,2})?)\s+"
    r"(?:toward|to|into|on)\s+(.+)$",
    re.I,
)
_CONTRIBUTE_ALT = re.compile(
    r"^(?:save|contribute|/save|/contribute)\s+"
    r"(?:rf\.?\s*|mvr\s*)?(\d+(?:[.,]\d{1,2})?)\s+(.+)$",
    re.I,
)

_NEW_GOAL_RE = re.compile(
    r"^(?:new\s+goal|/goal|goal)\s+"
    r"([A-Za-z][\w\s'-]{0,40}?)\s+"
    r"(?:rf\.?\s*|mvr\s*)?(\d+(?:[.,]\d{1,2})?)"
    r"(?:\s+monthly\s+(?:rf\.?\s*|mvr\s*)?(\d+(?:[.,]\d{1,2})?))?\s*$",
    re.I,
)

_FIXED_SET_RE = re.compile(
    r"^(?:fixed|/fixed)\s+"
    r"([A-Za-z][\w\s'-]{0,40}?)\s+"
    r"(?:rf\.?\s*|mvr\s*)?(\d+(?:[.,]\d{1,2})?)\s*$",
    re.I,
)

_GOALS_LIST_RE = re.compile(r"^(?:/goals|goals|savings|/savings)\s*$", re.I)
_FIXED_LIST_RE = re.compile(r"^/fixed\s*$", re.I)
_FLEX_RE = re.compile(
    r"^(?:/flex|flex|variable\s+left|what.?s\s+left\s+for\s+variable)\s*$",
    re.I,
)
_DIGEST_RE = re.compile(r"^(?:/digest|digest)(?:\s+(week|month|weekly|monthly))?\s*$", re.I)


def parse_savings_contribute(text: str) -> Optional[ParsedSavingsContribute]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _CONTRIBUTE_RE.match(raw) or _CONTRIBUTE_ALT.match(raw)
    if not m:
        return None
    # Avoid hijacking "save" that is really expense NL with "toward" missing
    # and a finance category word — still allow goal names freely.
    amount = _to_float(m.group(1))
    if amount <= 0:
        return None
    goal = (m.group(2) or "").strip(" .,!-")
    # Strip trailing soft words
    goal = re.sub(r"\s+(please|now|fund)$", "", goal, flags=re.I).strip()
    if not goal:
        return None
    # If matched via ALT and goal looks like expense ("on groceries at Agora"), skip
    if _CONTRIBUTE_ALT.match(raw) and not _CONTRIBUTE_RE.match(raw):
        if re.search(r"\b(at|on|for)\b", goal, re.I) and _guess_category(goal) != "Other":
            return None
    return ParsedSavingsContribute(amount_mvr=amount, goal_hint=goal, raw=raw)


def parse_new_goal(text: str) -> Optional[ParsedNewGoal]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _NEW_GOAL_RE.match(raw)
    if not m:
        return None
    name = m.group(1).strip(" .,!-")
    target = _to_float(m.group(2))
    monthly = _to_float(m.group(3)) if m.group(3) else 0.0
    if not name or target <= 0:
        return None
    return ParsedNewGoal(
        name=name, target_mvr=target, monthly_target_mvr=monthly, raw=raw
    )


def parse_fixed_set(text: str) -> Optional[ParsedFixed]:
    raw = (text or "").strip()
    if not raw:
        return None
    # Bare /fixed is list, not set
    if _FIXED_LIST_RE.match(raw):
        return None
    m = _FIXED_SET_RE.match(raw)
    if not m:
        return None
    name = m.group(1).strip(" .,!-")
    amount = _to_float(m.group(2))
    if not name or amount <= 0:
        return None
    return ParsedFixed(name=name, amount_mvr=amount, raw=raw)


def is_goals_list(text: str) -> bool:
    return bool(_GOALS_LIST_RE.match((text or "").strip()))


def is_fixed_list(text: str) -> bool:
    return bool(_FIXED_LIST_RE.match((text or "").strip()))


def is_flex_query(text: str) -> bool:
    return bool(_FLEX_RE.match((text or "").strip()))


def parse_digest_period(text: str) -> Optional[str]:
    m = _DIGEST_RE.match((text or "").strip())
    if not m:
        return None
    period = (m.group(1) or "month").lower()
    if period in ("week", "weekly"):
        return "week"
    return "month"



@dataclass
class ParsedSetBudget:
    category_hint: str
    amount_mvr: float
    raw: str


_SET_BUDGET_RES: list[re.Pattern[str]] = [
    # set food budget 3000 / set food limit 3000
    re.compile(
        r"^set\s+([A-Za-z][\w-]{0,30})\s+(?:budget|limit)\s+"
        r"(?:(?:rf\.?\s*|mvr\s*))?(\d+(?:[.,]\d{1,2})?)\s*$",
        re.I,
    ),
    # food budget 3000 / food limit 3000
    re.compile(
        r"^([A-Za-z][\w-]{0,30})\s+(?:budget|limit)\s+"
        r"(?:(?:rf\.?\s*|mvr\s*))?(\d+(?:[.,]\d{1,2})?)\s*$",
        re.I,
    ),
    # set budget food 3000 / set limit food 3000
    re.compile(
        r"^set\s+(?:budget|limit)\s+([A-Za-z][\w-]{0,30})\s+"
        r"(?:(?:rf\.?\s*|mvr\s*))?(\d+(?:[.,]\d{1,2})?)\s*$",
        re.I,
    ),
    # /budget food 3000
    re.compile(
        r"^/budget\s+([A-Za-z][\w-]{0,30})\s+"
        r"(?:(?:rf\.?\s*|mvr\s*))?(\d+(?:[.,]\d{1,2})?)\s*$",
        re.I,
    ),
]

_KNOWN_BUDGET_CATS = {
    "food",
    "transport",
    "rent",
    "utilities",
    "utility",
    "health",
    "shopping",
    "entertainment",
    "other",
    "salary",
    "groceries",
    "grocery",
}


def parse_set_budget(text: str) -> Optional[ParsedSetBudget]:
    """NL set category monthly limit — confirm-before-save in handlers."""
    raw = (text or "").strip()
    if not raw:
        return None
    for pat in _SET_BUDGET_RES:
        m = pat.match(raw)
        if not m:
            continue
        cat_raw = (m.group(1) or "").strip()
        amount = _to_float(m.group(2))
        if amount <= 0 or not cat_raw:
            return None
        low = cat_raw.lower()
        if low in {"my", "the", "a", "an", "our", "this", "monthly", "new"}:
            return None
        if low in {"grocery", "groceries"}:
            hint = "Food"
        elif low == "utility":
            hint = "Utilities"
        elif low in _KNOWN_BUDGET_CATS:
            hint = "Other" if low == "other" else low.title()
        else:
            # Resolve via alias table (agora→Food) or title-case for DB match
            guessed = _guess_category(cat_raw)
            hint = guessed if guessed != "Other" else cat_raw.title()
        return ParsedSetBudget(category_hint=hint, amount_mvr=amount, raw=raw)
    return None


def looks_like_phase3_finance(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if is_goals_list(t) or is_fixed_list(t) or is_flex_query(t):
        return True
    if parse_digest_period(t) is not None:
        return True
    if parse_savings_contribute(t) is not None:
        return True
    if parse_new_goal(t) is not None:
        return True
    if parse_fixed_set(t) is not None:
        return True
    if parse_set_budget(t) is not None:
        return True
    return False
