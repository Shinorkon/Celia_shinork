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



def _as_chat_id(chat_id) -> str:
    """Normalize Telegram chat id to text for finance_pending_logs.chat_id (TEXT)."""
    if chat_id is None:
        return ""
    return str(chat_id).strip()


def create_pending(
    chat_id: str,
    telegram_user_id: int,
    user_id: int,
    payload: dict[str, Any],
) -> Optional[int]:
    """Create pending confirm row; expire older pendings for same chat."""
    chat_id = _as_chat_id(chat_id)
    expires = datetime.now(timezone.utc) + timedelta(minutes=PENDING_TTL_MINUTES)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE finance_pending_logs
                    SET status = 'expired', resolved_at = NOW()
                    WHERE chat_id = %s::text AND status = 'pending'
                    """,
                    (chat_id,),
                )
                cur.execute(
                    """
                    INSERT INTO finance_pending_logs(
                        chat_id, telegram_user_id, user_id, payload_json, status, expires_at
                    ) VALUES (%s::text, %s, %s, %s::jsonb, 'pending', %s)
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
    chat_id = _as_chat_id(chat_id)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, payload_json, telegram_user_id
                    FROM finance_pending_logs
                    WHERE chat_id = %s::text AND status = 'pending' AND expires_at > NOW()
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



def clear_pending_for_chat(chat_id: str, status: str = "cancelled") -> int:
    """Cancel/expire all pending rows for a chat. Returns rows affected."""
    chat_id = _as_chat_id(chat_id)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE finance_pending_logs
                    SET status = %s, resolved_at = NOW()
                    WHERE chat_id = %s::text AND status = 'pending'
                    """,
                    (status, chat_id),
                )
                n = cur.rowcount or 0
            conn.commit()
        return int(n)
    except Exception as exc:
        logger.error(f"finance_clear_pending_error: {exc}")
        return 0


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
    entity_ids: Optional[list[int]] = None,
) -> Optional[int]:
    ids = [int(x) for x in (entity_ids or []) if x is not None]
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance_transactions(
                        user_id, category_id, tx_type, amount_mvr, merchant, note,
                        tx_date, receipt_image_path, entity_ids
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, CURRENT_DATE), %s, %s
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
                        ids,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        # Pre-migration fallback: column may not exist yet.
        if "entity_ids" in str(exc):
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
            except Exception as exc2:
                logger.error(f"finance_insert_tx_error: {exc2}")
                return None
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



def set_category_monthly_limit(
    user_id: int, category_hint: str, limit_mvr: Any
) -> Optional[dict[str, Any]]:
    """Set finance_categories.monthly_limit_mvr for a resolved category."""
    from decimal import Decimal as _D

    try:
        limit = _D(str(limit_mvr)).quantize(_D("0.01"))
    except Exception:
        return None
    if limit < 0:
        return None
    cat_id, cat_name = resolve_category_id(user_id, category_hint)
    if cat_id is None:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE finance_categories
                    SET monthly_limit_mvr = %s, is_active = TRUE
                    WHERE id = %s AND user_id = %s
                    RETURNING id, name, monthly_limit_mvr
                    """,
                    (limit, cat_id, user_id),
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "name": row[1],
            "monthly_limit_mvr": _D(str(row[2])),
        }
    except Exception as exc:
        logger.error(f"finance_set_category_limit_error: {exc}")
        return None


def fmt_mvr(amount: Any) -> str:
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
        return f"{d:.2f}"
    except Exception:
        return str(amount)


# ---------------------------------------------------------------------------
# Phase 3 — savings goals, fixed expenses, digest queries
# ---------------------------------------------------------------------------


def create_savings_goal(
    user_id: int,
    name: str,
    target_mvr: float,
    monthly_target_mvr: float = 0.0,
) -> Optional[int]:
    """Create or reactivate a savings goal. Returns goal id."""
    name = (name or "").strip()
    if not name or target_mvr <= 0:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance_savings_goals(
                        user_id, name, target_mvr, monthly_target_mvr, is_active
                    ) VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (user_id, name) DO UPDATE SET
                        target_mvr = EXCLUDED.target_mvr,
                        monthly_target_mvr = EXCLUDED.monthly_target_mvr,
                        is_active = TRUE
                    RETURNING id
                    """,
                    (
                        user_id,
                        name,
                        Decimal(str(round(target_mvr, 2))),
                        Decimal(str(round(monthly_target_mvr, 2))),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.error(f"finance_create_goal_error: {exc}")
        return None


def list_savings_goals(user_id: int, active_only: bool = True) -> list[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                sql = """
                    SELECT id, name, target_mvr, saved_mvr, monthly_target_mvr, is_active
                    FROM finance_savings_goals
                    WHERE user_id = %s
                """
                if active_only:
                    sql += " AND is_active = TRUE"
                sql += " ORDER BY created_at"
                cur.execute(sql, (user_id,))
                rows = cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "name": r[1],
                "target_mvr": Decimal(str(r[2])),
                "saved_mvr": Decimal(str(r[3])),
                "monthly_target_mvr": Decimal(str(r[4])),
                "is_active": bool(r[5]),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error(f"finance_list_goals_error: {exc}")
        return []


def find_savings_goal(user_id: int, name_hint: str) -> Optional[dict[str, Any]]:
    """Case-insensitive exact then contains match on active goals."""
    hint = (name_hint or "").strip().lower()
    if not hint:
        return None
    goals = list_savings_goals(user_id, active_only=True)
    for g in goals:
        if g["name"].lower() == hint:
            return g
    for g in goals:
        n = g["name"].lower()
        if hint in n or n in hint:
            return g
    return None


def contribute_to_goal(
    user_id: int,
    goal_id: int,
    amount_mvr: float,
    note: str = "",
) -> Optional[dict[str, Any]]:
    """Add ledger row and bump saved_mvr. Returns updated goal snapshot."""
    if amount_mvr <= 0:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance_savings_ledger(goal_id, user_id, amount_mvr, note)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        goal_id,
                        user_id,
                        Decimal(str(round(amount_mvr, 2))),
                        note or "",
                    ),
                )
                cur.execute(
                    """
                    UPDATE finance_savings_goals
                    SET saved_mvr = saved_mvr + %s
                    WHERE id = %s AND user_id = %s
                    RETURNING id, name, target_mvr, saved_mvr, monthly_target_mvr
                    """,
                    (
                        Decimal(str(round(amount_mvr, 2))),
                        goal_id,
                        user_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "name": row[1],
            "target_mvr": Decimal(str(row[2])),
            "saved_mvr": Decimal(str(row[3])),
            "monthly_target_mvr": Decimal(str(row[4])),
        }
    except Exception as exc:
        logger.error(f"finance_contribute_error: {exc}")
        return None


def ensure_category_kind(user_id: int, name: str, kind: str) -> tuple[Optional[int], str]:
    """Ensure category exists with given kind (fixed|variable|income)."""
    name = (name or "").strip() or "Other"
    kind = (kind or "variable").lower()
    if kind not in ("fixed", "variable", "income"):
        kind = "variable"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance_categories(user_id, name, kind, monthly_limit_mvr)
                    VALUES (%s, %s, %s, 0)
                    ON CONFLICT (user_id, name) DO UPDATE SET
                        kind = EXCLUDED.kind,
                        is_active = TRUE
                    RETURNING id, name
                    """,
                    (user_id, name, kind),
                )
                row = cur.fetchone()
            conn.commit()
        if row:
            return int(row[0]), row[1]
        return resolve_category_id(user_id, name)
    except Exception as exc:
        logger.error(f"finance_ensure_category_kind_error: {exc}")
        return None, name


def upsert_fixed_expense(
    user_id: int,
    name: str,
    amount_mvr: float,
    category_id: Optional[int] = None,
) -> Optional[int]:
    name = (name or "").strip()
    if not name or amount_mvr <= 0:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO finance_fixed_expenses(
                        user_id, name, amount_mvr, category_id, cadence, is_active
                    ) VALUES (%s, %s, %s, %s, 'monthly', TRUE)
                    ON CONFLICT (user_id, name) DO UPDATE SET
                        amount_mvr = EXCLUDED.amount_mvr,
                        category_id = COALESCE(EXCLUDED.category_id, finance_fixed_expenses.category_id),
                        is_active = TRUE
                    RETURNING id
                    """,
                    (
                        user_id,
                        name,
                        Decimal(str(round(amount_mvr, 2))),
                        category_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        logger.error(f"finance_upsert_fixed_error: {exc}")
        return None


def list_fixed_expenses(user_id: int) -> list[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f.id, f.name, f.amount_mvr, f.cadence,
                           c.name AS category_name
                    FROM finance_fixed_expenses f
                    LEFT JOIN finance_categories c ON c.id = f.category_id
                    WHERE f.user_id = %s AND f.is_active = TRUE
                    ORDER BY f.name
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "name": r[1],
                "amount_mvr": Decimal(str(r[2])),
                "cadence": r[3],
                "category_name": r[4] or "",
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error(f"finance_list_fixed_error: {exc}")
        return []


def sum_fixed_monthly(user_id: int) -> Decimal:
    rows = list_fixed_expenses(user_id)
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(r["amount_mvr"]))
    return total


def sum_variable_spend_month(user_id: int) -> Decimal:
    """Expense spend this month on categories with kind=variable (or uncategorized)."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(t.amount_mvr), 0)
                    FROM finance_transactions t
                    LEFT JOIN finance_categories c ON c.id = t.category_id
                    WHERE t.user_id = %s
                      AND t.tx_type = 'expense'
                      AND t.tx_date >= date_trunc('month', CURRENT_DATE)::date
                      AND (c.kind IS NULL OR c.kind = 'variable')
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return Decimal(str(row[0])) if row else Decimal("0")
    except Exception as exc:
        logger.error(f"finance_sum_variable_error: {exc}")
        return Decimal("0")


def sum_income_month(user_id: int) -> Decimal:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount_mvr), 0)
                    FROM finance_transactions
                    WHERE user_id = %s
                      AND tx_type = 'income'
                      AND tx_date >= date_trunc('month', CURRENT_DATE)::date
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return Decimal(str(row[0])) if row else Decimal("0")
    except Exception as exc:
        logger.error(f"finance_sum_income_error: {exc}")
        return Decimal("0")


def spend_by_category_period(
    user_id: int, period: str = "month"
) -> list[dict[str, Any]]:
    """Top expense categories for week|month."""
    period = (period or "month").lower()
    if period == "week":
        where = "t.tx_date >= date_trunc('week', CURRENT_DATE)::date"
    else:
        where = "t.tx_date >= date_trunc('month', CURRENT_DATE)::date"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COALESCE(c.name, 'Other') AS name,
                           COALESCE(c.kind, 'variable') AS kind,
                           COALESCE(SUM(t.amount_mvr), 0) AS spent
                    FROM finance_transactions t
                    LEFT JOIN finance_categories c ON c.id = t.category_id
                    WHERE t.user_id = %s AND t.tx_type = 'expense' AND {where}
                    GROUP BY COALESCE(c.name, 'Other'), COALESCE(c.kind, 'variable')
                    ORDER BY spent DESC
                    LIMIT 8
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "name": r[0],
                "kind": r[1],
                "spent": Decimal(str(r[2])),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error(f"finance_spend_by_cat_error: {exc}")
        return []


def sum_expenses_by_kind_period(
    user_id: int, period: str = "month"
) -> dict[str, Decimal]:
    """Return {fixed, variable, other} expense totals for period."""
    period = (period or "month").lower()
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
                    SELECT COALESCE(c.kind, 'variable') AS kind,
                           COALESCE(SUM(t.amount_mvr), 0) AS spent
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
        return out
    except Exception as exc:
        logger.error(f"finance_sum_by_kind_error: {exc}")
        return out


def list_finance_digest_targets() -> list[dict[str, Any]]:
    """Users with finance activity or authorized telegram users.

    Returns [{user_id, telegram_user_id}] — telegram_user_id is private chat_id.
    """
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
