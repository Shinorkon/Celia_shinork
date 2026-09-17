"""Pure budget-alert threshold helpers (unit-testable)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional


def alert_crossed(
    spent_before: Any,
    amount: Any,
    limit: Any,
) -> Optional[str]:
    """Return '100' or '80' if this expense newly crosses that % of limit.

    Prefer '100' when both thresholds are newly crossed in one tx.
    Returns None if limit <= 0 or already at/over the threshold before.
    """
    try:
        before = Decimal(str(spent_before))
        amt = Decimal(str(amount))
        lim = Decimal(str(limit))
    except Exception:
        return None
    if lim <= 0 or amt <= 0:
        return None
    after = before + amt
    # Newly hit/crossed 100%
    if before < lim <= after:
        return "100"
    # Newly hit/crossed 80% (and not already past 80)
    eighty = lim * Decimal("0.8")
    if before < eighty <= after:
        return "80"
    return None


def format_budget_alert(
    category_name: str,
    spent_after: Any,
    limit: Any,
    level: str,
    fmt_mvr,
) -> str:
    """Warm Carliabot copy for post-save budget heads-up."""
    name = category_name or "That category"
    spent_s = fmt_mvr(spent_after)
    limit_s = fmt_mvr(limit)
    try:
        lim = Decimal(str(limit))
        after = Decimal(str(spent_after))
        pct = int((after / lim * 100).quantize(Decimal("1"))) if lim > 0 else 0
    except Exception:
        pct = 0
    if level == "100":
        return (
            f"{name}'s tapped out for the month — {limit_s} MVR limit hit "
            f"({spent_s} / {limit_s} MVR)."
        )
    return (
        f"Heads up — {name} is at {pct}% of its monthly limit "
        f"({spent_s} / {limit_s} MVR)."
    )
