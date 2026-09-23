"""Postgres store for Celia-only calendar_events (Life OS slice 4)."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)


def _conn():
    return psycopg.connect(DATABASE_URL)


def ensure_user(telegram_user_id: int) -> Optional[int]:
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
        logger.warning("calendar_ensure_user_error: %s", exc)
        return None


_SELECT = (
    "id, user_id, starts_at, ends_at, title, location, entity_ids, source, status"
)


def _row(r) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "user_id": int(r[1]),
        "starts_at": r[2],
        "ends_at": r[3],
        "title": r[4],
        "location": r[5],
        "entity_ids": list(r[6] or []),
        "source": r[7],
        "status": r[8],
    }


def find_conflicts(
    db_user_id: int,
    starts_at: datetime,
    ends_at: datetime,
    *,
    exclude_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT}
                    FROM calendar_events
                    WHERE user_id = %s
                      AND status = 'active'
                      AND starts_at < %s
                      AND ends_at > %s
                      AND (%s::bigint IS NULL OR id <> %s)
                    ORDER BY starts_at
                    LIMIT 5
                    """,
                    (db_user_id, ends_at, starts_at, exclude_id, exclude_id),
                )
                rows = cur.fetchall()
        return [_row(r) for r in rows]
    except Exception as exc:
        logger.warning("calendar_conflict_error: %s", exc)
        return []


def create_event(
    *,
    db_user_id: int,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    location: Optional[str] = None,
    entity_ids: Optional[list[int]] = None,
    source: str = "telegram",
) -> Optional[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO calendar_events(
                      user_id, starts_at, ends_at, title, location, entity_ids, source, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
                    RETURNING {_SELECT}
                    """,
                    (
                        db_user_id,
                        starts_at,
                        ends_at,
                        title,
                        location,
                        [int(x) for x in (entity_ids or []) if x is not None],
                        source,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return _row(row) if row else None
    except Exception as exc:
        logger.warning("calendar_create_error: %s", exc)
        return None


def update_event(
    *,
    db_user_id: int,
    event_id: int,
    title: Optional[str] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    location: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE calendar_events
                    SET title = COALESCE(%s, title),
                        starts_at = COALESCE(%s, starts_at),
                        ends_at = COALESCE(%s, ends_at),
                        location = COALESCE(%s, location),
                        updated_at = NOW()
                    WHERE id = %s AND user_id = %s AND status = 'active'
                    RETURNING {_SELECT}
                    """,
                    (title, starts_at, ends_at, location, event_id, db_user_id),
                )
                row = cur.fetchone()
            conn.commit()
            return _row(row) if row else None
    except Exception as exc:
        logger.warning("calendar_update_error: %s", exc)
        return None


def list_events(
    db_user_id: int,
    *,
    start: datetime,
    end: datetime,
    limit: int = 40,
) -> list[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT}
                    FROM calendar_events
                    WHERE user_id = %s
                      AND status = 'active'
                      AND starts_at < %s
                      AND ends_at > %s
                    ORDER BY starts_at
                    LIMIT %s
                    """,
                    (db_user_id, end, start, limit),
                )
                rows = cur.fetchall()
        return [_row(r) for r in rows]
    except Exception as exc:
        logger.warning("calendar_list_error: %s", exc)
        return []


def find_events(db_user_id: int, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if q.isdigit():
                    cur.execute(
                        f"""
                        SELECT {_SELECT} FROM calendar_events
                        WHERE user_id = %s AND status = 'active' AND id = %s
                        """,
                        (db_user_id, int(q)),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT {_SELECT} FROM calendar_events
                        WHERE user_id = %s AND status = 'active'
                          AND (title ILIKE '%%' || %s || '%%'
                               OR COALESCE(location, '') ILIKE '%%' || %s || '%%')
                        ORDER BY starts_at DESC
                        LIMIT %s
                        """,
                        (db_user_id, q, q, limit),
                    )
                rows = cur.fetchall()
        return [_row(r) for r in rows]
    except Exception as exc:
        logger.warning("calendar_find_error: %s", exc)
        return []
