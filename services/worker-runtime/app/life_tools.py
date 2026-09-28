"""Phase D1/D2 life-agent tool executors.

D1: reminders + tasks (Postgres + aop-scheduler).
D2: shopping lists (Redis), calendar (Postgres + confirm pending),
    notes (memory_items kind=note).
D3: memory remember/recall/forget/correct; finance session amount-pref +
    receipt recalculate.
No shell. Self-ping only. Calendar create + memory forget/correct = confirm.
"""
from __future__ import annotations

import json
import re
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://127.0.0.1:8104")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6380/0")
_LIST_ACTIVE_TTL = 7 * 24 * 3600
_LIST_DOC_TTL = 30 * 24 * 3600
_CAL_PENDING_TTL = int(os.getenv("CALENDAR_PENDING_TTL_SEC", "300"))
_MEM_PENDING_TTL = int(os.getenv("MEMORY_PENDING_TTL_SEC", "300"))
_FINANCE_SESSION_TTL = int(os.getenv("FINANCE_SESSION_TTL_SEC", str(45 * 60)))
USER_TZ_NAME = os.getenv("CELIA_USER_TZ", "Indian/Maldives")
USER_TZ = ZoneInfo(USER_TZ_NAME)

LIFE_TOOL_NAMES = frozenset(
    {
        # D1
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "create_task",
        "list_tasks",
        # D2 lists
        "create_list",
        "show_list",
        "add_list_items",
        "remove_list_item",
        "clear_list",
        "mark_list_item_bought",
        # D2 calendar
        "create_calendar_event",
        "list_calendar_events",
        # D2 notes
        "add_note",
        "list_notes",
        # D3 memory
        "memory_remember",
        "memory_recall",
        "memory_forget",
        "memory_correct",
        # D3 finance session
        "set_lower_text_amount_pref",
        "recalculate_receipts",
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



# ---------------------------------------------------------------------------
# D2 — Redis shopping lists (mirror list_store)
# ---------------------------------------------------------------------------


def _redis():
    try:
        from redis import Redis

        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("life_list_redis_unavailable: %s", exc)
        return None


def _list_active_key(chat_id: str) -> str:
    return f"celia:list:active:{chat_id}"


def _list_doc_key(list_id: str) -> str:
    return f"celia:list:{list_id}"


def _list_session_key(chat_id: str) -> str:
    return f"celia:list:session:{chat_id}"


def _save_list_doc(doc: dict) -> dict:
    r = _redis()
    lid = doc["list_id"]
    doc = {**doc, "updated_at": time.time()}
    if r is not None:
        try:
            r.set(_list_doc_key(lid), json.dumps(doc), ex=_LIST_DOC_TTL)
            r.set(_list_active_key(doc["chat_id"]), lid, ex=_LIST_ACTIVE_TTL)
        except Exception as exc:
            logger.warning("life_list_save_error: %s", exc)
    return doc


def _get_list_doc(list_id: str) -> Optional[dict]:
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_list_doc_key(list_id))
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("life_list_get_error: %s", exc)
        return None


def _get_active_list(chat_id: str) -> Optional[dict]:
    r = _redis()
    if r is None:
        return None
    try:
        lid = r.get(_list_active_key(chat_id))
        if not lid:
            return None
        return _get_list_doc(lid)
    except Exception as exc:
        logger.warning("life_list_active_error: %s", exc)
        return None


def _fmt_list_item(it: dict) -> str:
    name = it.get("name") or "?"
    qty = int(it.get("qty") or 1)
    mark = "x" if it.get("done") else " "
    return f"[{mark}] {name}" + (f" ×{qty}" if qty != 1 else "")


def create_shopping_list(
    *, chat_id: str, title: str = "List", items: Optional[list] = None
) -> str:
    chat_id = str(chat_id or "")
    if not chat_id:
        return "Need chat context for lists."
    list_id = uuid.uuid4().hex[:12]
    norm = []
    for it in items or []:
        if isinstance(it, str):
            name, qty = it.strip(), 1
        else:
            name = str((it or {}).get("name") or "").strip()
            try:
                qty = int((it or {}).get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
        if name:
            norm.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "name": name,
                    "qty": max(1, qty),
                    "done": False,
                }
            )
    doc = {
        "list_id": list_id,
        "chat_id": chat_id,
        "title": (title or "List").strip() or "List",
        "items": norm,
        "updated_at": time.time(),
        "collecting": True,
    }
    _save_list_doc(doc)
    r = _redis()
    if r is not None:
        try:
            r.set(
                _list_session_key(chat_id),
                json.dumps({"list_id": list_id, "started_at": time.time()}),
                ex=15 * 60,
            )
        except Exception:
            pass
    n = len(norm)
    if n:
        names = ", ".join(i["name"] for i in norm[:6])
        return f"Created {doc['title']} with {n} items ({names})."
    return f"Created {doc['title']}. Send items whenever."


def show_shopping_list(*, chat_id: str) -> str:
    doc = _get_active_list(str(chat_id or ""))
    if not doc:
        return "No list yet — create one first."
    items = doc.get("items") or []
    title = doc.get("title") or "List"
    if not items:
        return f"{title} is empty."
    lines = [_fmt_list_item(it) for it in items]
    body = "\n".join(lines) if len(lines) > 4 else "; ".join(lines)
    return f"{title} ({len(items)} items):\n{body}" if len(lines) > 4 else f"{title} ({len(items)} items): {body}."


def add_shopping_items(*, chat_id: str, items: Optional[list] = None) -> str:
    chat_id = str(chat_id or "")
    raw_items = items or []
    if not raw_items:
        return "No items to add."
    doc = _get_active_list(chat_id)
    if doc is None:
        return create_shopping_list(chat_id=chat_id, title="List", items=raw_items)
    existing = list(doc.get("items") or [])
    added = []
    for it in raw_items:
        if isinstance(it, str):
            name, qty = it.strip(), 1
        else:
            name = str((it or {}).get("name") or "").strip()
            try:
                qty = int((it or {}).get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
        if not name:
            continue
        # merge qty if same name (casefold)
        matched = next((x for x in existing if (x.get("name") or "").casefold() == name.casefold()), None)
        if matched:
            matched["qty"] = int(matched.get("qty") or 1) + max(1, qty)
            matched["done"] = False
            added.append(matched)
        else:
            row = {"id": uuid.uuid4().hex[:8], "name": name, "qty": max(1, qty), "done": False}
            existing.append(row)
            added.append(row)
    doc["items"] = existing
    _save_list_doc(doc)
    if not added:
        return "Nothing to add."
    names = ", ".join(
        (a["name"] if int(a.get("qty") or 1) == 1 else f"{a['name']} ×{a['qty']}") for a in added[:6]
    )
    return f"Added {names} to {doc.get('title') or 'List'} ({len(existing)} items)."


def _match_list_item(items: list[dict], query: str) -> Optional[dict]:
    q = (query or "").strip().casefold()
    if not q:
        return None
    for it in items:
        name = (it.get("name") or "").casefold()
        if q == name or q in name or name in q:
            return it
    return None


def remove_shopping_item(*, chat_id: str, query: str) -> str:
    doc = _get_active_list(str(chat_id or ""))
    if not doc:
        return "No list yet."
    items = list(doc.get("items") or [])
    matched = _match_list_item(items, query)
    if not matched:
        return f"Couldn't find “{query}” on {doc.get('title') or 'List'}."
    items = [it for it in items if it.get("id") != matched.get("id")]
    doc["items"] = items
    _save_list_doc(doc)
    return f"Removed {matched.get('name')} from {doc.get('title') or 'List'} ({len(items)} items)."


def clear_shopping_list(*, chat_id: str) -> str:
    chat_id = str(chat_id or "")
    doc = _get_active_list(chat_id)
    r = _redis()
    if r is not None:
        try:
            r.delete(_list_session_key(chat_id))
        except Exception:
            pass
    if not doc:
        return "No list to clear."
    n = len(doc.get("items") or [])
    doc["items"] = []
    _save_list_doc(doc)
    return f"Cleared {doc.get('title') or 'List'} ({n} removed)." if n else f"{doc.get('title') or 'List'} already empty."


def mark_shopping_bought(*, chat_id: str, query: str) -> str:
    doc = _get_active_list(str(chat_id or ""))
    if not doc:
        return "No list yet."
    items = list(doc.get("items") or [])
    matched = _match_list_item(items, query)
    if not matched:
        return f"Couldn't find “{query}” on {doc.get('title') or 'List'}."
    matched["done"] = True
    doc["items"] = items
    _save_list_doc(doc)
    return f"Marked {matched.get('name')} bought on {doc.get('title') or 'List'}."


# ---------------------------------------------------------------------------
# D2 — Calendar (confirm-first create via Redis pending shared with ingress)
# ---------------------------------------------------------------------------


def _cal_pending_key(chat_id: str) -> str:
    return f"celia:calendar:pending:{chat_id}"


def _set_cal_pending(chat_id: str, payload: dict) -> None:
    payload = {**payload, "expires_at": time.time() + _CAL_PENDING_TTL}
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_cal_pending_key(chat_id), _CAL_PENDING_TTL, json.dumps(payload))
    except Exception as exc:
        logger.warning("life_cal_pending_error: %s", exc)


def create_calendar_event_tool(
    *,
    db_user_id: int,
    chat_id: str,
    title: str,
    starts_at_iso: str,
    ends_at_iso: Optional[str] = None,
    location: Optional[str] = None,
    confirmed: bool = False,
) -> str:
    title = (title or "").strip()
    starts = _parse_iso(starts_at_iso)
    if not title or starts is None:
        return "Need title and starts_at_iso (UTC) for a calendar event."
    ends = _parse_iso(ends_at_iso) if ends_at_iso else starts + timedelta(hours=1)
    if ends <= starts:
        ends = starts + timedelta(hours=1)
    loc = (location or "").strip() or None
    # Phase C: cal.create = confirm unless explicitly confirmed
    if not confirmed:
        _set_cal_pending(
            str(chat_id),
            {
                "action": "create",
                "title": title,
                "starts_at": starts.isoformat(),
                "ends_at": ends.isoformat(),
                "location": loc,
            },
        )
        when = _fmt_when(starts)
        loc_bit = f" @ {loc}" if loc else ""
        return (
            f"PENDING_CONFIRM: Add {title}{loc_bit} — {when}? "
            "Tell the user to reply yes to confirm (or no to cancel)."
        )
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO calendar_events(
                      user_id, starts_at, ends_at, title, location, entity_ids, source, status
                    )
                    VALUES (%s, %s, %s, %s, %s, '{}', 'life-agent', 'active')
                    RETURNING id, title, starts_at
                    """,
                    (db_user_id, starts, ends, title, loc),
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            return "Couldn't save that event."
        return f"Booked #{int(row[0])} {row[1]} — {_fmt_when(row[2])}."
    except Exception as exc:
        logger.warning("life_cal_create_error: %s", exc)
        return f"Calendar create failed: {exc}"


def list_calendar_events_tool(
    *, db_user_id: int, days_ahead: int = 7, start_iso: Optional[str] = None, end_iso: Optional[str] = None
) -> str:
    now = datetime.now(timezone.utc)
    start = _parse_iso(start_iso) or now
    if end_iso:
        end = _parse_iso(end_iso) or (start + timedelta(days=max(1, int(days_ahead or 7))))
    else:
        try:
            days = max(1, min(int(days_ahead or 7), 60))
        except (TypeError, ValueError):
            days = 7
        end = start + timedelta(days=days)
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, starts_at, ends_at, location
                    FROM calendar_events
                    WHERE user_id = %s AND status = 'active'
                      AND starts_at < %s AND ends_at > %s
                    ORDER BY starts_at
                    LIMIT 40
                    """,
                    (db_user_id, end, start),
                )
                rows = cur.fetchall()
    except Exception as exc:
        return f"Calendar list failed: {exc}"
    if not rows:
        return "Nothing on the calendar in that window."
    lines = []
    for rid, title, starts_at, _ends, loc in rows:
        loc_bit = f" @ {loc}" if loc else ""
        lines.append(f"#{rid} {title} — {_fmt_when(starts_at)}{loc_bit}")
    body = "\n".join(lines) if len(lines) > 2 else "; ".join(lines)
    return f"Agenda:\n{body}" if len(lines) > 2 else f"Agenda: {body}."


# ---------------------------------------------------------------------------
# D2 — Notes (memory_items kind=note)
# ---------------------------------------------------------------------------


def add_note_tool(*, db_user_id: int, chat_id: str, body: str, title: Optional[str] = None) -> str:
    body = (body or "").strip()
    if not body:
        return "What should I note?"
    if not title:
        first = body.split(".")[0].split("\n")[0].strip()
        words = first.split()
        title = " ".join(words[:8]) if words else body[:60]
    title = (title or "note")[:80]
    segment = "episodic" if any(
        w in body.lower()
        for w in ("today", "yesterday", "this morning", "monday", "tuesday", "wednesday", "thursday", "friday")
    ) else "semantic"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_items(
                      kind, title, body, tags, user_id, segment,
                      importance, salience, source_chat_id, status
                    )
                    VALUES (
                      'note', %s, %s, ARRAY['note'], %s, %s,
                      0.55, 0.6, %s, 'active'
                    )
                    RETURNING id
                    """,
                    (title, body, db_user_id, segment, str(chat_id or "") or None),
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            return "Couldn't save that note."
        return f"Noted #{int(row[0])}: {title}."
    except Exception as exc:
        logger.warning("life_note_save_error: %s", exc)
        return f"Note save failed: {exc}"


def list_notes_tool(*, db_user_id: int, query: Optional[str] = None, limit: int = 8) -> str:
    limit = max(1, min(int(limit or 8), 20))
    q = (query or "").strip()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if q:
                    cur.execute(
                        """
                        SELECT id, title, body FROM memory_items
                        WHERE user_id = %s AND status = 'active' AND forgotten_at IS NULL
                          AND kind = 'note'
                          AND (
                            title ILIKE '%%' || %s || '%%'
                            OR body ILIKE '%%' || %s || '%%'
                            OR search_tsv @@ plainto_tsquery('english', %s)
                          )
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (db_user_id, q, q, q, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, title, body FROM memory_items
                        WHERE user_id = %s AND status = 'active' AND forgotten_at IS NULL
                          AND kind = 'note'
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (db_user_id, limit),
                    )
                rows = cur.fetchall()
    except Exception as exc:
        return f"Notes list failed: {exc}"
    if not rows:
        return "No notes matched." if q else "No notes yet."
    lines = [f"• {title}: {(body or '')[:140]}" for _id, title, body in rows]
    return "Notes:\n" + "\n".join(lines)



# ---------------------------------------------------------------------------
# D3 — Memory (confirm for forget/correct via Redis pending shared with ingress)
# ---------------------------------------------------------------------------


def _mem_pending_key(chat_id: str) -> str:
    return f"celia:memory:pending:{chat_id}"


def _set_mem_pending(chat_id: str, payload: dict) -> None:
    payload = {**payload, "expires_at": time.time() + _MEM_PENDING_TTL}
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_mem_pending_key(chat_id), _MEM_PENDING_TTL, json.dumps(payload))
    except Exception as exc:
        logger.warning("life_mem_pending_error: %s", exc)


def _guess_memory_kind(text: str) -> str:
    low = (text or "").lower()
    if re.search(r"(?i)\b(?:prefer|preference|like(?:s)?\s+to|want(?:s)?\s+me\s+to)\b", text or ""):
        return "preference"
    if re.search(r"(?i)\b(?:goal|want\s+to|planning\s+to|aim)\b", text or ""):
        return "goal"
    if re.search(r"(?i)\b(?:decided|decision|chose|going\s+with)\b", text or ""):
        return "decision"
    return "fact"


def memory_remember_tool(
    *, db_user_id: int, chat_id: str, body: str, title: Optional[str] = None, kind: Optional[str] = None
) -> str:
    import re

    body = (body or "").strip()
    if not body:
        return "What should I remember?"
    kind = (kind or _guess_memory_kind(body)).strip() or "fact"
    if kind not in ("goal", "decision", "preference", "fact", "habit", "note", "event", "correction"):
        kind = "fact"
    if not title:
        words = body.split()
        title = " ".join(words[:8]) if words else body[:60]
    title = (title or "memory")[:80]
    segment = {
        "preference": "semantic",
        "goal": "semantic",
        "fact": "semantic",
        "habit": "procedural",
        "decision": "episodic",
        "event": "episodic",
        "note": "semantic",
        "correction": "semantic",
    }.get(kind, "semantic")
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_items(
                      kind, title, body, tags, user_id, segment,
                      importance, salience, source_chat_id, status
                    )
                    VALUES (%s, %s, %s, '{}', %s, %s, 0.7, 0.7, %s, 'active')
                    RETURNING id
                    """,
                    (kind, title, body, db_user_id, segment, str(chat_id or "") or None),
                )
                row = cur.fetchone()
            conn.commit()
        if not row:
            return "Couldn't save that."
        return f"Remembered #{int(row[0])}: {title}."
    except Exception as exc:
        logger.warning("life_memory_remember_error: %s", exc)
        return f"Remember failed: {exc}"


def memory_recall_tool(*, db_user_id: int, query: Optional[str] = None, limit: int = 10) -> str:
    limit = max(1, min(int(limit or 10), 20))
    q = (query or "").strip()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if q:
                    cur.execute(
                        """
                        SELECT id, kind, title, body FROM memory_items
                        WHERE user_id = %s AND status = 'active' AND forgotten_at IS NULL
                          AND (
                            title ILIKE '%%' || %s || '%%'
                            OR body ILIKE '%%' || %s || '%%'
                            OR search_tsv @@ plainto_tsquery('english', %s)
                            OR %s = ANY(tags)
                          )
                        ORDER BY importance DESC, salience DESC, updated_at DESC
                        LIMIT %s
                        """,
                        (db_user_id, q, q, q, q.lower(), limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, kind, title, body FROM memory_items
                        WHERE user_id = %s AND status = 'active' AND forgotten_at IS NULL
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (db_user_id, limit),
                    )
                rows = cur.fetchall()
    except Exception as exc:
        return f"Recall failed: {exc}"
    if not rows:
        return "Not much stored yet." if not q else "Don't think I have that stored."
    lines = [f"• [{kind}] {title}: {(body or '')[:120]}" for _id, kind, title, body in rows]
    return "Here's what I've got:\n" + "\n".join(lines)


def memory_forget_tool(
    *,
    db_user_id: int,
    chat_id: str,
    query: Optional[str] = None,
    memory_id: Optional[int] = None,
    confirmed: bool = False,
) -> str:
    # Resolve target
    row = None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if memory_id is not None:
                    cur.execute(
                        """
                        SELECT id, title, body FROM memory_items
                        WHERE user_id = %s AND id = %s AND forgotten_at IS NULL AND status = 'active'
                        """,
                        (db_user_id, int(memory_id)),
                    )
                    row = cur.fetchone()
                elif query:
                    cur.execute(
                        """
                        SELECT id, title, body FROM memory_items
                        WHERE user_id = %s AND status = 'active' AND forgotten_at IS NULL
                          AND (
                            title ILIKE '%%' || %s || '%%'
                            OR body ILIKE '%%' || %s || '%%'
                            OR search_tsv @@ plainto_tsquery('english', %s)
                          )
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (db_user_id, query.strip(), query.strip(), query.strip()),
                    )
                    row = cur.fetchone()
    except Exception as exc:
        return f"Forget lookup failed: {exc}"
    if not row:
        return "Don't think I have that stored."
    tid, title, body = int(row[0]), row[1], row[2] or ""
    label = title or body[:60]
    if not confirmed:
        _set_mem_pending(
            str(chat_id),
            {
                "action": "forget",
                "target_id": tid,
                "label": label,
                "reason": query or str(tid),
                "user_id": None,
            },
        )
        return (
            f"PENDING_CONFIRM: Forget “{label}”? "
            "Tell the user to reply yes to confirm (or no to cancel)."
        )
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
                    (tid, db_user_id),
                )
                ok = cur.fetchone()
                if ok:
                    cur.execute(
                        """
                        INSERT INTO memory_corrections(user_id, target_id, action, reason)
                        VALUES (%s, %s, 'forget', %s)
                        """,
                        (db_user_id, tid, query or "life-agent forget"),
                    )
            conn.commit()
        return f"Forgotten “{label}”." if ok else "Couldn't forget that."
    except Exception as exc:
        return f"Forget failed: {exc}"


def memory_correct_tool(
    *,
    db_user_id: int,
    chat_id: str,
    query: str,
    new_body: str,
    new_title: Optional[str] = None,
    kind: Optional[str] = None,
    confirmed: bool = False,
) -> str:
    query = (query or "").strip()
    new_body = (new_body or "").strip()
    if not query or not new_body:
        return "Need what to correct and the new value."
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, body FROM memory_items
                    WHERE user_id = %s AND status = 'active' AND forgotten_at IS NULL
                      AND (
                        title ILIKE '%%' || %s || '%%'
                        OR body ILIKE '%%' || %s || '%%'
                        OR search_tsv @@ plainto_tsquery('english', %s)
                      )
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (db_user_id, query, query, query),
                )
                row = cur.fetchone()
    except Exception as exc:
        return f"Correct lookup failed: {exc}"
    if not row:
        # No prior — treat as remember
        return memory_remember_tool(
            db_user_id=db_user_id,
            chat_id=chat_id,
            body=new_body,
            title=new_title or query,
            kind=kind or _guess_memory_kind(new_body),
        )
    tid, old_title, _old_body = int(row[0]), row[1], row[2]
    title = (new_title or query or old_title)[:80]
    kind = (kind or _guess_memory_kind(new_body)).strip() or "fact"
    if not confirmed:
        _set_mem_pending(
            str(chat_id),
            {
                "action": "correct",
                "target_id": tid,
                "label": old_title or query,
                "kind": kind,
                "title": title,
                "body": new_body,
                "reason": query,
                "user_id": None,
            },
        )
        return (
            f"PENDING_CONFIRM: Replace “{old_title}” with “{title}”? "
            "Tell the user to reply yes to confirm (or no to cancel)."
        )
    # apply correct
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_items(
                      kind, title, body, user_id, segment, correct_of, status,
                      importance, salience
                    )
                    VALUES (%s, %s, %s, %s, 'semantic', %s, 'active', 0.7, 0.7)
                    RETURNING id
                    """,
                    (kind, title, new_body, db_user_id, tid),
                )
                new_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    UPDATE memory_items
                    SET status = 'superseded', superseded_by = %s, updated_at = NOW(),
                        forgotten_at = COALESCE(forgotten_at, NOW())
                    WHERE id = %s AND user_id = %s
                    """,
                    (new_id, tid, db_user_id),
                )
                cur.execute(
                    """
                    INSERT INTO memory_corrections(user_id, target_id, action, reason, replacement_id)
                    VALUES (%s, %s, 'correct', %s, %s)
                    """,
                    (db_user_id, tid, query, new_id),
                )
            conn.commit()
        return f"Updated — now “{title}”."
    except Exception as exc:
        logger.warning("life_memory_correct_error: %s", exc)
        return f"Correct failed: {exc}"


# ---------------------------------------------------------------------------
# D3 — Finance receipt session prefs (Redis celia:finance:session:{chat_id})
# ---------------------------------------------------------------------------


def _finance_session_key(chat_id: str) -> str:
    return f"celia:finance:session:{chat_id}"


def _get_finance_session(chat_id: str) -> dict:
    empty = {
        "total_only": False,
        "calc_mode": False,
        "prefer_lower_text_amount": False,
        "receipts": [],
        "updated_at": time.time(),
    }
    r = _redis()
    if r is None:
        return empty
    try:
        raw = r.get(_finance_session_key(chat_id))
        if not raw:
            return empty
        data = json.loads(raw)
        if not isinstance(data, dict):
            return empty
        empty.update({
            "total_only": bool(data.get("total_only")),
            "calc_mode": bool(data.get("calc_mode")),
            "prefer_lower_text_amount": bool(data.get("prefer_lower_text_amount")),
            "receipts": list(data.get("receipts") or []),
            "updated_at": float(data.get("updated_at") or time.time()),
        })
        return empty
    except Exception as exc:
        logger.warning("life_finance_session_get_error: %s", exc)
        return empty


def _save_finance_session(chat_id: str, session: dict) -> dict:
    session = {**session, "updated_at": time.time()}
    r = _redis()
    if r is not None:
        try:
            r.set(_finance_session_key(chat_id), json.dumps(session), ex=_FINANCE_SESSION_TTL)
        except Exception as exc:
            logger.warning("life_finance_session_save_error: %s", exc)
    return session


def set_lower_text_amount_pref_tool(*, chat_id: str, enabled: bool = True) -> str:
    chat_id = str(chat_id or "")
    if not chat_id:
        return "Need chat context for receipt session."
    s = _get_finance_session(chat_id)
    s["prefer_lower_text_amount"] = bool(enabled)
    _save_finance_session(chat_id, s)
    if enabled:
        return (
            "Got it — when receipt and typed amounts differ, I'll use the lower text amount "
            "for this session."
        )
    return "Okay — turned off lower-text amount preference for this session."


def recalculate_receipts_tool(*, chat_id: str) -> str:
    chat_id = str(chat_id or "")
    if not chat_id:
        return "Need chat context for receipt session."
    s = _get_finance_session(chat_id)
    receipts = list(s.get("receipts") or [])
    if not receipts:
        return "Nothing parked to recalculate — send receipts or ask for a total first."
    prefer = bool(s.get("prefer_lower_text_amount"))
    updated = 0
    dual_missing = 0
    if prefer:
        new_receipts = []
        for r in receipts:
            entry = dict(r)
            vision = r.get("vision_amount")
            text_amt = r.get("text_amount")
            if vision is not None and text_amt is not None:
                new_amt = min(float(vision), float(text_amt))
                if abs(float(entry.get("amount") or 0) - new_amt) >= 0.001:
                    updated += 1
                entry["amount"] = new_amt
            else:
                dual_missing += 1
            new_receipts.append(entry)
        s["receipts"] = new_receipts
        _save_finance_session(chat_id, s)
        receipts = new_receipts
    total = sum(float(r.get("amount") or 0) for r in receipts)
    n = len(receipts)
    summary = f"{n} receipt{'s' if n != 1 else ''} · MVR {total:.2f}"
    if prefer and updated:
        msg = f"Updated {updated} · {summary}"
        if dual_missing:
            msg += f"\n({dual_missing} without a text amount stayed as-is.)"
        return msg
    if prefer and dual_missing and not updated:
        return (
            f"{summary}\n"
            "Preference is on — older lines without a text amount stay as-is; "
            "new receipts will use the lower text."
        )
    return summary


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
    # D2 lists
    if name == "create_list":
        return create_shopping_list(
            chat_id=str(chat_id or ""),
            title=str(args.get("title") or "List"),
            items=args.get("items"),
        )
    if name == "show_list":
        return show_shopping_list(chat_id=str(chat_id or ""))
    if name == "add_list_items":
        return add_shopping_items(chat_id=str(chat_id or ""), items=args.get("items"))
    if name == "remove_list_item":
        return remove_shopping_item(chat_id=str(chat_id or ""), query=str(args.get("query") or ""))
    if name == "clear_list":
        return clear_shopping_list(chat_id=str(chat_id or ""))
    if name == "mark_list_item_bought":
        return mark_shopping_bought(chat_id=str(chat_id or ""), query=str(args.get("query") or ""))
    # D2 calendar
    if name == "create_calendar_event":
        return create_calendar_event_tool(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            title=str(args.get("title") or ""),
            starts_at_iso=str(args.get("starts_at_iso") or ""),
            ends_at_iso=args.get("ends_at_iso"),
            location=args.get("location"),
            confirmed=bool(args.get("confirmed") or False),
        )
    if name == "list_calendar_events":
        return list_calendar_events_tool(
            db_user_id=db_uid,
            days_ahead=args.get("days_ahead") or 7,
            start_iso=args.get("start_iso"),
            end_iso=args.get("end_iso"),
        )
    # D2 notes
    if name == "add_note":
        return add_note_tool(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            body=str(args.get("body") or args.get("text") or ""),
            title=args.get("title"),
        )
    if name == "list_notes":
        return list_notes_tool(
            db_user_id=db_uid,
            query=args.get("query"),
            limit=args.get("limit") or 8,
        )
    # D3 memory
    if name == "memory_remember":
        return memory_remember_tool(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            body=str(args.get("body") or args.get("text") or ""),
            title=args.get("title"),
            kind=args.get("kind"),
        )
    if name == "memory_recall":
        return memory_recall_tool(
            db_user_id=db_uid,
            query=args.get("query"),
            limit=args.get("limit") or 10,
        )
    if name == "memory_forget":
        mid = args.get("memory_id")
        try:
            mid_i = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid_i = None
        return memory_forget_tool(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            query=args.get("query"),
            memory_id=mid_i,
            confirmed=bool(args.get("confirmed") or False),
        )
    if name == "memory_correct":
        return memory_correct_tool(
            db_user_id=db_uid,
            chat_id=str(chat_id or ""),
            query=str(args.get("query") or ""),
            new_body=str(args.get("new_body") or args.get("body") or ""),
            new_title=args.get("new_title") or args.get("title"),
            kind=args.get("kind"),
            confirmed=bool(args.get("confirmed") or False),
        )
    # D3 finance session
    if name == "set_lower_text_amount_pref":
        enabled = args.get("enabled")
        if enabled is None:
            enabled = True
        return set_lower_text_amount_pref_tool(
            chat_id=str(chat_id or ""),
            enabled=bool(enabled),
        )
    if name == "recalculate_receipts":
        return recalculate_receipts_tool(chat_id=str(chat_id or ""))
    return f"(unhandled life tool: {name})"
