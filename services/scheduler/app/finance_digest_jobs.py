"""Finance digest publisher — weekly + monthly → notification.requested."""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import psycopg
from redis import Redis

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
NOTIFICATION_STREAM = os.getenv("NOTIFICATION_STREAM", "notification.requested")

WEEKLY_JOB_ID = "finance-digest-weekly"
MONTHLY_JOB_ID = "finance-digest-monthly"


def _conn():
    return psycopg.connect(DATABASE_URL)


def _redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def fmt_mvr(amount: Any) -> str:
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
        return f"{d:.2f}"
    except Exception:
        return str(amount)


def _list_targets() -> list[dict[str, int]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT u.id, u.telegram_user_id
                    FROM users u
                    WHERE u.is_active = TRUE
                      AND u.telegram_user_id IS NOT NULL
                      AND (
                        EXISTS (
                          SELECT 1 FROM finance_transactions t WHERE t.user_id = u.id
                        )
                        OR EXISTS (
                          SELECT 1 FROM finance_savings_goals g WHERE g.user_id = u.id
                        )
                        OR EXISTS (
                          SELECT 1 FROM finance_fixed_expenses f WHERE f.user_id = u.id
                        )
                        OR u.role IN ('authorized', 'admin', 'owner')
                      )
                    ORDER BY u.id
                    """
                )
                rows = cur.fetchall()
        return [
            {"user_id": int(r[0]), "telegram_user_id": int(r[1])}
            for r in rows
            if r[1] is not None
        ]
    except Exception as exc:
        logger.error(f"finance_digest_targets_error: {exc}")
        return []


def _sum_expenses(user_id: int, period: str) -> tuple[Decimal, int]:
    if period == "week":
        where = "tx_date >= date_trunc('week', CURRENT_DATE)::date"
    else:
        where = "tx_date >= date_trunc('month', CURRENT_DATE)::date"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(amount_mvr), 0), COUNT(*)
                    FROM finance_transactions
                    WHERE user_id = %s AND tx_type = 'expense' AND {where}
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return Decimal(str(row[0])), int(row[1])
    except Exception as exc:
        logger.error(f"finance_digest_sum_error: {exc}")
        return Decimal("0"), 0


def _by_kind(user_id: int, period: str) -> dict[str, Decimal]:
    if period == "week":
        where = "t.tx_date >= date_trunc('week', CURRENT_DATE)::date"
    else:
        where = "t.tx_date >= date_trunc('month', CURRENT_DATE)::date"
    out = {"fixed": Decimal("0"), "variable": Decimal("0"), "other": Decimal("0")}
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(c.kind, 'variable'), COALESCE(SUM(t.amount_mvr), 0)
                    FROM finance_transactions t
                    LEFT JOIN finance_categories c ON c.id = t.category_id
                    WHERE t.user_id = %s AND t.tx_type = 'expense' AND {where}
                    GROUP BY COALESCE(c.kind, 'variable')
                    """,
                    (user_id,),
                )
                for kind, spent in cur.fetchall():
                    k = (kind or "variable").lower()
                    if k in out:
                        out[k] = Decimal(str(spent))
                    else:
                        out["other"] += Decimal(str(spent))
    except Exception as exc:
        logger.error(f"finance_digest_kind_error: {exc}")
    return out


def _top_cats(user_id: int, period: str) -> list[dict[str, Any]]:
    if period == "week":
        where = "t.tx_date >= date_trunc('week', CURRENT_DATE)::date"
    else:
        where = "t.tx_date >= date_trunc('month', CURRENT_DATE)::date"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(c.name, 'Other'), COALESCE(SUM(t.amount_mvr), 0)
                    FROM finance_transactions t
                    LEFT JOIN finance_categories c ON c.id = t.category_id
                    WHERE t.user_id = %s AND t.tx_type = 'expense' AND {where}
                    GROUP BY COALESCE(c.name, 'Other')
                    ORDER BY 2 DESC
                    LIMIT 5
                    """,
                    (user_id,),
                )
                return [{"name": r[0], "spent": Decimal(str(r[1]))} for r in cur.fetchall()]
    except Exception as exc:
        logger.error(f"finance_digest_cats_error: {exc}")
        return []


def _fixed_total(user_id: int) -> Decimal:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount_mvr), 0)
                    FROM finance_fixed_expenses
                    WHERE user_id = %s AND is_active = TRUE
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return Decimal(str(row[0])) if row else Decimal("0")
    except Exception:
        return Decimal("0")


def _variable_month(user_id: int) -> Decimal:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(t.amount_mvr), 0)
                    FROM finance_transactions t
                    LEFT JOIN finance_categories c ON c.id = t.category_id
                    WHERE t.user_id = %s AND t.tx_type = 'expense'
                      AND t.tx_date >= date_trunc('month', CURRENT_DATE)::date
                      AND (c.kind IS NULL OR c.kind = 'variable')
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return Decimal(str(row[0])) if row else Decimal("0")
    except Exception:
        return Decimal("0")


def _goals(user_id: int) -> list[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT name, target_mvr, saved_mvr, is_active
                    FROM finance_savings_goals
                    WHERE user_id = %s AND is_active = TRUE
                    ORDER BY created_at
                    """,
                    (user_id,),
                )
                return [
                    {
                        "name": r[0],
                        "target_mvr": Decimal(str(r[1])),
                        "saved_mvr": Decimal(str(r[2])),
                        "is_active": bool(r[3]),
                    }
                    for r in cur.fetchall()
                ]
    except Exception:
        return []


def build_digest_text(user_id: int, period: str) -> str:
    period = "week" if period == "week" else "month"
    label = "this week" if period == "week" else "this month"
    total, count = _sum_expenses(user_id, period)
    by_kind = _by_kind(user_id, period)
    top = _top_cats(user_id, period)
    fixed_ob = _fixed_total(user_id)
    var_so_far = _variable_month(user_id)
    goals = _goals(user_id)

    lines: list[str] = []
    if count == 0:
        lines.append(f"Quiet {label} on the spending front — nothing logged yet.")
    else:
        lines.append(
            f"Here's your {label} money snapshot — "
            f"{fmt_mvr(total)} MVR across {count} expense"
            f"{'s' if count != 1 else ''}."
        )
        lines.append(
            f"Fixed vs variable spend: {fmt_mvr(by_kind.get('fixed', 0))} MVR fixed, "
            f"{fmt_mvr(by_kind.get('variable', 0))} MVR variable."
        )
    if top:
        bits = [f"{c['name']} {fmt_mvr(c['spent'])}" for c in top if c["spent"] > 0]
        if bits:
            lines.append("Top categories: " + ", ".join(bits) + ".")
    if fixed_ob > 0:
        lines.append(
            f"Fixed obligations on the books: {fmt_mvr(fixed_ob)} MVR/month. "
            f"Variable spend so far {label}: {fmt_mvr(var_so_far)} MVR."
        )
    if goals:
        goal_bits = []
        for g in goals[:5]:
            try:
                pct = int((g["saved_mvr"] / g["target_mvr"] * 100).quantize(Decimal("1"))) if g["target_mvr"] > 0 else 0
            except Exception:
                pct = 0
            goal_bits.append(
                f"{g['name']} {fmt_mvr(g['saved_mvr'])}/{fmt_mvr(g['target_mvr'])} ({pct}%)"
            )
        lines.append("Savings: " + "; ".join(goal_bits) + ".")
    lines.append("Ping /digest anytime if you want this on demand.")
    return "\n".join(lines)


def _publish(chat_id: str, text: str) -> None:
    r = _redis()
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": str(uuid.uuid4()),
        "target_user_id": chat_id,
        "chat_id": chat_id,
        "thread_id": "",
        "text": text,
        "priority": "normal",
        "kind": "finance_digest",
    }
    r.xadd(NOTIFICATION_STREAM, {"payload": json.dumps(event)})


def publish_finance_digests(period: str) -> int:
    """Build + publish digests for all finance targets. Returns send count."""
    period = "week" if period == "week" else "month"
    targets = _list_targets()
    sent = 0
    for t in targets:
        chat_id = str(t["telegram_user_id"])
        try:
            text = build_digest_text(t["user_id"], period)
            _publish(chat_id, text)
            sent += 1
            logger.info(
                f"finance_digest_published: period={period} user_id={t['user_id']} chat_id={chat_id}"
            )
        except Exception as exc:
            logger.error(
                f"finance_digest_publish_error: user_id={t['user_id']} err={exc}"
            )
    return sent


def weekly_digest_job() -> None:
    publish_finance_digests("week")


def monthly_digest_job() -> None:
    publish_finance_digests("month")


def register_finance_digest_jobs(scheduler) -> None:
    """Idempotent cron registration on APScheduler (UTC).

    Weekly Sunday 18:00 Indian/Maldives (UTC+5) → 13:00 UTC
    Monthly 1st 09:00 MVT → 04:00 UTC
    """
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        weekly_digest_job,
        trigger=CronTrigger(
            day_of_week="sun", hour=13, minute=0, timezone="UTC"
        ),
        id=WEEKLY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monthly_digest_job,
        trigger=CronTrigger(day=1, hour=4, minute=0, timezone="UTC"),
        id=MONTHLY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "finance_digest_jobs_registered: weekly=%s monthly=%s",
        WEEKLY_JOB_ID,
        MONTHLY_JOB_ID,
    )
