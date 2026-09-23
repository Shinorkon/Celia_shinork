"""Ingress-local tasks + reminders (Life OS slice 2). Quiet voice; no brochures."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from app.side_effect_policy import policy_for
from app import task_store as store
from app.reminder_parse import (
    USER_TZ,
    bundle_window_key,
    is_list_reminders,
    is_list_tasks,
    looks_like_reminder,
    looks_like_task,
    parse_cancel_query,
    parse_complete_query,
    parse_delete_task_query,
    parse_reminder,
    parse_snooze_delta,
    parse_task,
)

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, str], bool]


def _fmt_when(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(USER_TZ)
    return local.strftime("%a %d %b %H:%M MVT")


def _fmt_reminder_line(r: dict) -> str:
    if r.get("kind") == "cron" and r.get("cron_expr"):
        parts = (r["cron_expr"] or "").split()
        if len(parts) == 5:
            minute, hour, _, _, dow = parts
            return f"#{r['id']} {r['title']} (every {dow} {int(hour):02d}:{int(minute):02d} MVT)"
        return f"#{r['id']} {r['title']} (recurring)"
    when = _fmt_when(r.get("run_at"))
    return f"#{r['id']} {r['title']}" + (f" — {when}" if when else "")


def _fmt_task_line(t: dict) -> str:
    when = _fmt_when(t.get("due_at"))
    list_bit = f" [{t['list_name']}]" if t.get("list_name") else ""
    return f"#{t['id']} {t['title']}{list_bit}" + (f" — due {when}" if when else "")


def try_handle_tasks(
    text: str,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    chat_type: str,
    send: SendFn,
) -> Optional[str]:
    if chat_type != "private":
        return None
    t = (text or "").strip()
    if not t:
        return None

    # Third-party notify never auto — refuse here if phrasing asks to ping someone else.
    if re.search(
        r"(?i)\bremind\b.+\b(?:them|him|her|the\s+team|everyone|group)\b"
        r"|\bsend\s+(?:a\s+)?reminder\s+to\b",
        t,
    ):
        if policy_for("comms.third_party") != "auto":
            send(chat_id, "I only self-ping unless you confirm a third-party send.", thread_id)
            return "reminder_third_party_blocked"

    is_rem = looks_like_reminder(t)
    is_task = looks_like_task(t)
    if not is_rem and not is_task:
        return None

    db_user_id = store.ensure_user(telegram_user_id)
    if db_user_id is None:
        send(chat_id, "Couldn't reach the task store.", thread_id)
        return "task_store_unavailable"

    # --- list ---
    if is_list_reminders(t):
        rows = store.list_active_reminders(db_user_id)
        if not rows:
            send(chat_id, "No reminders set.", thread_id)
            return "reminder_list_empty"
        lines = [_fmt_reminder_line(r) for r in rows]
        body = "\n".join(lines) if len(lines) > 3 else "; ".join(lines)
        send(chat_id, f"Reminders:\n{body}" if len(lines) > 3 else f"Reminders: {body}.", thread_id)
        return "reminder_listed"

    if is_list_tasks(t):
        rows = store.list_open_tasks(db_user_id)
        if not rows:
            send(chat_id, "No open tasks.", thread_id)
            return "task_list_empty"
        lines = [_fmt_task_line(r) for r in rows]
        body = "\n".join(lines) if len(lines) > 3 else "; ".join(lines)
        send(chat_id, f"Tasks:\n{body}" if len(lines) > 3 else f"Tasks: {body}.", thread_id)
        return "task_listed"

    # --- snooze ---
    if re.match(r"(?i)^snooze\b", t):
        rem = store.latest_active_reminder(db_user_id)
        if not rem:
            send(chat_id, "Nothing to snooze.", thread_id)
            return "reminder_snooze_empty"
        delta = parse_snooze_delta(t)
        new_at = datetime.now(timezone.utc) + delta
        ok = store.snooze_reminder(
            db_user_id,
            rem["id"],
            new_run_at=new_at,
            chat_id=chat_id,
            thread_id=thread_id,
            telegram_user_id=str(telegram_user_id),
        )
        send(
            chat_id,
            f"Snoozed #{rem['id']} until {_fmt_when(new_at)}." if ok else "Couldn't snooze.",
            thread_id,
        )
        return "reminder_snoozed" if ok else "reminder_snooze_failed"

    # --- cancel reminder ---
    if is_rem and re.search(r"(?i)\b(?:cancel|delete|drop|stop|remove)\b", t):
        q = parse_cancel_query(t) or re.sub(
            r"(?i)^(?:cancel|delete|drop|stop|remove)\s+(?:the\s+)?(?:reminder\s+)?",
            "",
            t,
        ).strip()
        cands = store.find_reminders(db_user_id, q)
        if not cands:
            send(chat_id, "No matching reminder.", thread_id)
            return "reminder_cancel_miss"
        # cancel/edit sensible = auto (self)
        target = cands[0]
        ok = store.cancel_reminder(db_user_id, target["id"])
        send(
            chat_id,
            f"Cancelled #{target['id']} {target['title']}." if ok else "Couldn't cancel.",
            thread_id,
        )
        return "reminder_cancelled" if ok else "reminder_cancel_failed"

    # --- create reminder ---
    if is_rem:
        spec = parse_reminder(t)
        if spec is None:
            send(chat_id, "Say when — e.g. in 3 hours, every Monday, Thursday 9am.", thread_id)
            return "reminder_clarify"
        # Self-ping create = auto (Phase C spirit)
        bkey = None
        if spec.kind == "once" and spec.run_at is not None:
            bkey = bundle_window_key(spec.run_at)
        rem = store.create_reminder(
            db_user_id=db_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            title=spec.title,
            kind=spec.kind,
            run_at=spec.run_at,
            cron_expr=spec.cron_expr,
            telegram_user_id=str(telegram_user_id),
            bundle_key=bkey,
            notify_text=f"Reminder: {spec.title}",
        )
        if not rem:
            send(chat_id, "Couldn't set that reminder.", thread_id)
            return "reminder_create_failed"
        if rem.get("bundled"):
            send(chat_id, f"Bundled with #{rem['id']} ({spec.local_when}).", thread_id)
            return "reminder_bundled"
        when = spec.local_when or _fmt_when(spec.run_at)
        send(chat_id, f"Okay — #{rem['id']} {spec.title} ({when}).", thread_id)
        return "reminder_created"

    # --- task complete / delete ---
    if re.search(r"(?i)\b(?:complete|finish|done|check\s*off|mark\s+done)\b", t) and is_task:
        q = parse_complete_query(t) or ""
        cands = store.find_open_tasks(db_user_id, q)
        if not cands:
            send(chat_id, "No matching task.", thread_id)
            return "task_complete_miss"
        ok = store.complete_task(db_user_id, cands[0]["id"])
        send(
            chat_id,
            f"Done — #{cands[0]['id']} {cands[0]['title']}." if ok else "Couldn't complete.",
            thread_id,
        )
        return "task_completed" if ok else "task_complete_failed"

    if re.search(r"(?i)\b(?:delete|remove|drop|cancel)\s+task\b", t) or (
        is_task and re.search(r"(?i)^(?:delete|remove|drop)\s+todo\b", t)
    ):
        q = parse_delete_task_query(t) or ""
        cands = store.find_open_tasks(db_user_id, q)
        if not cands:
            send(chat_id, "No matching task.", thread_id)
            return "task_delete_miss"
        ok = store.delete_task(db_user_id, cands[0]["id"])
        send(
            chat_id,
            f"Removed #{cands[0]['id']}." if ok else "Couldn't remove.",
            thread_id,
        )
        return "task_deleted" if ok else "task_delete_failed"

    # --- create task ---
    spec_t = parse_task(t)
    if spec_t is None:
        return None
    list_id = None
    if spec_t.list_name:
        list_id = store.get_or_create_list(db_user_id, spec_t.list_name)
    task = store.create_task(
        db_user_id=db_user_id,
        title=spec_t.title,
        due_at=spec_t.due_at,
        list_id=list_id,
    )
    if not task:
        send(chat_id, "Couldn't save that task.", thread_id)
        return "task_create_failed"

    rem_note = ""
    if spec_t.due_at is not None:
        rem = store.create_reminder(
            db_user_id=db_user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            title=spec_t.title,
            kind="once",
            run_at=spec_t.due_at,
            telegram_user_id=str(telegram_user_id),
            task_id=task["id"],
            bundle_key=bundle_window_key(spec_t.due_at),
            notify_text=f"Task due: {spec_t.title}",
        )
        if rem:
            store.link_task_reminder(db_user_id, task["id"], rem["id"])
            rem_note = f" Reminder #{rem['id']} set."

    when = spec_t.local_when or _fmt_when(spec_t.due_at)
    due_bit = f" due {when}" if when else ""
    send(chat_id, f"Task #{task['id']} {spec_t.title}{due_bit}.{rem_note}", thread_id)
    return "task_created"
