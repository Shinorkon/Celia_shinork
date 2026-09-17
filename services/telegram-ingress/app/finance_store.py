"""DB helpers for Celia-native finance (agent_platform / DATABASE_URL)."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("Food", "variable"),
    ("Transport", "variable"),
    ("Rent", "fixed"),
    ("Utilities", "variable"),
    ("Health", "variable"),
    ("Shopping", "variable"),
    ("Entertainment", "variable"),
    ("Other", "variable"),
    ("Salary", "income"),
]

PENDING_TTL_MINUTES = 30


def _conn():
    return psycopg.connect(DATABASE_URL)


def ensure_user_row(telegram_user_id: int) -> Optional[int]:
    """Ensure users row exists; return users.id."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users(telegram_user_id, role, is_active)
                    VALUES (%s, 'visitor', TRUE)
                    ON CONFLICT (telegram_user_id) DO NOTHING
                    """,
                    (telegram_user_id,),
                )
                cur.execute(
                    "SELECT id FROM users WHERE telegram_user_id = %s",
                    (telegram_user_id,),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.error(f"finance_ensure_user_error: {exc}")
        return None


def seed_default_categories(user_id: int) -> None:
    """Idempotent default category seed for one user."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                for name, kind in DEFAULT_CATEGORIES:
                    cur.execute(
                        """
                        INSERT INTO finance_categories(user_id, name, kind, monthly_limit_mvr)
                        VALUES (%s, %s, %s, 0)
                        ON CONFLICT (user_id, name) DO NOTHING
                        """,
                        (user_id, name, kind),
                    )
            conn.commit()
    except Exception as exc:
        logger.error(f"finance_seed_categories_error: user_id={user_id} err={exc}")


def seed_defaults_for_all_users() -> int:
    """Seed defaults for every users row. Returns user count touched."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users")
                ids = [int(r[0]) for r in cur.fetchall()]
            conn.commit()
        for uid in ids:
            seed_default_categories(uid)
        return len(ids)
    except Exception as exc:
        logger.error(f"finance_seed_all_error: {exc}")
        return 0


def resolve_category_id(user_id: int, hint: str) -> tuple[Optional[int], str]:
    """Match category by case-insensitive contains / fuzzy; fallback Other."""
    hint = (hint or "Other").strip()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name FROM finance_categories
                    WHERE user_id = %s AND is_active = TRUE
                    ORDER BY id
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        if not rows:
            seed_default_categories(user_id)
            return resolve_category_id(user_id, hint)

        low = hint.lower()
        for cid, name in rows:
            if name.lower() == low:
                return int(cid), name
        for cid, name in rows:
            n = name.lower()
            if low in n or n in low:
                return int(cid), name
        best = None
        best_score = 0
        for cid, name in rows:
            n = name.lower()
            score = 0
            if n.startswith(low[:3]) or low.startswith(n[:3]):
                score = 2
            overlap = len(set(n) & set(low))
            score += overlap
            if score > best_score and (low[:3] in n or n[:3] in low or overlap >= 4):
                best_score = score
                best = (int(cid), name)
        if best:
            return best

        for cid, name in rows:
            if name.lower() == "other":
                return int(cid), name
        return int(rows[0][0]), rows[0][1]
    except Exception as exc:
        logger.error(f"finance_resolve_category_error: {exc}")
        return None, "Other"


def create_pending(
    chat_id: str,
    telegram_user_id: int,
    user_id: int,
    payload: dict[str, Any],
) -> Optional[int]:
    """Create pending confirm row; expire older pendings for same chat."""
    expires = datetime.now(timezone.utc) + timedelta(minutes=PENDING_TTL_MINUTES)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE finance_pending_logs
                    SET status = 'expired', resolved_at = NOW()
                    WHERE chat_id = %s AND status = 'pending'
                    """,
                    (chat_id,),
                )
                cur.execute(
                    """
                    INSERT INTO finance_pending_logs(
                        chat_id, telegram_user_id, user_id, payload_json, status, expires_at
                    ) VALUES (%s, %s, %s, %s::jsonb, 'pending', %s)
                    RETURNING id
                    """,
                    (
                        chat_id,
                        telegram_user_id,
                        user_id,
                        json.dumps(payload),
                        expires,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.error(f"finance_create_pending_error: {exc}")
        return None


def get_pending(chat_id: str) -> Optional[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, payload_json, telegram_user_id
                    FROM finance_pending_logs
                    WHERE chat_id = %s AND status = 'pending' AND expires_at > NOW()
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (chat_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        payload = row[2]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "id": int(row[0]),
            "user_id": int(row[1]),
            "payload": payload,
            "telegram_user_id": int(row[3]),
        }
    except Exception as exc:
        logger.error(f"finance_get_pending_error: {exc}")
        return None


def resolve_pending(pending_id: int, status: str) -> None:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE finance_pending_logs
                    SET status = %s, resolved_at = NOW()
                    WHERE id = %s AND status = 'pending'
                    """,
                    (status, pending_id),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"finance_resolve_pending_error: {exc}")


def insert_transaction(
    user_id: int,
    category_id: Optional[int],
    tx_type: str,
    amount_mvr: float,
    merchant: str = "",
    note: str = "",
    tx_date: Optional[date] = None,
    receipt_image_path: Optional[str] = None,
) -> Optional[int]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance_transactions(
                        user_id, category_id, tx_type, amount_mvr, merchant, note,
                        tx_date, receipt_image_path
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, CURRENT_DATE), %s
                    )
                    RETURNING id
                    """,
                    (
                        user_id,
                        category_id,
                        tx_type,
                        Decimal(str(round(amount_mvr, 2))),
                        merchant or "",
                        note or "",
                        tx_date,
                        receipt_image_path,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.error(f"finance_insert_tx_error: {exc}")
        return None


def category_month_spend_and_limit(
    user_id: int, category_id: Optional[int]
) -> Optional[dict[str, Any]]:
    """Return {name, spent, limit} for one category this calendar month."""
    if category_id is None:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.name, c.monthly_limit_mvr,
                           COALESCE((
                             SELECT SUM(t.amount_mvr)
                             FROM finance_transactions t
                             WHERE t.category_id = c.id
                               AND t.user_id = c.user_id
                               AND t.tx_type = 'expense'
                               AND t.tx_date >= date_trunc('month', CURRENT_DATE)::date
                           ), 0) AS spent
                    FROM finance_categories c
                    WHERE c.id = %s AND c.user_id = %s
                    """,
                    (category_id, user_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {
            "name": row[0],
            "limit": Decimal(str(row[1])),
            "spent": Decimal(str(row[2])),
        }
    except Exception as exc:
        logger.error(f"finance_category_spend_error: {exc}")
        return None


def sum_expenses(
    user_id: int, period: str = "month"
) -> tuple[Decimal, int]:
    """Return (total, count) for expenses in today|week|month."""
    period = (period or "month").lower()
    if period == "today":
        where = "tx_date = CURRENT_DATE"
    elif period == "week":
        where = "tx_date >= date_trunc('week', CURRENT_DATE)::date"
    else:
        where = "tx_date >= date_trunc('month', CURRENT_DATE)::date"
        period = "month"
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
        logger.error(f"finance_sum_expenses_error: {exc}")
        return Decimal("0"), 0


def budget_snapshot(user_id: int) -> list[dict[str, Any]]:
    """Categories with monthly_limit_mvr > 0 vs spent this month."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.name, c.monthly_limit_mvr,
                           COALESCE(SUM(t.amount_mvr), 0) AS spent
                    FROM finance_categories c
                    LEFT JOIN finance_transactions t
                      ON t.category_id = c.id
                     AND t.user_id = c.user_id
                     AND t.tx_type = 'expense'
                     AND t.tx_date >= date_trunc('month', CURRENT_DATE)::date
                    WHERE c.user_id = %s AND c.is_active = TRUE AND c.monthly_limit_mvr > 0
                    GROUP BY c.id, c.name, c.monthly_limit_mvr
                    ORDER BY c.name
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "name": r[1],
                "limit": Decimal(str(r[2])),
                "spent": Decimal(str(r[3])),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error(f"finance_budget_snapshot_error: {exc}")
        return []


def fmt_mvr(amount: Any) -> str:
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
        return f"{d:.2f}"
    except Exception:
        return str(amount)
