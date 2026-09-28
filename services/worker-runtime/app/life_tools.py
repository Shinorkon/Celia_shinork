"""Phase D1 life-agent tool executors (reminders + tasks).

Mirrors ingress task_store semantics against agent_platform + aop-scheduler.
No shell. Self-ping only.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://127.0.0.1:8104")
USER_TZ_NAME = os.getenv("CELIA_USER_TZ", "Indian/Maldives")
USER_TZ = ZoneInfo(USER_TZ_NAME)

LIFE_TOOL_NAMES = frozenset(
    {
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "create_task",
        "list_tasks",
    }
)


def _conn():
    return psycopg.connect(DATABASE_URL)


def resolve_db_user_id(telegram_user_id: str | int | None) -> Optional[int]:
    if telegram_user_id is None or str(telegram_user_id).strip() == "":
        return None
    try:
        tid = int(str(telegram_user_id).strip())
    except ValueError:
        return None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users(telegram_user_id, role, is_active)
                    VALUES (%s, 'authorized', TRUE)
                    ON CONFLICT (telegram_user_id) DO UPDATE SET is_active = TRUE
                    RETURNING id
                    """,
                    (tid,),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
    except Exception as exc:
        logger.warning("life_tools_ensure_user_error: %s", exc)
        return None


def _scheduler_post(path: str, payload: dict) -> Optional[dict]:
    url = f"{SCHEDULER_URL.rstrip('/')}{path}"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("scheduler_post_fail: %s %s", resp.status_code, resp.text[:200])
                return None
            return resp.json()
    except Exception as exc:
        logger.warning("scheduler_post_error: %s", exc)
        return None


def _scheduler_delete(job_id: str) -> bool:
    if not job_id:
        return False
    url = f"{SCHEDULER_URL.rstrip('/')}/jobs/{job_id}"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.delete(url)
            return resp.status_code < 400
    except Exception as exc:
        logger.warning("scheduler_delete_error: %s", exc)
        return False


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=USER_TZ).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_when(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(USER_TZ).strftime("%a %d %b %H:%M MVT")


def create_reminder(
    *,
    db_user_id: int,
    chat_id: str,
    thread_id: str,
    telegram_user_id: str,
    title: str,
    kind: str = "once",
    run_at_iso: Optional[str] = None,
    cron_expr: Optional[str] = None,
) -> str:
    title = (title or "").strip() or "reminder"
    kind = (kind or "once").lower()
    run_at = _parse_iso(run_at_iso)
    text = f"Reminder: {title}"
    payload: dict[str, Any] = {
        "text": text,
        "target_user_id": str(telegram_user_id or ""),
        "chat_id": str(chat_id),
        "thread_id": str(thread_id or ""),
        "timezone": USER_TZ_NAME,
        "is_reflect": False,
    }
    if kind == "cron":
        if not cron_expr:
            return "Need cron_expr for recurring reminder."
        payload["job_type"] = "cron"
        payload["cron_expr"] = cron_expr
    else:
        kind = "once"
        if run_at is None:
            return "Need run_at_iso (UTC) for a one-shot reminder."
        payload["job_type"] = "once"
        payload["run_at"] = run_at.isoformat()

    job = _scheduler_post("/jobs", payload)
    if not job or not job.get("job_id"):
        return "Couldn't schedule reminder (scheduler rejected)."
    job_id = job["job_id"]
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reminders(
                      user_id, chat_id, thread_id, title, body, kind,
                      run_at, cron_expr, timezone, scheduler_job_id, status
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
                    RETURNING id
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
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        rid = int(row[0]) if row else 0
        when = _fmt_when(run_at) if kind == "once" else f"cron {cron_expr}"
        return f"Created reminder #{rid} “{title}” ({when})."
    except Exception as exc:
        logger.warning("life_create_reminder_error: %s", exc)
        _scheduler_delete(job_id)
        return f"Reminder DB error: {exc}"


def list_reminders(*, db_user_id: int) -> str:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, kind, run_at, cron_expr
                    FROM reminders
                    WHERE user_id = %s AND status = 'active'
                    ORDER BY COALESCE(run_at, NOW()) ASC
                    LIMIT 20
                    """,
                    (db_user_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        return f"List reminders failed: {exc}"
    if not rows:
        return "No active reminders."
    lines = []
    for rid, title, kind, run_at, cron_expr in rows:
        if kind == "cron":
            lines.append(f"#{rid} {title} (cron {cron_expr})")
        else:
            lines.append(f"#{rid} {title} — {_fmt_when(run_at)}")
    return "Reminders:\n" + "\n".join(lines)


def cancel_reminder(
    *,
    db_user_id: int,
    reminder_id: Optional[int] = None,
    query: Optional[str] = None,
) -> str:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                row = None
                if reminder_id:
                    cur.execute(
                        """
                        SELECT id, title, scheduler_job_id FROM reminders
                        WHERE user_id = %s AND id = %s AND status = 'active'
                        """,
                        (db_user_id, int(reminder_id)),
                    )
                    row = cur.fetchone()
                elif query:
                    cur.execute(
                        """
                        SELECT id, title, scheduler_job_id FROM reminders
                        WHERE user_id = %s AND status = 'active'
                          AND title ILIKE '%%' || %s || '%%'
                        ORDER BY updated_at DESC NULLS LAST, id DESC
                        LIMIT 1
                        """,
                        (db_user_id, query.strip()),
                    )
                    row = cur.fetchone()
                if not row:
                    return "No matching reminder."
                rid, title, job_id = int(row[0]), row[1], row[2]
                cur.execute(
                    """
                    UPDATE reminders SET status = 'cancelled', updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                    """,
                    (rid, db_user_id),
                )
            conn.commit()
        if job_id:
            _scheduler_delete(str(job_id))
        return f"Cancelled #{rid} {title}."
    except Exception as exc:
        return f"Cancel failed: {exc}"


def create_task(
    *,
    db_user_id: int,
    chat_id: str,
    thread_id: str,
    telegram_user_id: str,
    title: str,
    due_at_iso: Optional[str] = None,
    list_name: Optional[str] = None,
) -> str:
    title = (title or "").strip()
    if not title:
        return "Need a task title."
    due_at = _parse_iso(due_at_iso)
    list_id = None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if list_name:
                    cur.execute(
                        """
                        INSERT INTO task_lists(user_id, name)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id, name) DO UPDATE SET name = EXCLUDED.name
                        RETURNING id
                        """,
                        (db_user_id, list_name.strip()[:80]),
                    )
                    row = cur.fetchone()
                    if row:
                        list_id = int(row[0])
                cur.execute(
                    """
                    INSERT INTO tasks(user_id, title, due_at, list_id, status)
                    VALUES (%s, %s, %s, %s, 'open')
                    RETURNING id
                    """,
                    (db_user_id, title, due_at, list_id),
                )
                row = cur.fetchone()
            conn.commit()
        tid = int(row[0]) if row else 0
        due_bit = f" due {_fmt_when(due_at)}" if due_at else ""
        rem_note = ""
        if due_at is not None:
            rem = create_reminder(
                db_user_id=db_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                telegram_user_id=telegram_user_id,
                title=title,
                kind="once",
                run_at_iso=due_at.isoformat(),
            )
            rem_note = " " + rem
        return f"Task #{tid} {title}{due_bit}.{rem_note}".strip()
    except Exception as exc:
        logger.warning("life_create_task_error: %s", exc)
        return f"Create task failed: {exc}"


def list_tasks(*, db_user_id: int) -> str:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.id, t.title, t.due_at, tl.name
                    FROM tasks t
                    LEFT JOIN task_lists tl ON tl.id = t.list_id
                    WHERE t.user_id = %s AND t.status = 'open'
                    ORDER BY t.due_at NULLS LAST, t.id DESC
                    LIMIT 20
                    """,
                    (db_user_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        return f"List tasks failed: {exc}"
    if not rows:
        return "No open tasks."
    lines = []
    for tid, title, due_at, list_name in rows:
        list_bit = f" [{list_name}]" if list_name else ""
        due_bit = f" — due {_fmt_when(due_at)}" if due_at else ""
        lines.append(f"#{tid} {title}{list_bit}{due_bit}")
    return "Tasks:\n" + "\n".join(lines)


def execute_life_tool(
    name: str,
    args: dict,
    *,
    user_id: str = "",
    chat_id: str = "",
    thread_id: str = "",
) -> str:
    if name not in LIFE_TOOL_NAMES:
        return f"(unsupported life tool: {name})"
    db_uid = resolve_db_user_id(user_id or chat_id)
    if db_uid is None:
        return "No user context for life tools."
    tg = str(user_id or chat_id or "")
    if name == "create_reminder":
        return create_reminder(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            thread_id=str(thread_id or ""),
            telegram_user_id=tg,
            title=str(args.get("title") or ""),
            kind=str(args.get("kind") or "once"),
            run_at_iso=args.get("run_at_iso"),
            cron_expr=args.get("cron_expr"),
        )
    if name == "list_reminders":
        return list_reminders(db_user_id=db_uid)
    if name == "cancel_reminder":
        rid = args.get("reminder_id")
        try:
            rid_i = int(rid) if rid is not None else None
        except (TypeError, ValueError):
            rid_i = None
        return cancel_reminder(
            db_user_id=db_uid,
            reminder_id=rid_i,
            query=args.get("query"),
        )
    if name == "create_task":
        return create_task(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            thread_id=str(thread_id or ""),
            telegram_user_id=tg,
            title=str(args.get("title") or ""),
            due_at_iso=args.get("due_at_iso"),
            list_name=args.get("list_name"),
        )
    if name == "list_tasks":
        return list_tasks(db_user_id=db_uid)
    return f"(unhandled life tool: {name})"
