"""Warm Carliabot finance digest text builders (week / month)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable


def _pct(saved: Any, target: Any) -> int:
    try:
        s = Decimal(str(saved))
        t = Decimal(str(target))
        if t <= 0:
            return 0
        return int((s / t * 100).quantize(Decimal("1")))
    except Exception:
        return 0


def build_digest_text(
    *,
    period: str,
    total_expense: Any,
    expense_count: int,
    by_kind: dict[str, Any],
    top_categories: list[dict[str, Any]],
    fixed_obligations: Any,
    variable_spend: Any,
    goals: list[dict[str, Any]],
    fmt_mvr: Callable[[Any], str],
) -> str:
    """Compose a warm weekly/monthly spending summary."""
    period = (period or "month").lower()
    label = "this week" if period == "week" else "this month"
    lines: list[str] = []

    if expense_count == 0:
        lines.append(f"Quiet {label} on the spending front — nothing logged yet.")
    else:
        lines.append(
            f"Here's your {label} money snapshot — "
            f"{fmt_mvr(total_expense)} MVR across {expense_count} expense"
            f"{'s' if expense_count != 1 else ''}."
        )

    fixed_spent = by_kind.get("fixed") or Decimal("0")
    variable_spent = by_kind.get("variable") or Decimal("0")
    if expense_count > 0:
        lines.append(
            f"Fixed vs variable spend: {fmt_mvr(fixed_spent)} MVR fixed, "
            f"{fmt_mvr(variable_spent)} MVR variable."
        )

    if top_categories:
        bits = [
            f"{c['name']} {fmt_mvr(c['spent'])}"
            for c in top_categories[:5]
            if Decimal(str(c.get("spent") or 0)) > 0
        ]
        if bits:
            lines.append("Top categories: " + ", ".join(bits) + ".")

    fixed_ob = Decimal(str(fixed_obligations or 0))
    var_so_far = Decimal(str(variable_spend or 0))
    if fixed_ob > 0:
        lines.append(
            f"Fixed obligations on the books: {fmt_mvr(fixed_ob)} MVR/month. "
            f"Variable spend so far {label}: {fmt_mvr(var_so_far)} MVR."
        )

    active_goals = [g for g in (goals or []) if g.get("is_active", True)]
    if active_goals:
        goal_bits = []
        for g in active_goals[:5]:
            pct = _pct(g.get("saved_mvr"), g.get("target_mvr"))
            goal_bits.append(
                f"{g['name']} {fmt_mvr(g.get('saved_mvr'))}/"
                f"{fmt_mvr(g.get('target_mvr'))} ({pct}%)"
            )
        lines.append("Savings: " + "; ".join(goal_bits) + ".")
    elif expense_count > 0:
        lines.append("No savings goals yet — say new goal emergency 10000 when you want one.")

    lines.append("Ping /digest anytime if you want this on demand.")
    return "\n".join(lines)


def build_goals_list_text(goals: list[dict[str, Any]], fmt_mvr: Callable[[Any], str]) -> str:
    if not goals:
        return (
            "No savings goals yet — try new goal emergency 10000 "
            "or goal vacation 5000 monthly 500."
        )
    lines = ["Here's where your savings stand:"]
    for g in goals:
        saved = g.get("saved_mvr") or 0
        target = g.get("target_mvr") or 0
        pct = _pct(saved, target)
        monthly = g.get("monthly_target_mvr") or 0
        left = Decimal(str(target)) - Decimal(str(saved))
        bit = (
            f"· {g['name']}: {fmt_mvr(saved)} of {fmt_mvr(target)} MVR "
            f"({pct}%)"
        )
        if left > 0:
            bit += f" — {fmt_mvr(left)} to go"
        else:
            bit += " — goal met, nice"
        if Decimal(str(monthly)) > 0:
            bit += f" (aiming {fmt_mvr(monthly)}/mo)"
        lines.append(bit)
    return "\n".join(lines)


def build_fixed_list_text(
    rows: list[dict[str, Any]], total: Any, fmt_mvr: Callable[[Any], str]
) -> str:
    if not rows:
        return (
            "No fixed monthly obligations logged yet — "
            "try fixed rent 12000 and I'll track them."
        )
    lines = ["Your fixed monthly obligations:"]
    for r in rows:
        cat = r.get("category_name") or ""
        tail = f" ({cat})" if cat else ""
        lines.append(f"· {r['name']}: {fmt_mvr(r['amount_mvr'])} MVR/mo{tail}")
    lines.append(f"Total fixed: {fmt_mvr(total)} MVR/month.")
    return "\n".join(lines)


def build_flex_text(
    *,
    fixed_total: Any,
    variable_spend: Any,
    income_month: Any,
    fmt_mvr: Callable[[Any], str],
) -> str:
    fixed_total = Decimal(str(fixed_total or 0))
    variable_spend = Decimal(str(variable_spend or 0))
    income_month = Decimal(str(income_month or 0))

    # Warm Carlia — never invent income/fixed amounts; Decimal-safe throughout
    if fixed_total <= 0 and income_month <= 0 and variable_spend <= 0:
        return (
            "Nothing on the books yet for flex — "
            "log income when it lands, or a fixed bill when you want, "
            "then /flex will show what's left for variable."
        )

    parts: list[str] = []
    if fixed_total > 0:
        parts.append(f"Fixed so far: {fmt_mvr(fixed_total)} MVR/month.")
    else:
        parts.append("No fixed obligations logged yet.")

    parts.append(f"Variable spend this month: {fmt_mvr(variable_spend)}.")

    if income_month > 0:
        left = income_month - fixed_total - variable_spend
        parts.append(f"Income logged this month: {fmt_mvr(income_month)}.")
        if left >= 0:
            parts.append(
                f"That leaves about {fmt_mvr(left)} for variable if income holds."
            )
        else:
            over = -left
            parts.append(
                f"That's about {fmt_mvr(over)} over vs income so far — worth a peek."
            )
    else:
        parts.append(
            "No income logged this month yet — "
            "log it when it lands and I'll show what's left."
        )
    return " ".join(parts)
