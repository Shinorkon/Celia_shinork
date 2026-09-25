"""Telegram finance handlers — warm Carliabot voice, confirm-before-save."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import date
from threading import Lock
from typing import Callable, Optional

from app.finance_parse import (
    ParsedFinance,
    looks_like_amount_preference_rule,
    looks_like_finance,
    looks_like_phase3_finance,
    looks_like_receipt_flow_text,
    looks_like_receipt_recalculate,
    looks_like_receipt_spend_ask,
    wants_breakdown,
    wants_total_only,
    parse_finance,
    parse_savings_contribute,
    parse_new_goal,
    parse_fixed_set,
    parse_set_budget,
    is_goals_list,
    is_fixed_list,
    is_flex_query,
    parse_digest_period,
)
from app.finance_alerts import alert_crossed, format_budget_alert
from app.finance_vision import extract_receipt_from_image
from app.finance_digest import (
    build_digest_text,
    build_goals_list_text,
    build_fixed_list_text,
    build_flex_text,
)
from app import finance_store as store

logger = logging.getLogger(__name__)

_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "do it", "go ahead"}
_NO = {"no", "n", "nah", "nope", "cancel", "don't", "dont", "stop", "nevermind", "never mind"}

_SOFT_CTAS = (
    "Want me to log that?",
    "Sound good?",
    "Shall I put it down?",
)

# Income must not say "save" — that reads like savings goals.
_INCOME_CTAS = (
    "Want me to log that?",
    "Sound good?",
)

_BUDGET_CTAS = (
    "Sound good?",
    "Want me to set that?",
    "Shall I lock that in?",
    "Cool if I cap it there?",
)

_RECEIPT_CUE = re.compile(
    r"\b(receipt|bill|invoice|scan|photo of|from the (?:receipt|bill))\b",
    re.I,
)

RECEIPT_STORAGE_DIR = os.getenv("RECEIPT_STORAGE_DIR", "/data/receipts")
VISION_MIN_CONFIDENCE = 0.5

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
_SESSION_TTL_SEC = int(os.getenv("FINANCE_SESSION_TTL_SEC", str(45 * 60)))
_SESSION_LOCK = Lock()
_MEM_SESSIONS: dict[str, dict] = {}


def _redis():
    try:
        from redis import Redis

        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("finance_session_redis_unavailable: %s", exc)
        return None


def _session_key(chat_id: str) -> str:
    return f"celia:finance:session:{chat_id}"


def _empty_session(now: Optional[float] = None) -> dict:
    return {
        "total_only": False,
        "calc_mode": False,
        "prefer_lower_text_amount": False,
        "receipts": [],  # list[{amount, merchant, category}]
        "updated_at": float(now if now is not None else time.time()),
    }


def _normalize_session(raw: dict, *, now: Optional[float] = None) -> dict:
    s = _empty_session(now)
    if not isinstance(raw, dict):
        return s
    s["total_only"] = bool(raw.get("total_only"))
    s["calc_mode"] = bool(raw.get("calc_mode"))
    s["prefer_lower_text_amount"] = bool(raw.get("prefer_lower_text_amount"))
    s["updated_at"] = float(raw.get("updated_at") or s["updated_at"])
    receipts = []
    for r in raw.get("receipts") or []:
        if not isinstance(r, dict):
            continue
        try:
            entry = {
                "amount": float(r.get("amount") or 0),
                "merchant": (r.get("merchant") or "").strip(),
                "category": (r.get("category") or "").strip(),
            }
            if r.get("vision_amount") is not None:
                entry["vision_amount"] = float(r["vision_amount"])
            if r.get("text_amount") is not None:
                entry["text_amount"] = float(r["text_amount"])
            receipts.append(entry)
        except (TypeError, ValueError):
            continue
    s["receipts"] = receipts
    return s


def _save_finance_session(chat_id: str, session: dict) -> dict:
    """Persist receipt session to Redis (TTL ~45m) with in-process fallback."""
    chat_id = str(chat_id)
    s = _normalize_session(session, now=time.time())
    r = _redis()
    if r is not None:
        try:
            r.set(_session_key(chat_id), json.dumps(s), ex=_SESSION_TTL_SEC)
            return s
        except Exception as exc:
            logger.error("finance_session_save_redis_error: %s", exc)
    with _SESSION_LOCK:
        _MEM_SESSIONS[chat_id] = s
    return s


def _get_finance_session(chat_id: str) -> dict:
    """Per-chat receipt session (total-only + batch totals) via Redis.

    Survives ingress restart; multi-replica safe. Falls back to in-process
    dict when Redis is unavailable (same shape as pre-slice-3).
    """
    chat_id = str(chat_id)
    now = time.time()
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_session_key(chat_id))
            if raw:
                s = _normalize_session(json.loads(raw), now=now)
                # Touch TTL so mid-batch restarts keep the window alive.
                r.set(_session_key(chat_id), json.dumps(s), ex=_SESSION_TTL_SEC)
                return s
        except Exception as exc:
            logger.error("finance_session_get_redis_error: %s", exc)

    with _SESSION_LOCK:
        s = _MEM_SESSIONS.get(chat_id)
        if not s or now - float(s.get("updated_at", 0)) > _SESSION_TTL_SEC:
            s = _empty_session(now)
            _MEM_SESSIONS[chat_id] = s
        else:
            s = _normalize_session(s, now=now)
            _MEM_SESSIONS[chat_id] = s
        return s


def _mark_total_only(chat_id: str, *, calc_mode: bool = False) -> dict:
    s = _get_finance_session(chat_id)
    s["total_only"] = True
    if calc_mode:
        s["calc_mode"] = True
    return _save_finance_session(chat_id, s)


def _append_session_receipt(
    chat_id: str,
    amount: float,
    merchant: str,
    category: str,
    *,
    vision_amount: Optional[float] = None,
    text_amount: Optional[float] = None,
) -> dict:
    s = _get_finance_session(chat_id)
    receipts = list(s.get("receipts") or [])
    entry: dict = {
        "amount": float(amount),
        "merchant": (merchant or "").strip(),
        "category": (category or "").strip(),
    }
    if vision_amount is not None:
        entry["vision_amount"] = float(vision_amount)
    if text_amount is not None:
        entry["text_amount"] = float(text_amount)
    receipts.append(entry)
    s["receipts"] = receipts
    return _save_finance_session(chat_id, s)


def clear_finance_session_for_tests(chat_id: str = "") -> None:
    """Test helper: drop Redis + mem session(s)."""
    with _SESSION_LOCK:
        if chat_id:
            _MEM_SESSIONS.pop(str(chat_id), None)
        else:
            _MEM_SESSIONS.clear()
    r = _redis()
    if r is None:
        return
    try:
        if chat_id:
            r.delete(_session_key(str(chat_id)))
        else:
            for key in r.scan_iter(match="celia:finance:session:*", count=100):
                r.delete(key)
    except Exception as exc:
        logger.warning("finance_session_clear_error: %s", exc)


def _fmt_receipt_oneliner(amount: float, merchant: str) -> str:
    amt = store.fmt_mvr(amount)
    m = (merchant or "").strip()
    if m:
        return f"{m} — {amt} MVR"
    return f"{amt} MVR"


def _batch_totals_copy(receipts: list[dict], *, heading: str = "") -> str:
    """Long merchant list — only when user asks for breakdown."""
    if not receipts:
        return "No receipt totals parked yet — send the photos and I'll add them up."
    lines = []
    if heading:
        lines.append(heading)
    total = 0.0
    for r in receipts:
        total += float(r["amount"])
        lines.append(_fmt_receipt_oneliner(r["amount"], r.get("merchant") or ""))
    if len(receipts) == 1:
        lines.append(f"Total: {store.fmt_mvr(total)} MVR")
    else:
        lines.append(f"Sum ({len(receipts)}): {store.fmt_mvr(total)} MVR")
    return "\n".join(lines)


def _session_sum(receipts: list[dict]) -> float:
    return sum(float(r["amount"]) for r in receipts or [])


def _short_batch_summary(receipts: list[dict]) -> str:
    """Default quiet reply: count + sum only (Carlia voice)."""
    if not receipts:
        return "No receipt totals parked yet — send the photos and I'll add them up."
    n = len(receipts)
    total = _session_sum(receipts)
    amt = store.fmt_mvr(total)
    label = "receipt" if n == 1 else "receipts"
    return f"{n} {label} · {amt} MVR"


def _session_totals_reply(receipts: list[dict], text: str = "") -> str:
    """Short by default; long merchant dump only on explicit breakdown ask."""
    if wants_breakdown(text or ""):
        return _batch_totals_copy(receipts)
    return _short_batch_summary(receipts)


def _set_prefer_lower_text_amount(chat_id: str, value: bool = True) -> dict:
    s = _get_finance_session(chat_id)
    s["prefer_lower_text_amount"] = bool(value)
    return _save_finance_session(chat_id, s)


def _persist_amount_pref_memory(telegram_user_id: int, chat_id: str) -> None:
    """Best-effort procedural/semantic preference — non-fatal if memory DB is down."""
    try:
        from app import memory_store as mem

        uid = mem.ensure_user(telegram_user_id)
        if uid is None:
            return
        mem.save_item(
            db_user_id=uid,
            kind="habit",
            title="Prefer lower text amount on receipts",
            body=(
                "When a receipt photo has an amount and a separate text has a "
                "lower amount, count the lower (text) amount."
            ),
            tags=["finance", "receipt", "preference"],
            segment="procedural",
            importance=0.7,
            salience=0.7,
            source_chat_id=str(chat_id),
        )
    except Exception as exc:
        logger.warning("finance_amount_pref_memory_error: %s", exc)


def _strip_finance_opener(msg: str) -> str:
    """Never ship ✅/capability openers on finance replies."""
    s = (msg or "").strip()
    # Drop leading status emoji prefixes if somehow present
    s = re.sub(r"^[\u2705\u274c\u2139\ufe0f]+\s*", "", s)
    s = re.sub(r"^✅\s*", "", s)
    return s




def receipt_low_confidence_copy() -> str:
    """Warm short reply when vision can't read total — no menus/backticks."""
    return (
        "Couldn't quite catch the total on that photo — "
        "type the amount if you can? spent 85 at Agora works, "
        "or send a clearer shot of the total."
    )


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
    """Short confirm: amount + merchant (+ category if useful) + soft CTA.

    Receipt confirms never include invoice/cashier/payment/date fluff via note.
    """
    amt = store.fmt_mvr(parsed.amount_mvr)

    if parsed.tx_type == "income":
        seed = "|".join(
            str(p)
            for p in (parsed.amount_mvr, parsed.merchant, parsed.note, category_name, "income")
        )
        cta = _INCOME_CTAS[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(_INCOME_CTAS)]
        label = category_name if category_name and category_name.lower() != "other" else "income"
        extra = f" at {parsed.merchant}" if parsed.merchant else ""
        core = f"{amt} MVR {label.lower()}{extra} coming in"
        return _strip_finance_opener(f"{core} — {cta}")

    cta = _soft_cta(parsed.amount_mvr, parsed.merchant, parsed.note, parsed.tx_type, category_name)

    bits: list[str] = [f"{amt} MVR"]
    if parsed.merchant:
        bits.append(
            f"at {parsed.merchant}"
            if _looks_like_place(parsed.merchant)
            else f"for {parsed.merchant}"
        )
    if from_receipt:
        # Receipt path: amount + merchant; category only when merchant missing
        if not parsed.merchant and category_name and category_name.lower() not in ("other", ""):
            bits.append(f"for {category_name.lower()}")
    else:
        if parsed.note and parsed.note.lower() != (parsed.merchant or "").lower():
            bits.append(f"for {parsed.note}")
        elif not parsed.merchant and not parsed.note and category_name and category_name.lower() not in ("other", ""):
            bits.append(f"for {category_name.lower()}")
    core = " ".join(bits)
    return _strip_finance_opener(f"{core} — {cta}")


def _help_text() -> str:
    return (
        "Hey — I can keep your money trail in MVR.\n"
        "Try something like: spent 85 on groceries at Agora\n"
        "Or: income 5000 salary\n"
        "Or: set food budget 3000\n"
        "Or just send a receipt photo and I'll read it.\n"
        "I'll double-check before saving. Also:\n"
        "/spent — what you've spent this month (or today / week)\n"
        "/budget — how you're tracking against category limits\n"
        "/goals — savings progress · or say save 500 toward emergency\n"
        "/fixed — fixed bills · or say fixed rent 12000\n"
        "/flex — what's left for variable after fixed\n"
        "/digest — weekly or monthly money snapshot"
    )


def should_try_receipt(caption: str, has_image: bool, chat_id: str = "") -> bool:
    """Heuristic: try vision when image looks like a receipt intent."""
    if not has_image:
        return False
    # Active calc / total-only session → always treat photos as receipts
    if chat_id:
        s = _get_finance_session(chat_id)
        if s.get("calc_mode") or s.get("total_only") or s.get("receipts"):
            return True
    cap = (caption or "").strip()
    if not cap:
        return True  # image-only → try receipt
    if _RECEIPT_CUE.search(cap):
        return True
    if looks_like_receipt_flow_text(cap):
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
    image_data_urls: Optional[list] = None,
) -> Optional[str]:
    """Handle finance intents. Return reason string if handled (caller must NOT publish to ingress stream)."""
    chat_id = str(chat_id)
    if chat_type != "private":
        return None

    text = (text or "").strip()
    urls: list[str] = []
    for u in list(image_data_urls or []):
        u = (u or "").strip()
        if u and u not in urls:
            urls.append(u)
    single = (image_data_url or "").strip()
    if single and single not in urls:
        urls.append(single)
    image_data_url = urls[-1] if urls else ""
    has_image = bool(urls)

    if not text and not has_image:
        return None

    lowered = text.lower().strip() if text else ""

    # Pending yes/no first (finance confirms take priority over orchestration when pending exists)
    pending = store.get_pending(chat_id)
    if pending is not None and text:
        if lowered in _YES:
            return _confirm_pending(pending, chat_id, thread_id, send)
        if lowered in _NO:
            kind = (pending.get("payload") or {}).get("kind") or "tx"
            store.resolve_pending(pending["id"], "cancelled")
            if kind == "set_budget":
                send(chat_id, "Got it — leaving that limit alone.", thread_id)
            else:
                send(chat_id, "Got it — not logging that one.", thread_id)
            return "finance_cancelled"
        # Not yes/no — finance receipt/total cues stay here (never orchestration essays)
        if looks_like_receipt_flow_text(text):
            return _handle_receipt_flow_text(
                text=text,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                send=send,
                pending=pending,
            )
        # else fall through; if new finance NL / receipt, it will replace pending

    # Recalculate parked session (before list / AOP — never shopping-list add)
    if text and looks_like_receipt_recalculate(text) and not has_image:
        return _handle_receipt_recalculate(
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
        )

    # Amount preference rules (short confirm — never re-dump merchant list)
    if text and looks_like_amount_preference_rule(text) and not has_image:
        return _handle_amount_preference_rule(
            text=text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
        )

    # Receipt spend / total-only / breakdown asks (no amount) — before slash help
    if text and looks_like_receipt_flow_text(text) and not has_image:
        return _handle_receipt_flow_text(
            text=text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
            pending=store.get_pending(chat_id),
        )

    # Help
    if text and lowered in {"/finance", "/finance help", "/log help", "/log"}:
        send(chat_id, _help_text(), thread_id)
        return "finance_help"

    # set food budget 3000 / /budget food 3000 (before bare /budget snapshot)
    if text:
        set_b = parse_set_budget(text)
        if set_b is not None:
            return _start_set_budget(set_b, telegram_user_id, chat_id, thread_id, send)

    # /budget snapshot
    if text and (lowered == "/budget" or lowered.startswith("/budget ")):
        return _handle_budget(telegram_user_id, chat_id, thread_id, send)

    # /spent [today|week|month]
    if text:
        spent_m = re.match(r"^/spent(?:\s+(today|week|month))?\s*$", lowered)
        if spent_m:
            period = spent_m.group(1) or "month"
            return _handle_spent(telegram_user_id, period, chat_id, thread_id, send)

    # Phase 3 — savings / fixed / flex / digest (lists + mutations)
    if text and looks_like_phase3_finance(text):
        handled = _handle_phase3(
            text=text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
        )
        if handled is not None:
            return handled

    # Receipt photo path (one or many from debounce / media_group)
    if has_image and should_try_receipt(text, has_image, chat_id):
        return _handle_receipt_images(
            text=text,
            image_data_urls=urls,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
        )

    # Image with clearly non-finance caption → leave to orchestration
    if has_image and text and not should_try_receipt(text, has_image, chat_id):
        return None

    if not text:
        return None

    # Parseable finance NL or /spent|/income|/log with amount
    if looks_like_receipt_flow_text(text):
        return _handle_receipt_flow_text(
            text=text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
            pending=store.get_pending(chat_id),
        )

    if not looks_like_finance(text) and parse_finance(text) is None:
        return None

    parsed = parse_finance(text)
    if parsed is None:
        return None

    return _start_confirm(parsed, telegram_user_id, chat_id, thread_id, send)


def _apply_lower_pref_to_session(chat_id: str) -> tuple[int, int, dict]:
    """Re-apply prefer_lower_text_amount to session rows that have both amounts.

    Returns (updated_count, dual_missing_count, session).
    """
    chat_id = str(chat_id)
    s = _get_finance_session(chat_id)
    receipts = list(s.get("receipts") or [])
    updated = 0
    dual_missing = 0
    new_receipts = []
    for r in receipts:
        vision = r.get("vision_amount")
        text_amt = r.get("text_amount")
        entry = dict(r)
        if vision is not None and text_amt is not None:
            new_amt = min(float(vision), float(text_amt))
            if abs(float(entry.get("amount") or 0) - new_amt) >= 0.001:
                updated += 1
            entry["amount"] = new_amt
        else:
            dual_missing += 1
        new_receipts.append(entry)
    s["receipts"] = new_receipts
    s = _save_finance_session(chat_id, s)
    return updated, dual_missing, s


def _handle_receipt_recalculate(
    *,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    """Apply session prefs to parked receipts; one short total reply."""
    chat_id = str(chat_id)
    session = _get_finance_session(chat_id)
    receipts = list(session.get("receipts") or [])
    if not receipts and not session.get("calc_mode") and not session.get("total_only"):
        send(
            chat_id,
            _strip_finance_opener(
                "Nothing parked to recalculate — send receipts or ask for a total first."
            ),
            thread_id,
        )
        return "finance_recalc_empty"

    if not receipts:
        send(
            chat_id,
            _strip_finance_opener(
                "Nothing parked to recalculate — send receipts or ask for a total first."
            ),
            thread_id,
        )
        return "finance_recalc_empty"

    prefer = bool(session.get("prefer_lower_text_amount"))
    updated = 0
    dual_missing = 0
    if prefer:
        updated, dual_missing, session = _apply_lower_pref_to_session(chat_id)
        receipts = list(session.get("receipts") or [])

    summary = _short_batch_summary(receipts)
    if prefer and updated:
        msg = f"Updated {updated} · {summary}"
        if dual_missing:
            msg += (
                f"\n({dual_missing} without a text amount stayed as-is.)"
            )
    elif prefer and dual_missing and not updated:
        msg = (
            f"{summary}\n"
            "Preference is on — older lines without a text amount stay as-is; "
            "new receipts will use the lower text."
        )
    elif prefer:
        msg = summary
    else:
        msg = summary

    send(chat_id, _strip_finance_opener(msg), thread_id)
    return "finance_recalc_done"


def _handle_amount_preference_rule(
    *,
    text: str,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    """Store lower-text-amount preference; short confirm — never dump the list."""
    chat_id = str(chat_id)
    _set_prefer_lower_text_amount(chat_id, True)
    _persist_amount_pref_memory(telegram_user_id, chat_id)
    send(
        chat_id,
        _strip_finance_opener(
            "Got it — I'll use the lower text amount when both are present."
        ),
        thread_id,
    )
    return "finance_amount_pref_saved"


def _handle_receipt_flow_text(
    *,
    text: str,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
    pending: Optional[dict] = None,
) -> str:
    """Handle spending-from-receipts / just-the-total / scan-all / breakdown."""
    chat_id = str(chat_id)
    session = _get_finance_session(chat_id)

    if looks_like_receipt_recalculate(text):
        return _handle_receipt_recalculate(
            chat_id=chat_id, thread_id=thread_id, send=send
        )

    if looks_like_amount_preference_rule(text):
        return _handle_amount_preference_rule(
            text=text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
        )

    # Fresh receipt-total session must not leak a stale confirm pending.
    if looks_like_receipt_spend_ask(text) and not session.get("receipts"):
        if pending is not None:
            try:
                store.clear_pending_for_chat(chat_id, "cancelled")
            except Exception:
                pid = pending.get("id")
                if pid is not None:
                    store.resolve_pending(int(pid), "cancelled")
            pending = None
        _mark_total_only(chat_id, calc_mode=True)
        send(
            chat_id,
            _strip_finance_opener(
                "Yep — send the receipt photos and I'll add up the totals "
                "(just amounts + shops, no item lists)."
            ),
            thread_id,
        )
        return "finance_awaiting_receipts"

    if wants_total_only(text) or looks_like_receipt_spend_ask(text):
        _mark_total_only(chat_id, calc_mode=bool(session.get("calc_mode")) or True)
        session = _get_finance_session(chat_id)

    if session.get("receipts"):
        reply = _session_totals_reply(session["receipts"], text)
        send(chat_id, _strip_finance_opener(reply), thread_id)
        return (
            "finance_receipt_breakdown"
            if wants_breakdown(text)
            else "finance_receipt_totals"
        )

    if pending is not None:
        payload = pending.get("payload") or {}
        if payload.get("kind") in (None, "tx", "") and payload.get("amount_mvr") is not None:
            amt = float(payload["amount_mvr"])
            merchant = (payload.get("merchant") or "").strip()
            one = _fmt_receipt_oneliner(amt, merchant)
            send(
                chat_id,
                _strip_finance_opener(f"{one}\n(That's the one waiting on confirm.)"),
                thread_id,
            )
            return "finance_receipt_totals_pending"

    if session.get("calc_mode"):
        _mark_total_only(chat_id, calc_mode=True)
        send(
            chat_id,
            _strip_finance_opener(
                "Yep — send the receipt photos and I'll add up the totals "
                "(just amounts + shops, no item lists)."
            ),
            thread_id,
        )
        return "finance_awaiting_receipts"

    send(
        chat_id,
        _strip_finance_opener(
            "Send the receipt photos and I'll give you the totals — "
            "or say which ones to sum if you've got a few coming."
        ),
        thread_id,
    )
    return "finance_awaiting_receipts"


def _vision_fields_for_image(
    *,
    image_data_url: str,
    text: str,
    session: dict,
) -> Optional[dict]:
    """Run vision + caption overrides. Returns None on low confidence."""
    vision = extract_receipt_from_image(image_data_url)
    if vision is None or vision.confidence < VISION_MIN_CONFIDENCE or vision.amount is None:
        return None

    vision_amount = float(vision.amount)
    amount = vision_amount
    merchant = vision.merchant or ""
    category_hint = vision.category_hint or "Other"
    tx_date = _parse_tx_date(vision.date)
    caption_amount = None

    if text:
        cap = parse_finance(text)
        if cap is not None:
            caption_amount = float(cap.amount_mvr)
            if session.get("prefer_lower_text_amount"):
                amount = min(vision_amount, caption_amount)
            else:
                # Caption still wins when present (pre-existing behaviour).
                amount = caption_amount
            if cap.merchant:
                merchant = cap.merchant
            if cap.category_hint and cap.category_hint != "Other":
                category_hint = cap.category_hint
        else:
            at_m = re.search(
                r"\bat\s+([A-Za-z0-9][\w\s&'.-]{0,40}?)(?:\s+(?:for|on|—|-)\s+|$)",
                text,
                re.I,
            )
            if at_m:
                merchant = at_m.group(1).strip(" .,!-")
            probe = parse_finance(f"spent 1 {text}")
            if probe and probe.category_hint and probe.category_hint != "Other":
                category_hint = probe.category_hint

    return {
        "amount": amount,
        "merchant": merchant,
        "category_hint": category_hint,
        "tx_date": tx_date,
        "vision_amount": vision_amount,
        "caption_amount": caption_amount,
    }


def _fold_pending_into_session(chat_id: str) -> None:
    pending = store.get_pending(chat_id)
    if pending is None:
        return
    payload = pending.get("payload") or {}
    if not payload.get("from_receipt") or payload.get("amount_mvr") is None:
        return
    session = _get_finance_session(chat_id)
    already = any(
        abs(float(r["amount"]) - float(payload["amount_mvr"])) < 0.001
        and (r.get("merchant") or "") == (payload.get("merchant") or "")
        for r in session.get("receipts") or []
    )
    if not already:
        _append_session_receipt(
            chat_id,
            float(payload["amount_mvr"]),
            payload.get("merchant") or "",
            payload.get("category_name") or "",
        )
    store.resolve_pending(pending["id"], "cancelled")
    _mark_total_only(chat_id)


def _handle_receipt_images(
    *,
    text: str,
    image_data_urls: list,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    """Process one or many receipt photos; batch → one short reply."""
    urls = [u for u in (image_data_urls or []) if (u or "").strip()]
    if not urls:
        return "finance_receipt_no_image"
    if len(urls) == 1:
        return _handle_receipt_image(
            text=text,
            image_data_url=urls[0],
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            send=send,
            force_batch=False,
        )

    # Multi-photo turn (media group / debounce burst) → one batch pass, one reply.
    session = _get_finance_session(chat_id)
    if text and (wants_total_only(text) or looks_like_receipt_spend_ask(text)):
        _mark_total_only(chat_id, calc_mode=True)
        session = _get_finance_session(chat_id)

    _fold_pending_into_session(chat_id)
    added = 0
    failed = 0
    # Caption applies to the first image only (Telegram album captions sit on one part).
    for idx, url in enumerate(urls):
        cap = text if idx == 0 else ""
        fields = _vision_fields_for_image(
            image_data_url=url, text=cap, session=_get_finance_session(chat_id)
        )
        if fields is None:
            failed += 1
            continue
        _append_session_receipt(
            chat_id,
            fields["amount"],
            fields["merchant"],
            fields["category_hint"],
            vision_amount=fields.get("vision_amount"),
            text_amount=fields.get("caption_amount"),
        )
        _save_receipt_image(telegram_user_id, url)
        added += 1

    _mark_total_only(chat_id, calc_mode=True)
    session = _get_finance_session(chat_id)
    if not session.get("receipts") and added == 0:
        send(
            chat_id,
            _strip_finance_opener(receipt_low_confidence_copy()),
            thread_id,
        )
        return "finance_receipt_low_confidence"

    reply = _session_totals_reply(session["receipts"], text)
    if failed and added:
        reply = f"{reply}\n(Skipped {failed} I couldn't read.)"
    send(chat_id, _strip_finance_opener(reply), thread_id)
    return "finance_receipt_batched"


def _handle_receipt_image(
    *,
    text: str,
    image_data_url: str,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
    force_batch: bool = False,
) -> str:
    session = _get_finance_session(chat_id)
    if text and (wants_total_only(text) or looks_like_receipt_spend_ask(text)):
        _mark_total_only(chat_id, calc_mode=True)
        session = _get_finance_session(chat_id)

    fields = _vision_fields_for_image(
        image_data_url=image_data_url, text=text, session=session
    )
    if fields is None:
        # Short finance caption alone can still start a text confirm without vision
        cap_parsed = parse_finance(text) if text else None
        if (
            cap_parsed is not None
            and not session.get("total_only")
            and not session.get("calc_mode")
            and not force_batch
        ):
            return _start_confirm(
                cap_parsed, telegram_user_id, chat_id, thread_id, send, from_receipt=False
            )
        send(
            chat_id,
            _strip_finance_opener(receipt_low_confidence_copy()),
            thread_id,
        )
        return "finance_receipt_low_confidence"

    amount = float(fields["amount"])
    merchant = fields["merchant"] or ""
    note = ""  # never carry vision note fluff into confirm/batch
    category_hint = fields["category_hint"] or "Other"
    tx_date = fields["tx_date"]

    # Multi-receipt / calc / total-only: accumulate + short totals, never itemize
    pending = store.get_pending(chat_id)
    fold_pending = False
    if pending is not None:
        payload = pending.get("payload") or {}
        if payload.get("from_receipt") and payload.get("amount_mvr") is not None:
            fold_pending = True

    use_batch = bool(
        force_batch
        or session.get("total_only")
        or session.get("calc_mode")
        or session.get("receipts")
        or fold_pending
        or (text and looks_like_receipt_flow_text(text))
    )

    if use_batch:
        if fold_pending:
            _fold_pending_into_session(chat_id)

        _append_session_receipt(
            chat_id,
            amount,
            merchant,
            category_hint,
            vision_amount=fields.get("vision_amount"),
            text_amount=fields.get("caption_amount"),
        )
        session = _get_finance_session(chat_id)
        _mark_total_only(chat_id)
        send(
            chat_id,
            _strip_finance_opener(_session_totals_reply(session["receipts"], text)),
            thread_id,
        )
        # Persist image for possible later log, but do not open fat confirm
        _save_receipt_image(telegram_user_id, image_data_url)
        return "finance_receipt_batched"

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

    send(chat_id, _strip_finance_opener(_confirm_copy(parsed, cat_name, from_receipt=from_receipt)), thread_id)
    return "finance_pending_confirm"


def _confirm_pending(
    pending: dict,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    payload = pending["payload"] or {}
    kind = payload.get("kind") or "tx"
    if kind == "savings_contribute":
        return _confirm_savings_contribute(pending, chat_id, thread_id, send)
    if kind == "savings_create":
        return _confirm_savings_create(pending, chat_id, thread_id, send)
    if kind == "fixed_upsert":
        return _confirm_fixed_upsert(pending, chat_id, thread_id, send)
    if kind == "set_budget":
        return _confirm_set_budget(pending, chat_id, thread_id, send)

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

    merchant = (payload.get("merchant") or "").strip()
    entity_ids: list[int] = []
    if merchant:
        try:
            from app import memory_store as mem_store

            eid = mem_store.upsert_entity(
                db_user_id=int(pending["user_id"]),
                entity_type="merchant",
                canonical_name=merchant,
                attrs={"source": "finance"},
            )
            if eid is not None:
                entity_ids = [int(eid)]
        except Exception as exc:
            logger.warning("finance_merchant_entity_error: %s", exc)

    tx_id = store.insert_transaction(
        user_id=pending["user_id"],
        category_id=cat_id,
        tx_type=tx_type,
        amount_mvr=amount,
        merchant=merchant,
        note=payload.get("note") or "",
        tx_date=tx_date,
        receipt_image_path=receipt_path,
        entity_ids=entity_ids or None,
    )
    if tx_id is None:
        send(chat_id, "Ugh — confirm worked but the save didn't. Want to try again?", thread_id)
        return "finance_insert_error"

    store.resolve_pending(pending["id"], "confirmed")
    amt = store.fmt_mvr(payload.get("amount_mvr"))
    cat = payload.get("category_name") or "Other"
    if tx_type == "income":
        tail = f" under {cat}" if cat and cat.lower() != "other" else ""
        send(
            chat_id,
            f"Logged — {amt} MVR income{tail}. Nice — "
            f"/flex whenever you want what's left for variable.",
            thread_id,
        )
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
                from decimal import Decimal
                spent_after = Decimal(str(spent_before)) + Decimal(str(amount))
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



def _handle_phase3(
    *,
    text: str,
    telegram_user_id: int,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> Optional[str]:
    """Route Phase 3 intents. Return reason or None if not actually handled."""
    if is_goals_list(text):
        return _handle_goals_list(telegram_user_id, chat_id, thread_id, send)
    if is_fixed_list(text):
        return _handle_fixed_list(telegram_user_id, chat_id, thread_id, send)
    if is_flex_query(text):
        return _handle_flex(telegram_user_id, chat_id, thread_id, send)
    digest_period = parse_digest_period(text)
    if digest_period is not None:
        return _handle_digest(telegram_user_id, digest_period, chat_id, thread_id, send)

    contrib = parse_savings_contribute(text)
    if contrib is not None:
        return _start_savings_contribute(
            contrib, telegram_user_id, chat_id, thread_id, send
        )
    goal = parse_new_goal(text)
    if goal is not None:
        return _start_savings_create(goal, telegram_user_id, chat_id, thread_id, send)
    fixed = parse_fixed_set(text)
    if fixed is not None:
        return _start_fixed_upsert(fixed, telegram_user_id, chat_id, thread_id, send)
    set_b = parse_set_budget(text)
    if set_b is not None:
        return _start_set_budget(set_b, telegram_user_id, chat_id, thread_id, send)
    return None


def _handle_goals_list(
    telegram_user_id: int, chat_id: str, thread_id: str, send: SendFn
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Couldn't peek at savings right now — try again shortly?", thread_id)
        return "finance_db_error"
    goals = store.list_savings_goals(user_id)
    send(chat_id, build_goals_list_text(goals, store.fmt_mvr), thread_id)
    return "finance_goals_list"


def _handle_fixed_list(
    telegram_user_id: int, chat_id: str, thread_id: str, send: SendFn
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Couldn't load fixed bills right now — give me another shot?", thread_id)
        return "finance_db_error"
    rows = store.list_fixed_expenses(user_id)
    total = store.sum_fixed_monthly(user_id)
    send(chat_id, build_fixed_list_text(rows, total, store.fmt_mvr), thread_id)
    return "finance_fixed_list"


def _handle_flex(
    telegram_user_id: int, chat_id: str, thread_id: str, send: SendFn
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Flex check failed on my end — try again in a sec?", thread_id)
        return "finance_db_error"
    msg = build_flex_text(
        fixed_total=store.sum_fixed_monthly(user_id),
        variable_spend=store.sum_variable_spend_month(user_id),
        income_month=store.sum_income_month(user_id),
        fmt_mvr=store.fmt_mvr,
    )
    send(chat_id, msg, thread_id)
    return "finance_flex"


def _handle_digest(
    telegram_user_id: int,
    period: str,
    chat_id: str,
    thread_id: str,
    send: SendFn,
) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Couldn't build your digest right now — try again shortly?", thread_id)
        return "finance_db_error"
    send(chat_id, render_digest_for_user(user_id, period), thread_id)
    return "finance_digest"


def render_digest_for_user(user_id: int, period: str = "month") -> str:
    """Shared digest renderer (on-demand /digest and scheduled jobs)."""
    period = "week" if (period or "").lower() == "week" else "month"
    total, count = store.sum_expenses(user_id, period)
    return build_digest_text(
        period=period,
        total_expense=total,
        expense_count=count,
        by_kind=store.sum_expenses_by_kind_period(user_id, period),
        top_categories=store.spend_by_category_period(user_id, period),
        fixed_obligations=store.sum_fixed_monthly(user_id),
        variable_spend=store.sum_variable_spend_month(user_id),
        goals=store.list_savings_goals(user_id),
        fmt_mvr=store.fmt_mvr,
    )


def _start_savings_contribute(contrib, telegram_user_id, chat_id, thread_id, send) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Hmm, I couldn't reach the books right now. Try again in a sec?", thread_id)
        return "finance_db_error"
    goal = store.find_savings_goal(user_id, contrib.goal_hint)
    if goal is None:
        send(
            chat_id,
            (
                f"I don't see a savings goal called {contrib.goal_hint} yet — "
                f"try new goal {contrib.goal_hint} 10000 first, or /goals to list them."
            ),
            thread_id,
        )
        return "finance_goal_missing"
    payload = {
        "kind": "savings_contribute",
        "goal_id": goal["id"],
        "goal_name": goal["name"],
        "amount_mvr": float(contrib.amount_mvr),
        "raw": contrib.raw,
    }
    pid = store.create_pending(chat_id, telegram_user_id, user_id, payload)
    if pid is None:
        send(chat_id, "Couldn't park that for confirmation — mind sending it again?", thread_id)
        return "finance_pending_error"
    amt = store.fmt_mvr(contrib.amount_mvr)
    cta = _soft_cta(contrib.amount_mvr, goal["name"], "contribute")
    send(
        chat_id,
        f"{amt} MVR toward {goal['name']} — {cta}",
        thread_id,
    )
    return "finance_pending_contribute"


def _confirm_savings_contribute(pending, chat_id, thread_id, send) -> str:
    payload = pending["payload"] or {}
    amount = float(payload.get("amount_mvr") or 0)
    goal_id = int(payload.get("goal_id"))
    snap = store.contribute_to_goal(pending["user_id"], goal_id, amount)
    if snap is None:
        send(chat_id, "Ugh — confirm worked but the save didn't. Want to try again?", thread_id)
        return "finance_contribute_error"
    store.resolve_pending(pending["id"], "confirmed")
    pct = 0
    try:
        from decimal import Decimal
        t = Decimal(str(snap["target_mvr"]))
        s = Decimal(str(snap["saved_mvr"]))
        if t > 0:
            pct = int((s / t * 100).quantize(Decimal("1")))
    except Exception:
        pct = 0
    send(
        chat_id,
        f"Parked — {store.fmt_mvr(amount)} MVR toward {snap['name']}. "
        f"You're at {store.fmt_mvr(snap['saved_mvr'])} / {store.fmt_mvr(snap['target_mvr'])} MVR ({pct}%).",
        thread_id,
    )
    return "finance_contribute_confirmed"


def _start_savings_create(goal, telegram_user_id, chat_id, thread_id, send) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Hmm, I couldn't reach the books right now. Try again in a sec?", thread_id)
        return "finance_db_error"
    payload = {
        "kind": "savings_create",
        "name": goal.name,
        "target_mvr": float(goal.target_mvr),
        "monthly_target_mvr": float(goal.monthly_target_mvr or 0),
        "raw": goal.raw,
    }
    pid = store.create_pending(chat_id, telegram_user_id, user_id, payload)
    if pid is None:
        send(chat_id, "Couldn't park that for confirmation — mind sending it again?", thread_id)
        return "finance_pending_error"
    amt = store.fmt_mvr(goal.target_mvr)
    cta = _soft_cta(goal.name, goal.target_mvr, "goal")
    if goal.monthly_target_mvr and goal.monthly_target_mvr > 0:
        msg = (
            f"New goal {goal.name} at {amt} MVR "
            f"(monthly {store.fmt_mvr(goal.monthly_target_mvr)}) — {cta}"
        )
    else:
        msg = f"New goal {goal.name} at {amt} MVR — {cta}"
    send(chat_id, msg, thread_id)
    return "finance_pending_goal"


def _confirm_savings_create(pending, chat_id, thread_id, send) -> str:
    payload = pending["payload"] or {}
    gid = store.create_savings_goal(
        pending["user_id"],
        payload.get("name") or "",
        float(payload.get("target_mvr") or 0),
        float(payload.get("monthly_target_mvr") or 0),
    )
    if gid is None:
        send(chat_id, "Ugh — confirm worked but creating the goal didn't. Want to try again?", thread_id)
        return "finance_goal_create_error"
    store.resolve_pending(pending["id"], "confirmed")
    name = payload.get("name") or "goal"
    send(
        chat_id,
        f"Goal set — {name} at {store.fmt_mvr(payload.get('target_mvr'))} MVR. "
        f"Drop money in anytime — just say save 200 toward {name}.",
        thread_id,
    )
    return "finance_goal_created"


def _start_fixed_upsert(fixed, telegram_user_id, chat_id, thread_id, send) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Hmm, I couldn't reach the books right now. Try again in a sec?", thread_id)
        return "finance_db_error"
    # Title-case common names for category (Rent, Utilities)
    cat_hint = fixed.name.strip().title()
    payload = {
        "kind": "fixed_upsert",
        "name": fixed.name.strip(),
        "amount_mvr": float(fixed.amount_mvr),
        "category_hint": cat_hint,
        "raw": fixed.raw,
    }
    pid = store.create_pending(chat_id, telegram_user_id, user_id, payload)
    if pid is None:
        send(chat_id, "Couldn't park that for confirmation — mind sending it again?", thread_id)
        return "finance_pending_error"
    amt = store.fmt_mvr(fixed.amount_mvr)
    cta = _soft_cta(fixed.name, fixed.amount_mvr, "fixed")
    send(chat_id, f"Fixed: {fixed.name} at {amt} MVR/month — {cta}", thread_id)
    return "finance_pending_fixed"


def _confirm_fixed_upsert(pending, chat_id, thread_id, send) -> str:
    payload = pending["payload"] or {}
    name = (payload.get("name") or "").strip()
    amount = float(payload.get("amount_mvr") or 0)
    cat_hint = payload.get("category_hint") or name.title()
    cat_id, cat_name = store.ensure_category_kind(
        pending["user_id"], cat_hint, "fixed"
    )
    fid = store.upsert_fixed_expense(
        pending["user_id"], name, amount, category_id=cat_id
    )
    if fid is None:
        send(chat_id, "Ugh — confirm worked but saving the fixed bill didn't. Want to try again?", thread_id)
        return "finance_fixed_error"
    store.resolve_pending(pending["id"], "confirmed")
    send(
        chat_id,
        f"Logged fixed — {name} at {store.fmt_mvr(amount)} MVR/month "
        f"(category {cat_name}, marked fixed). /fixed to see the full list.",
        thread_id,
    )
    return "finance_fixed_confirmed"


def _start_set_budget(parsed, telegram_user_id, chat_id, thread_id, send) -> str:
    user_id = store.ensure_user_row(telegram_user_id)
    if user_id is None:
        send(chat_id, "Hmm, I couldn't reach the books right now. Try again in a sec?", thread_id)
        return "finance_db_error"
    cat_id, cat_name = store.resolve_category_id(user_id, parsed.category_hint)
    if cat_id is None:
        send(chat_id, "Couldn't find that category — try Food, Transport, Rent, etc.", thread_id)
        return "finance_budget_cat_missing"
    payload = {
        "kind": "set_budget",
        "category_id": cat_id,
        "category_name": cat_name,
        "category_hint": parsed.category_hint,
        "amount_mvr": float(parsed.amount_mvr),
        "raw": parsed.raw,
    }
    pid = store.create_pending(chat_id, telegram_user_id, user_id, payload)
    if pid is None:
        send(chat_id, "Couldn't park that for confirmation — mind sending it again?", thread_id)
        return "finance_pending_error"
    amt = store.fmt_mvr(parsed.amount_mvr)
    seed = f"{cat_name}|{parsed.amount_mvr}|budget"
    cta = _BUDGET_CTAS[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(_BUDGET_CTAS)]
    send(
        chat_id,
        f"Cap {cat_name} at {amt} MVR/month — {cta}",
        thread_id,
    )
    return "finance_pending_set_budget"


def _confirm_set_budget(pending, chat_id, thread_id, send) -> str:
    payload = pending["payload"] or {}
    amount = payload.get("amount_mvr") or 0
    hint = payload.get("category_hint") or payload.get("category_name") or "Other"
    snap = store.set_category_monthly_limit(pending["user_id"], hint, amount)
    if snap is None:
        send(chat_id, "Ugh — confirm worked but saving the limit didn't. Want to try again?", thread_id)
        return "finance_set_budget_error"
    store.resolve_pending(pending["id"], "confirmed")
    send(
        chat_id,
        f"Got it — {snap['name']} is capped at {store.fmt_mvr(snap['monthly_limit_mvr'])} MVR/month. "
        f"Say /budget anytime to see how you're tracking.",
        thread_id,
    )
    return "finance_set_budget_confirmed"

