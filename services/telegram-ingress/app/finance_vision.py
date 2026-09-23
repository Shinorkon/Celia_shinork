"""Receipt vision via Celia LiteLLM (Gemini) — JSON-only extraction."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://localhost:4000").rstrip("/")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
VISION_MODEL = os.getenv("FINANCE_VISION_MODEL", "gemini-2.5-flash")

_RECEIPT_PROMPT = (
    "You are reading a receipt or bill photo for personal expense tracking in the Maldives (MVR). "
    "Return ONLY a single JSON object with these keys (no markdown, no commentary):\n"
    '{"amount": <number>, "merchant": <string>, "date": <"YYYY-MM-DD" or null>, '
    '"category_hint": <string>, "note": <string>, "confidence": <number 0-1>}\n'
    "Rules: amount is the grand total paid only (not line items, not GST alone). "
    "merchant is the store/vendor name only — never invoice numbers, cashier names, "
    "payment method, card type, or date-format commentary. "
    "category_hint is a short label like Food, Transport, Utilities, Shopping, Health, Other. "
    "note must be empty or a very short useful label (e.g. groceries). "
    "NEVER put invoice/cashier/paid-by/GST/line-item lists/date-parse essays in note or merchant. "
    "Do NOT list or describe individual items. "
    "confidence reflects how sure you are this is a readable receipt with a clear total. "
    "If not a receipt or unreadable, set confidence low and amount to null."
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


@dataclass
class ReceiptVisionResult:
    amount: Optional[float]
    merchant: str
    date: Optional[str]  # YYYY-MM-DD
    category_hint: str
    note: str
    confidence: float
    raw_response: str = ""


def parse_vision_json(content: str) -> Optional[ReceiptVisionResult]:
    """Parse model text into ReceiptVisionResult. Pure — safe to unit-test."""
    if not content or not str(content).strip():
        return None
    text = str(content).strip()
    fence = _JSON_FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start : end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        try:
            cleaned = re.sub(r",\s*}", "}", blob)
            cleaned = re.sub(r",\s*]", "]", cleaned)
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("finance_vision_json_parse_fail")
            return None
    if not isinstance(data, dict):
        return None

    amount = _as_amount(data.get("amount"))
    conf = _as_confidence(data.get("confidence"))
    merchant = _clean_merchant(_as_str(data.get("merchant")))
    note = _clean_note(_as_str(data.get("note")))
    category_hint = _as_str(data.get("category_hint")) or "Other"
    if _FLUFF_RE.search(category_hint):
        category_hint = "Other"
    date_s = _as_date(data.get("date"))

    return ReceiptVisionResult(
        amount=amount,
        merchant=merchant,
        date=date_s,
        category_hint=category_hint,
        note=note,
        confidence=conf,
        raw_response=str(content)[:2000],
    )


def extract_receipt_from_image(image_data_url: str) -> Optional[ReceiptVisionResult]:
    """Call LiteLLM chat completions with image_url content block."""
    if not image_data_url:
        return None
    if not LITELLM_API_KEY:
        logger.error("finance_vision_missing_litellm_key")
        return None

    body: dict[str, Any] = {
        "model": VISION_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _RECEIPT_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ],
    }
    payload = json.dumps(body).encode("utf-8")
    url = f"{LITELLM_BASE_URL}/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
        content = (
            parsed.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text") or "")
                elif isinstance(p, str):
                    parts.append(p)
            content = "\n".join(parts)
        result = parse_vision_json(content or "")
        if result is None:
            logger.warning("finance_vision_empty_or_unparsed")
        return result
    except urllib.error.HTTPError as exc:
        body_txt = ""
        try:
            body_txt = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        logger.error(f"finance_vision_http_error: status={exc.code} body={body_txt}")
        return None
    except Exception as exc:
        logger.error(f"finance_vision_error: {exc}")
        return None


def _as_amount(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        if isinstance(val, str):
            val = val.replace(",", "").replace("MVR", "").replace("Rf", "").strip()
        n = float(val)
        if n <= 0:
            return None
        return round(n, 2)
    except (TypeError, ValueError):
        return None


def _as_confidence(val: Any) -> float:
    try:
        c = float(val)
    except (TypeError, ValueError):
        return 0.0
    if c > 1.0 and c <= 100.0:
        c = c / 100.0
    return max(0.0, min(1.0, c))


_FLUFF_RE = re.compile(
    r"(invoice|cashier|paid\s*by|payment\s*method|\bgst\b|\bvat\b|"
    r"tax\s*invoice|receipt\s*no|txn\s*id|transaction\s*id|card\s*ending|"
    r"change\s*due|subtotal|line\s*items?|date\s*format|yyyy\s*-\s*mm)",
    re.I,
)


def _clean_merchant(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if _FLUFF_RE.search(s):
        for sep in (" — ", " – ", " - ", " | ", ",", ";", " / "):
            if sep in s:
                s = s.split(sep, 1)[0].strip()
                break
        s = re.split(r"\b(?:invoice|cashier|paid|payment|gst|vat)\b", s, maxsplit=1, flags=re.I)[0]
        s = s.strip(" -,|;:/")
    return s[:80]


def _clean_note(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if _FLUFF_RE.search(s) or len(s) > 48:
        return ""
    # Receipt meta often looks like "Invoice: 3/157452"
    if re.search(r"^[A-Za-z ]{3,20}:\s*\S+", s):
        return ""
    return s[:40]


def _as_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()[:120]


def _as_date(val: Any) -> Optional[str]:
    if val is None or val == "":
        return None
    s = str(val).strip()[:32]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return None
