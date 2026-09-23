"""Postgres store for tasks + reminders (Life OS slice 2)."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://127.0.0.1:8104")
USER_TZ_NAME = "Indian/Maldives"


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
        logger.warning("task_ensure_user_error: %s", exc)
        return None


def _scheduler_post(path: str, payload: dict) -> Optional[dict]:
    url = f"{SCHEDULER_URL.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.warning("scheduler_http_error: %s %s", exc.code, body[:200])
        return None
    except Exception as exc:
        logger.warning("scheduler_post_error: %s", exc)
        return None


def _scheduler_delete(job_id: str) -> bool:
    url = f"{SCHEDULER_URL.rstrip('/')}/jobs/{job_id}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            return True
    except Exception as exc:
        logger.warning("scheduler_delete_error: %s", exc)
        return False


def get_or_create_list(db_user_id: int, name: str) -> Optional[int]:
    name = (name or "Tasks").strip()[:60] or "Tasks"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO task_lists(user_id, name)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, name) DO UPDATE SET updated_at = NOW()
                    RETURNING id
                    """,
                    (db_user_id, name),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("task_list_upsert_error: %s", exc)
        return None


def create_task(
    *,
    db_user_id: int,
    title: str,
    due_at: Optional[datetime] = None,
    list_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> Optional[dict]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tasks(user_id, list_id, title, due_at, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, title, due_at, status, list_id
                    """,
                    (db_user_id, list_id, title, due_at, notes),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                return None
            return {
                "id": int(row[0]),
                "title": row[1],
                "due_at": row[2],
                "status": row[3],
                "list_id": row[4],
            }
    except Exception as exc:
        logger.warning("task_create_error: %s", exc)
        return None


def list_open_tasks(db_user_id: int, *, limit: int = 20) -> list[dict]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.id, t.title, t.due_at, t.status, tl.name
                    FROM tasks t
                    LEFT JOIN task_lists tl ON tl.id = t.list_id
                    WHERE t.user_id = %s AND t.status = 'open'
                    ORDER BY t.due_at NULLS LAST, t.id
                    LIMIT %s
                    """,
                    (db_user_id, limit),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "title": r[1],
                "due_at": r[2],
                "status": r[3],
                "list_name": r[4],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("task_list_error: %s", exc)
        return []


def find_open_tasks(db_user_id: int, query: str, *, limit: int = 5) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if q.isdigit():
                    cur.execute(
                        """
                        SELECT id, title, due_at, status FROM tasks
                        WHERE user_id = %s AND status = 'open' AND id = %s
                        """,
                        (db_user_id, int(q)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, title, due_at, status FROM tasks
                        WHERE user_id = %s AND status = 'open'
                          AND title ILIKE %s
                        ORDER BY due_at NULLS LAST, id
                        LIMIT %s
                        """,
                        (db_user_id, f"%{q}%", limit),
                    )
                rows = cur.fetchall()
        return [
            {"id": int(r[0]), "title": r[1], "due_at": r[2], "status": r[3]}
            for r in rows
        ]
    except Exception as exc:
        logger.warning("task_find_error: %s", exc)
        return []


def complete_task(db_user_id: int, task_id: int) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'done', completed_at = NOW(), updated_at = NOW()
                    WHERE id = %s AND user_id = %s AND status = 'open'
                    RETURNING id, reminder_id
                    """,
                    (task_id, db_user_id),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return False
                rem_id = row[1]
                if rem_id:
                    cur.execute(
                        """
                        SELECT scheduler_job_id FROM reminders
                        WHERE id = %s AND user_id = %s AND status = 'active'
                        """,
                        (rem_id, db_user_id),
                    )
                    jr = cur.fetchone()
                    if jr and jr[0]:
                        _scheduler_delete(jr[0])
                    cur.execute(
                        """
                        UPDATE reminders
                        SET status = 'cancelled', updated_at = NOW()
                        WHERE id = %s AND user_id = %s AND status = 'active'
                        """,
                        (rem_id, db_user_id),
                    )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("task_complete_error: %s", exc)
        return False


def delete_task(db_user_id: int, task_id: int) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE id = %s AND user_id = %s AND status = 'open'
                    RETURNING id, reminder_id
                    """,
                    (task_id, db_user_id),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return False
                rem_id = row[1]
                if rem_id:
                    cur.execute(
                        "SELECT scheduler_job_id FROM reminders WHERE id = %s",
                        (rem_id,),
                    )
                    jr = cur.fetchone()
                    if jr and jr[0]:
                        _scheduler_delete(jr[0])
                    cur.execute(
                        """
                        UPDATE reminders SET status = 'cancelled', updated_at = NOW()
                        WHERE id = %s AND status = 'active'
                        """,
                        (rem_id,),
                    )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("task_delete_error: %s", exc)
        return False


def link_task_reminder(db_user_id: int, task_id: int, reminder_id: int) -> None:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks SET reminder_id = %s, updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                    """,
                    (reminder_id, task_id, db_user_id),
                )
                cur.execute(
                    """
                    UPDATE reminders SET task_id = %s, updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                    """,
                    (task_id, reminder_id, db_user_id),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("task_link_reminder_error: %s", exc)


def create_reminder(
    *,
    db_user_id: int,
    chat_id: str,
    thread_id: str,
    title: str,
    kind: str,
    run_at: Optional[datetime] = None,
    cron_expr: Optional[str] = None,
    task_id: Optional[int] = None,
    telegram_user_id: Optional[str] = None,
    bundle_key: Optional[str] = None,
    notify_text: Optional[str] = None,
) -> Optional[dict]:
    """Insert reminder row + schedule via aop-scheduler. Self-ping only."""
    text = notify_text or f"Reminder: {title}"
    payload: dict[str, Any] = {
        "text": text,
        "target_user_id": str(telegram_user_id or ""),
        "chat_id": str(chat_id),
        "thread_id": str(thread_id or ""),
        "timezone": USER_TZ_NAME,
        "is_reflect": False,
    }
    if kind == "once":
        if run_at is None:
            return None
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        payload["job_type"] = "once"
        payload["run_at"] = run_at.astimezone(timezone.utc).isoformat()
    else:
        if not cron_expr:
            return None
        payload["job_type"] = "cron"
        payload["cron_expr"] = cron_expr

    # Cheap bundling: same chat + same bundle_key → append to existing once job text
    if kind == "once" and bundle_key:
        bundled = _try_bundle(db_user_id, chat_id, bundle_key, title, text)
        if bundled is not None:
            return bundled

    job = _scheduler_post("/jobs", payload)
    if not job or not job.get("job_id"):
        return None
    job_id = job["job_id"]
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders(
                      user_id, chat_id, thread_id, title, body, kind,
                      run_at, cron_expr, timezone, scheduler_job_id,
                      task_id, status, bundle_key
                    )
                    VALUES (
                      %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, 'active', %s
                    )
                    RETURNING id, title, kind, run_at, cron_expr, scheduler_job_id, status
                    """,
                    (
                        db_user_id,
                        str(chat_id),
                        thread_id or None,
                        title,
                        text,
                        kind,
                        run_at,
                        cron_expr,
                        USER_TZ_NAME,
                        job_id,
                        task_id,
                        bundle_key,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                return None
            return {
                "id": int(row[0]),
                "title": row[1],
                "kind": row[2],
                "run_at": row[3],
                "cron_expr": row[4],
                "scheduler_job_id": row[5],
                "status": row[6],
                "bundled": False,
            }
    except Exception as exc:
        logger.warning("reminder_create_error: %s", exc)
        _scheduler_delete(job_id)
        return None


def _try_bundle(
    db_user_id: int,
    chat_id: str,
    bundle_key: str,
    title: str,
    text: str,
) -> Optional[dict]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, body, scheduler_job_id, run_at, kind, status
                    FROM reminders
                    WHERE user_id = %s AND chat_id = %s AND bundle_key = %s
                      AND status = 'active' AND kind = 'once'
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (db_user_id, str(chat_id), bundle_key),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                rem_id, old_title, old_body, job_id, run_at, kind, status = row
                new_body = (old_body or "").rstrip()
                if title not in new_body:
                    new_body = f"{new_body}\n• {title}" if new_body else text
                # Cancel old job and recreate with combined text (APScheduler has no edit)
                if job_id:
                    _scheduler_delete(job_id)
                payload = {
                    "job_type": "once",
                    "text": new_body if new_body.startswith("Reminder") else f"Reminders:\n{new_body}",
                    "run_at": run_at.astimezone(timezone.utc).isoformat() if run_at else None,
                    "chat_id": str(chat_id),
                    "timezone": USER_TZ_NAME,
                }
                # need target from... leave empty; notification uses chat_id
                job = _scheduler_post("/jobs", payload)
                if not job or not job.get("job_id"):
                    conn.rollback()
                    return None
                cur.execute(
                    """
                    UPDATE reminders
                    SET title = %s, body = %s, scheduler_job_id = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, title, kind, run_at, cron_expr, scheduler_job_id, status
                    """,
                    (
                        (old_title + f"; {title}")[:120],
                        payload["text"],
                        job["job_id"],
                        rem_id,
                    ),
                )
                out = cur.fetchone()
            conn.commit()
            if not out:
                return None
            return {
                "id": int(out[0]),
                "title": out[1],
                "kind": out[2],
                "run_at": out[3],
                "cron_expr": out[4],
                "scheduler_job_id": out[5],
                "status": out[6],
                "bundled": True,
            }
    except Exception as exc:
        logger.warning("reminder_bundle_error: %s", exc)
        return None


def list_active_reminders(db_user_id: int, *, limit: int = 20) -> list[dict]:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, kind, run_at, cron_expr, status
                    FROM reminders
                    WHERE user_id = %s AND status = 'active'
                    ORDER BY COALESCE(run_at, NOW()), id
                    LIMIT %s
                    """,
                    (db_user_id, limit),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "title": r[1],
                "kind": r[2],
                "run_at": r[3],
                "cron_expr": r[4],
                "status": r[5],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("reminder_list_error: %s", exc)
        return []


def find_reminders(db_user_id: int, query: str, *, limit: int = 5) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if q.isdigit():
                    cur.execute(
                        """
                        SELECT id, title, kind, run_at, cron_expr, scheduler_job_id, status
                        FROM reminders
                        WHERE user_id = %s AND status = 'active' AND id = %s
                        """,
                        (db_user_id, int(q)),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, title, kind, run_at, cron_expr, scheduler_job_id, status
                        FROM reminders
                        WHERE user_id = %s AND status = 'active'
                          AND title ILIKE %s
                        ORDER BY COALESCE(run_at, NOW()), id
                        LIMIT %s
                        """,
                        (db_user_id, f"%{q}%", limit),
                    )
                rows = cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "title": r[1],
                "kind": r[2],
                "run_at": r[3],
                "cron_expr": r[4],
                "scheduler_job_id": r[5],
                "status": r[6],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("reminder_find_error: %s", exc)
        return []


def cancel_reminder(db_user_id: int, reminder_id: int) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT scheduler_job_id FROM reminders
                    WHERE id = %s AND user_id = %s AND status = 'active'
                    """,
                    (reminder_id, db_user_id),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return False
                if row[0]:
                    _scheduler_delete(row[0])
                cur.execute(
                    """
                    UPDATE reminders
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                    """,
                    (reminder_id, db_user_id),
                )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("reminder_cancel_error: %s", exc)
        return False


def snooze_reminder(
    db_user_id: int,
    reminder_id: int,
    *,
    new_run_at: datetime,
    chat_id: str,
    thread_id: str,
    telegram_user_id: str,
) -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, body, scheduler_job_id, kind
                    FROM reminders
                    WHERE id = %s AND user_id = %s AND status = 'active'
                    """,
                    (reminder_id, db_user_id),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return False
                title, body, old_job, kind = row
                if kind != "once":
                    # Snooze recurring = schedule one-shot overlay; keep cron
                    pass
                if old_job and kind == "once":
                    _scheduler_delete(old_job)
                payload = {
                    "job_type": "once",
                    "text": body or f"Reminder: {title}",
                    "run_at": new_run_at.astimezone(timezone.utc).isoformat(),
                    "target_user_id": str(telegram_user_id),
                    "chat_id": str(chat_id),
                    "thread_id": str(thread_id or ""),
                    "timezone": USER_TZ_NAME,
                }
                job = _scheduler_post("/jobs", payload)
                if not job or not job.get("job_id"):
                    conn.rollback()
                    return False
                if kind == "once":
                    cur.execute(
                        """
                        UPDATE reminders
                        SET run_at = %s, scheduler_job_id = %s,
                            snooze_until = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (new_run_at, job["job_id"], new_run_at, reminder_id),
                    )
                else:
                    # leave cron; just note snooze_until (one-shot fire separate)
                    cur.execute(
                        """
                        UPDATE reminders
                        SET snooze_until = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (new_run_at, reminder_id),
                    )
            conn.commit()
            return True
    except Exception as exc:
        logger.warning("reminder_snooze_error: %s", exc)
        return False


def latest_active_reminder(db_user_id: int) -> Optional[dict]:
    items = list_active_reminders(db_user_id, limit=1)
    return items[0] if items else None
