"""Postgres helpers for Life OS memory foundation (ingress-local)."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

KIND_TO_SEGMENT = {
    "preference": "semantic",
    "goal": "semantic",
    "fact": "semantic",
    "habit": "procedural",
    "note": "semantic",
    "correction": "semantic",
    "decision": "episodic",
    "project_state": "episodic",
    "event": "episodic",
}

# Junk the memory-writer must not persist as semantic prefs / goals.
_JUNK_TITLE_RE = re.compile(
    r"(?i)\b(?:quantity|spending\s+total|receipt\s+processed|updated\s+spending|"
    r"items?:\s*\d+|pcs\b|condensed\s+milk\s+quantity)\b"
)
_JUNK_BODY_RE = re.compile(
    r"(?i)(?:\b\d+\s*(?:units?|pcs|pieces)\b|"
    r"total\s+spending\s+updated|"
    r"processed\s+a\s+receipt|"
    r"wants?\s+\d+\s+units?\s+of|"
    r"calculate\s+(?:their|your|my)?\s*total\s+spending)"
)
_LIST_QTY_PREF_RE = re.compile(
    r"(?i)^(?:.+\s+)?(?:x\s*)?\d+\s*(?:x|×)?\s*.+$|.*\bx\s*\d+\b.*"
)


def is_junk_memory_item(kind: str, title: str, body: str) -> bool:
    """True when item is ephemeral list/receipt noise, not long-term memory."""
    kind = (kind or "").lower()
    title = title or ""
    body = body or ""
    if _JUNK_TITLE_RE.search(title) or _JUNK_BODY_RE.search(body):
        return True
    if kind == "preference" and (
        re.search(r"(?i)\b\d+\s*(?:units?|pcs|x)\b", body)
        or re.search(r"(?i)\b(?:milk|eggs|bread|qty|quantity)\b", title)
        and re.search(r"\d", title + body)
    ):
        return True
    if kind in ("project_state", "goal") and re.search(
        r"(?i)\b(?:receipt|spending\s+total|mvr)\b", title + " " + body
    ):
        # Receipt milestones / running totals are finance rows, not memory.
        if re.search(r"(?i)\b(?:total|processed|invoice|receipt)\b", title + " " + body):
            return True
    return False


def segment_for_kind(kind: str, explicit: Optional[str] = None) -> str:
    if explicit in ("episodic", "semantic", "procedural", "working"):
        return explicit
    return KIND_TO_SEGMENT.get((kind or "").lower(), "semantic")


def _conn():
    return psycopg.connect(DATABASE_URL)


def resolve_db_user_id(telegram_user_id: int) -> Optional[int]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE telegram_user_id = %s",
                    (telegram_user_id,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("memory_resolve_user_error: %s", exc)
        return None


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
        logger.warning("memory_ensure_user_error: %s", exc)
        return None


def save_item(
    *,
    db_user_id: int,
    kind: str,
    title: str,
    body: str,
    tags: Optional[list[str]] = None,
    project_ref: Optional[str] = None,
    segment: Optional[str] = None,
    importance: float = 0.5,
    salience: float = 0.5,
    source_chat_id: Optional[str] = None,
    source_run_ref: Optional[str] = None,
    expire_minutes: Optional[int] = None,
    correct_of: Optional[int] = None,
    entity_ids: Optional[list[int]] = None,
) -> Optional[int]:
    if is_junk_memory_item(kind, title, body):
        logger.info("memory_junk_filtered: kind=%s title=%s", kind, title[:80])
        return None
    seg = segment_for_kind(kind, segment)
    expire_at = None
    if seg == "working" or expire_minutes:
        mins = expire_minutes or 60
        expire_at = datetime.now(timezone.utc) + timedelta(minutes=mins)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_items(
                      kind, title, body, tags, project_ref, user_id, segment,
                      importance, salience, source_chat_id, source_run_ref,
                      expire_at, correct_of, entity_ids, status
                    )
                    VALUES (
                      %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s, 'active'
                    )
                    RETURNING id
                    """,
                    (
                        kind,
                        title,
                        body,
                        tags or [],
                        project_ref,
                        db_user_id,
                        seg,
                        importance,
                        salience,
                        source_chat_id,
                        source_run_ref,
                        expire_at,
                        correct_of,
                        [int(x) for x in (entity_ids or []) if x is not None],
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("memory_save_error: %s", exc)
        return None


def soft_forget(
    *,
    db_user_id: int,
    target_id: int,
    reason: Optional[str] = None,
) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_items
                    SET forgotten_at = NOW(), status = 'archived', updated_at = NOW()
                    WHERE id = %s AND user_id = %s AND forgotten_at IS NULL
                    RETURNING id
                    """,
                    (target_id, db_user_id),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return False
                cur.execute(
                    """
                    INSERT INTO memory_corrections(user_id, target_id, action, reason)
                    VALUES (%s, %s, 'forget', %s)
                    """,
                    (db_user_id, target_id, reason),
                )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("memory_forget_error: %s", exc)
        return False


def correct_item(
    *,
    db_user_id: int,
    target_id: int,
    new_kind: str,
    new_title: str,
    new_body: str,
    reason: Optional[str] = None,
) -> Optional[int]:
    if is_junk_memory_item(new_kind, new_title, new_body):
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT kind, segment FROM memory_items
                    WHERE id = %s AND user_id = %s AND forgotten_at IS NULL
                    """,
                    (target_id, db_user_id),
                )
                old = cur.fetchone()
                if not old:
                    return None
                seg = segment_for_kind(new_kind, old[1] if old[1] != "working" else "semantic")
                cur.execute(
                    """
                    INSERT INTO memory_items(
                      kind, title, body, user_id, segment, correct_of, status,
                      importance, salience
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'active', 0.7, 0.7)
                    RETURNING id
                    """,
                    (new_kind, new_title, new_body, db_user_id, seg, target_id),
                )
                new_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    UPDATE memory_items
                    SET status = 'superseded', superseded_by = %s, updated_at = NOW(),
                        forgotten_at = COALESCE(forgotten_at, NOW())
                    WHERE id = %s AND user_id = %s
                    """,
                    (new_id, target_id, db_user_id),
                )
                cur.execute(
                    """
                    INSERT INTO memory_corrections(
                      user_id, target_id, action, replacement_id, reason
                    )
                    VALUES (%s, %s, 'correct', %s, %s)
                    """,
                    (db_user_id, target_id, new_id, reason),
                )
            conn.commit()
            return new_id
    except Exception as exc:
        logger.warning("memory_correct_error: %s", exc)
        return None


def find_candidates(
    *,
    db_user_id: int,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Match active memories by tsvector / ILIKE / tags for forget/correct/recall."""
    q = (query or "").strip()
    if not q:
        return list_known(db_user_id=db_user_id, limit=limit)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, kind, segment, title, body,
                           ts_rank(search_tsv, plainto_tsquery('english', %s)) AS rank
                    FROM memory_items
                    WHERE user_id = %s
                      AND forgotten_at IS NULL
                      AND status = 'active'
                      AND (
                        search_tsv @@ plainto_tsquery('english', %s)
                        OR title ILIKE '%%' || %s || '%%'
                        OR body ILIKE '%%' || %s || '%%'
                        OR %s = ANY(tags)
                      )
                    ORDER BY rank DESC, updated_at DESC
                    LIMIT %s
                    """,
                    (q, db_user_id, q, q, q, q.lower(), limit),
                )
                rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "segment": r[2],
                "title": r[3],
                "body": r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("memory_find_error: %s", exc)
        return []


def list_known(*, db_user_id: int, limit: int = 12) -> list[dict[str, Any]]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, kind, segment, title, body
                    FROM memory_items
                    WHERE user_id = %s
                      AND forgotten_at IS NULL
                      AND status = 'active'
                      AND segment IN ('semantic', 'episodic', 'procedural')
                    ORDER BY
                      CASE segment
                        WHEN 'semantic' THEN 0
                        WHEN 'procedural' THEN 1
                        ELSE 2
                      END,
                      importance DESC,
                      updated_at DESC
                    LIMIT %s
                    """,
                    (db_user_id, limit),
                )
                rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "segment": r[2],
                "title": r[3],
                "body": r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("memory_list_error: %s", exc)
        return []


def touch_access(ids: list[int]) -> None:
    if not ids:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_items
                    SET last_accessed_at = NOW(),
                        access_count = access_count + 1,
                        updated_at = updated_at
                    WHERE id = ANY(%s)
                    """,
                    (ids,),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("memory_touch_error: %s", exc)


def upsert_entity(
    *,
    db_user_id: int,
    entity_type: str,
    canonical_name: str,
    aliases: Optional[list[str]] = None,
    attrs: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Insert or refresh a memory_entities row; return id.

    Cheap merchant/person/place link used by finance (and later calendar).
    Unique on (user_id, entity_type, canonical_name).
    """
    name = " ".join((canonical_name or "").split()).strip()
    etype = (entity_type or "other").strip().lower() or "other"
    if not name or not db_user_id:
        return None
    alias_list = [a.strip() for a in (aliases or []) if (a or "").strip()]
    import json as _json

    attrs_obj = attrs or {}
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_entities(
                      user_id, entity_type, canonical_name, aliases, attrs_jsonb, status
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, 'active')
                    ON CONFLICT (user_id, entity_type, canonical_name)
                    DO UPDATE SET
                      updated_at = NOW(),
                      status = 'active',
                      aliases = CASE
                        WHEN EXCLUDED.aliases = '{}'::text[] THEN memory_entities.aliases
                        ELSE (
                          SELECT ARRAY(
                            SELECT DISTINCT unnest(
                              memory_entities.aliases || EXCLUDED.aliases
                            )
                          )
                        )
                      END,
                      attrs_jsonb = memory_entities.attrs_jsonb || EXCLUDED.attrs_jsonb
                    RETURNING id
                    """,
                    (
                        int(db_user_id),
                        etype,
                        name,
                        alias_list,
                        _json.dumps(attrs_obj),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("memory_upsert_entity_error: %s", exc)
        return None
