"""Telegram finance handlers — warm Carliabot voice, confirm-before-save."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import uuid
from datetime import date
from typing import Callable, Optional

from app.finance_parse import ParsedFinance, looks_like_finance, parse_finance
from app.finance_alerts import alert_crossed, format_budget_alert
from app.finance_vision import extract_receipt_from_image
from app import finance_store as store

logger = logging.getLogger(__name__)

_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "do it", "go ahead"}
_NO = {"no", "n", "nah", "nope", "cancel", "don't", "dont", "stop", "nevermind", "never mind"}

_SOFT_CTAS = (
    "Want me to log that?",
    "Sound good?",
    "Should I save it?",
    "Shall I put it down?",
)

_RECEIPT_CUE = re.compile(
    r"\b(receipt|bill|invoice|scan|photo of|from the (?:receipt|bill))\b",
    re.I,
)

RECEIPT_STORAGE_DIR = os.getenv("RECEIPT_STORAGE_DIR", "/data/receipts")
VISION_MIN_CONFIDENCE = 0.5

SendFn = Callable[[str, str, str], bool]  # chat_id, text, thread_id


def _period_label(period: str) -> str:
    return {"today": "today", "week": "this week", "month": "this month"}.get(period, "this month")


def _soft_cta(*parts: object) -> str:
    """Stable soft confirm ending (varies by content, not a form prompt)."""
    seed = "|".join(str(p) for p in parts)
    idx = int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(_SOFT_CTAS)
    return _SOFT_CTAS[idx]


def _looks_like_place(name: str) -> bool:
    """Capitalized token → place (at X); else item (for X)."""
    words = [w for w in (name or "").split() if w]
    if not words:
        return False
    return any(w[:1].isupper() for w in words)


def _confirm_copy(parsed: ParsedFinance, category_name: str, *, from_receipt: bool = False) -> str:
    amt = store.fmt_mvr(parsed.amount_mvr)
    cta = _soft_cta(parsed.amount_mvr, parsed.merchant, parsed.note, parsed.tx_type, category_name)

    if parsed.tx_type == "income":
        label = category_name if category_name and category_name.lower() != "other" else "income"
        extra = f" at {parsed.merchant}" if parsed.merchant else ""
        core = f"{amt} MVR {label.lower()}{extra} coming in"
        if from_receipt:
            return f"From the receipt — {core} — {cta}"
        return f"{core} — {cta}"

    bits: list[str] = [f"{amt} MVR"]
    if parsed.merchant:
        bits.append(
            f"at {parsed.merchant}"
            if _looks_like_place(parsed.merchant)
            else f"for {parsed.merchant}"
        )
    if parsed.note and parsed.note.lower() != (parsed.merchant or "").lower():
        bits.append(f"for {parsed.note}")
    elif not parsed.merchant and not parsed.note and category_name and category_name.lower() not in ("other", ""):
        bits.append(f"for {category_name.lower()}")
    core = " ".join(bits)
    if from_receipt:
        return f"From the receipt — looks like {core} — {cta}"
    return f"{core} — {cta}"


def _help_text() -> str:
    return (
        "Hey — I can keep your money trail in MVR.\n"
        "Try something like: spent 85 on groceries at Agora\n"
        "Or: income 5000 salary\n"
        "Or just send a receipt photo and I'll read it.\n"
        "I'll double-check before saving. Also:\n"
        "/spent — what you've spent this month (or today / week)\n"
        "/budget — how you're tracking against category limits"
    )


def should_try_receipt(caption: str, has_image: bool) -> bool:
    """Heuristic: try vision when image looks like a receipt intent."""
    if not has_image:
        return False
    cap = (caption or "").strip()
    if not cap:
        return True  # image-only → try receipt
    if _RECEIPT_CUE.search(cap):
        return True
    if looks_like_finance(cap) or parse_finance(cap) is not None:
        return True
    # Caption present, clearly non-finance, no receipt cue → leave to orchestration
    return False


def _save_receipt_image(telegram_user_id: int, image_data_url: str) -> Optional[str]:
    """Persist data-URL bytes under RECEIPT_STORAGE_DIR; return container path."""
    try:
        if "," in image_data_url and image_data_url.startswith("data:"):
            b64 = image_data_url.split(",", 1)[1]
        else:
            b64 = image_data_url
        raw = base64.b64decode(b64, validate=False)
        if not raw:
            return None
        user_dir = os.path.join(RECEIPT_STORAGE_DIR, str(telegram_user_id))
        os.makedirs(user_dir, mode=0o755, exist_ok=True)
        path = os.path.join(user_dir, f"{uuid.uuid4().hex}.jpg")
        with open(path, "wb") as fh:
            fh.write(raw)
        return path
    except Exception as exc:
        logger.error(f"finance_receipt_save_error: {exc}")
        return None


def _parse_tx_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def try_handle_finance(
    *,
    text: str,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    chat_type: str,
    send: SendFn,
    image_data_url: str = "",
) -> Optional[str]:
    """Handle finance intents. Return reason string if handled (caller must NOT publish to ingress stream)."""
    if chat_type != "private":
        return None

    text = (text or "").strip()
    image_data_url = (image_data_url or "").strip()
    has_image = bool(image_data_url)

    if not text and not has_image:
        return None

    lowered = text.lower().strip() if text else ""

    # Pending yes/no first (finance confirms take priority over orchestration when pending exists)
    pending = store.get_pending(chat_id)
    if pending is not None and text:
        if lowered in _YES:
            return _confirm_pending(pending, chat_id, thread_id, send)
        if lowered in _NO:
            store.resolve_pending(pending["id"], "cancelled")
            send(chat_id, "Got it — not logging that one.", thread_id)
            return "finance_cancelled"
        # Not yes/no — fall through; if new finance NL / receipt, it will replace pending

    # Help
    if text and lowered in {"/finance", "/finance help", "/log help", "/log"}:
        send(chat_id, _help_text(), thread_id)
        return "finance_help"

    # /budget
    if text and (lowered == "/budget" or lowered.startswith("/budget ")):
        return _handle_budget(telegram_user_id, chat_id, thread_id, send)

    # /spent [today|week|month]
    if text:
        spent_m = re.match(r"^/spent(?:\s+(today|week|month))?\s*$", lowered)
        if spent_m:
            period = spent_m.group(1) or "month"
            return _handle_spent(telegram_user_id, period, chat_id, thread_id, send)

    # Receipt photo path
    if has_image and should_try_receipt(text, has_image):
        return _handle_receipt_image(
            text=text,
            image_data_url=image_data_url,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
        )

    # Image with clearly non-finance caption → leave to orchestration
    if has_image and text and not should_try_receipt(text, has_image):
        return None

    if not text:
        return None

    # Parseable finance NL or /spent|/income|/log with amount
    if not looks_like_finance(text) and parse_finance(text) is None:
        return None

    parsed = parse_finance(text)
    if parsed is None:
        return None

    return _start_confirm(parsed, telegram_user_id, chat_id, thread_id, send)


def _handle_receipt_image(
    *,
    text: str,
    image_data_url: str,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    vision = extract_receipt_from_image(image_data_url)
    if vision is None or vision.confidence < VISION_MIN_CONFIDENCE or vision.amount is None:
        # Short finance caption alone can still start a text confirm without vision
        cap_parsed = parse_finance(text) if text else None
        if cap_parsed is not None:
            return _start_confirm(
                cap_parsed, telegram_user_id, chat_id, thread_id, send, from_receipt=False
            )
        send(
            chat_id,
            "Couldn't read that as a receipt — want to type it instead? "
            "Something like: spent 85 on groceries at Agora",
            thread_id,
        )
        return "finance_receipt_low_confidence"

    amount = float(vision.amount)
    merchant = vision.merchant or ""
    note = vision.note or ""
    category_hint = vision.category_hint or "Other"
    tx_date = _parse_tx_date(vision.date)

    # Caption NL can override merchant/amount/category when parseable
    if text:
        cap = parse_finance(text)
        if cap is not None:
            amount = float(cap.amount_mvr)
            if cap.merchant:
                merchant = cap.merchant
            if cap.note:
                note = cap.note
            if cap.category_hint and cap.category_hint != "Other":
                category_hint = cap.category_hint
        else:
            # Lightweight overrides: "at STO" / category words without full parse
            at_m = re.search(
                r"\bat\s+([A-Za-z0-9][\w\s&'.-]{0,40}?)(?:\s+(?:for|on|—|-)\s+|$)",
                text,
                re.I,
            )
            if at_m:
                merchant = at_m.group(1).strip(" .,!-")
            # Reuse parse category aliases via a tiny probe string
            probe = parse_finance(f"spent 1 {text}")
            if probe and probe.category_hint and probe.category_hint != "Other":
                category_hint = probe.category_hint

    receipt_path = _save_receipt_image(telegram_user_id, image_data_url)

    parsed = ParsedFinance(
        amount_mvr=amount,
        tx_type="expense",
        category_hint=category_hint,
        merchant=merchant,
        note=note,
        raw=text or "[receipt photo]",
    )
    return _start_confirm(
        parsed,
        telegram_user_id,
        chat_id,
        thread_id,
        send,
        from_receipt=True,
        receipt_image_path=receipt_path,
        tx_date=tx_date,
    )


def _start_confirm(
    parsed: ParsedFinance,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
    *,
    from_receipt: bool = False,
    receipt_image_path: Optional[str] = None,
    tx_date: Optional[date] = None,
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Hmm, I couldn't reach the books right now. Try again in a sec?", thread_id)
        return "finance_db_error"

    store.seed_default_categories(user_id)
    cat_id, cat_name = store.resolve_category_id(user_id, parsed.category_hint)

    payload = {
        "tx_type": parsed.tx_type,
        "amount_mvr": float(parsed.amount_mvr),
        "category_id": cat_id,
        "category_name": cat_name,
        "merchant": parsed.merchant or "",
        "note": parsed.note or "",
        "raw": parsed.raw,
        "from_receipt": bool(from_receipt),
        "receipt_image_path": receipt_image_path,
        "tx_date": tx_date.isoformat() if tx_date else None,
    }
    pid = store.create_pending(chat_id, telegram_user_id, user_id, payload)
    if pid is None:
        send(chat_id, "Couldn't park that for confirmation — mind sending it again?", thread_id)
        return "finance_pending_error"

    send(chat_id, _confirm_copy(parsed, cat_name, from_receipt=from_receipt), thread_id)
    return "finance_pending_confirm"


def _confirm_pending(
    pending: dict,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    payload = pending["payload"] or {}
    tx_date = _parse_tx_date(payload.get("tx_date"))
    receipt_path = payload.get("receipt_image_path") or None
    cat_id = payload.get("category_id")
    amount = float(payload.get("amount_mvr") or 0)
    tx_type = payload.get("tx_type") or "expense"

    spent_before = None
    limit = None
    cat_name_snap = payload.get("category_name") or "Other"
    if tx_type == "expense" and cat_id is not None:
        snap = store.category_month_spend_and_limit(pending["user_id"], int(cat_id))
        if snap:
            spent_before = snap["spent"]
            limit = snap["limit"]
            cat_name_snap = snap["name"] or cat_name_snap

    tx_id = store.insert_transaction(
        user_id=pending["user_id"],
        category_id=cat_id,
        tx_type=tx_type,
        amount_mvr=amount,
        merchant=payload.get("merchant") or "",
        note=payload.get("note") or "",
        tx_date=tx_date,
        receipt_image_path=receipt_path,
    )
    if tx_id is None:
        send(chat_id, "Ugh — confirm worked but the save didn't. Want to try again?", thread_id)
        return "finance_insert_error"

    store.resolve_pending(pending["id"], "confirmed")
    amt = store.fmt_mvr(payload.get("amount_mvr"))
    cat = payload.get("category_name") or "Other"
    merchant = payload.get("merchant") or ""
    if tx_type == "income":
        tail = f" under {cat}" if cat else ""
        send(chat_id, f"Logged — {amt} MVR income{tail}. Nice.", thread_id)
    else:
        note = payload.get("note") or ""
        if merchant and _looks_like_place(merchant):
            where = f" at {merchant}"
        elif merchant:
            where = f" for {merchant}"
        elif note:
            where = f" for {note}"
        else:
            where = ""
        send(chat_id, f"Logged — {amt} MVR{where} ({cat}).", thread_id)

        # Post-save budget alert (80% / 100% newly crossed)
        if spent_before is not None and limit is not None:
            level = alert_crossed(spent_before, amount, limit)
            if level:
                spent_after = spent_before + amount
                alert_msg = format_budget_alert(
                    cat_name_snap, spent_after, limit, level, store.fmt_mvr
                )
                send(chat_id, alert_msg, thread_id)

    return "finance_confirmed"


def _handle_spent(
    telegram_user_id: int,
    period: str,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Couldn't peek at spending right now — try again shortly?", thread_id)
        return "finance_db_error"

    total, count = store.sum_expenses(user_id, period)
    label = _period_label(period)
    if count == 0:
        send(chat_id, f"Nothing logged {label} yet — quiet on the spending front.", thread_id)
    elif count == 1:
        send(
            chat_id,
            f"You've spent {store.fmt_mvr(total)} MVR {label} (1 expense).",
            thread_id,
        )
    else:
        send(
            chat_id,
            f"You've spent {store.fmt_mvr(total)} MVR {label} across {count} expenses.",
            thread_id,
        )
    return "finance_spent"


def _handle_budget(
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Budget check failed on my end — give me another shot?", thread_id)
        return "finance_db_error"

    store.seed_default_categories(user_id)
    rows = store.budget_snapshot(user_id)
    if not rows:
        send(
            chat_id,
            "No category limits set yet — tell me when you want caps on Food/Rent/etc and I'll track spend vs limit here. /spent still works anytime.",
            thread_id,
        )
        return "finance_budget_empty"

    lines = ["Here's how this month's looking:"]
    for r in rows:
        spent = r["spent"]
        limit = r["limit"]
        left = limit - spent
        if left < 0:
            lines.append(
                f"· {r['name']}: {store.fmt_mvr(spent)} / {store.fmt_mvr(limit)} MVR — over by {store.fmt_mvr(-left)}"
            )
        else:
            lines.append(
                f"· {r['name']}: {store.fmt_mvr(spent)} / {store.fmt_mvr(limit)} MVR — {store.fmt_mvr(left)} left"
            )
    send(chat_id, "\n".join(lines), thread_id)
    return "finance_budget"
