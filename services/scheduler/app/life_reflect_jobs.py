"""Life-reflect — optional proactive life ping (Life OS slice 5).

Separate from ops-reflect. May only lead to recall_memory + notify_user on the
worker (no shell). Does NOT duplicate weekly/monthly finance digests.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from redis import Redis

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DISPATCH_STREAM = os.getenv("DISPATCH_STREAM", "orchestration.dispatched")

LIFE_REFLECT_JOB_ID = "life-reflect-periodic"
LIFE_REFLECT_ENABLED = os.getenv("LIFE_REFLECT_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
)
# Minimum hours between dispatches per chat (rate limit).
LIFE_REFLECT_MIN_INTERVAL_HOURS = float(
    os.getenv("LIFE_REFLECT_MIN_INTERVAL_HOURS", "6")
)
# Cron: 09:00 and 18:00 Indian/Maldives (UTC+5) → 04:00 and 13:00 UTC
LIFE_REFLECT_CRON_HOURS_UTC = os.getenv("LIFE_REFLECT_CRON_HOURS_UTC", "4,13")
_RATE_KEY = "celia:life_reflect:last_dispatch:{chat_id}"


def _conn():
    return psycopg.connect(DATABASE_URL)


def _redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _list_targets() -> list[dict[str, int]]:
    """Authorized/active users with a Telegram id (same spirit as digests)."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT u.id, u.telegram_user_id
                    FROM users u
                    WHERE u.is_active = TRUE
                      AND u.telegram_user_id IS NOT NULL
                      AND u.role IN ('authorized', 'admin', 'owner')
                    ORDER BY u.id
                    """
                )
                rows = cur.fetchall()
        if rows:
            return [
                {"user_id": int(r[0]), "telegram_user_id": int(r[1])}
                for r in rows
                if r[1] is not None
            ]
    except Exception as exc:
        logger.error("life_reflect_targets_error: %s", exc)

    # Fallback: ALLOWED_TELEGRAM_USER_IDS (no DB role yet)
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
    out: list[dict[str, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            tid = int(part)
            out.append({"user_id": 0, "telegram_user_id": tid})
    return out


def _build_snapshot(user_id: int) -> str:
    """Brief due reminders / open tasks / near agenda for the prompt.

    Empty / thin snapshots are fine — silent is the default for life-reflect.
    Never includes finance digest totals (those have their own Sunday/month jobs).
    """
    if not user_id:
        return "(no user row yet — skip structured snapshot)"
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=48)
    lines: list[str] = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, run_at, kind
                    FROM reminders
                    WHERE user_id = %s AND status = 'active'
                      AND run_at IS NOT NULL
                      AND run_at <= %s
                    ORDER BY run_at
                    LIMIT 8
                    """,
                    (user_id, horizon),
                )
                for title, run_at, kind in cur.fetchall():
                    lines.append(f"- reminder ({kind or 'once'}): {title} @ {run_at}")

                cur.execute(
                    """
                    SELECT title, due_at
                    FROM tasks
                    WHERE user_id = %s AND status = 'open'
                      AND (
                        due_at IS NULL
                        OR due_at <= %s
                      )
                    ORDER BY due_at NULLS LAST, id
                    LIMIT 8
                    """,
                    (user_id, horizon),
                )
                for title, due_at in cur.fetchall():
                    due_s = f" due {due_at}" if due_at else ""
                    lines.append(f"- task: {title}{due_s}")

                cur.execute(
                    """
                    SELECT title, starts_at, location
                    FROM calendar_events
                    WHERE user_id = %s AND status = 'active'
                      AND starts_at < %s
                      AND ends_at > %s
                    ORDER BY starts_at
                    LIMIT 8
                    """,
                    (user_id, horizon, now - timedelta(hours=1)),
                )
                for title, starts_at, location in cur.fetchall():
                    loc = f" @ {location}" if location else ""
                    lines.append(f"- cal: {title} @ {starts_at}{loc}")
    except Exception as exc:
        logger.warning("life_reflect_snapshot_error: user_id=%s err=%s", user_id, exc)
        return f"(snapshot error: {exc})"

    if not lines:
        return "(nothing notable on reminders/tasks/agenda in the next ~48h)"
    return "Snapshot (next ~48h):\n" + "\n".join(lines)


def _rate_ok(chat_id: str) -> bool:
    r = _redis()
    key = _RATE_KEY.format(chat_id=chat_id)
    try:
        if r.get(key):
            return False
        return True
    except Exception as exc:
        logger.warning("life_reflect_rate_check_error: %s", exc)
        return True


def _mark_dispatched(chat_id: str) -> None:
    r = _redis()
    key = _RATE_KEY.format(chat_id=chat_id)
    ttl = max(60, int(LIFE_REFLECT_MIN_INTERVAL_HOURS * 3600))
    try:
        r.setex(key, ttl, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        logger.warning("life_reflect_rate_mark_error: %s", exc)


def _dispatch(chat_id: str, user_id: int, snapshot: str) -> None:
    r = _redis()
    cid = str(uuid.uuid4())
    text = (
        "Life-reflect cycle (proactive — he did not ask). "
        "You may ONLY use recall_memory and/or notify_user. No shell. "
        "Silent is the default. Only notify if something is genuinely worth "
        "his time right now (due reminder, open task, soon agenda). "
        "Use recall_memory first if unsure — respect dismissals / "
        "'don't remind me' preferences in memory. "
        "Do NOT restate weekly/monthly money digests. Keep any ping short and quiet.\n\n"
        f"{snapshot}"
    )
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": str(uuid.uuid4()),
        "agent_role": "life-reflect",
        "text": text,
        "chat_id": chat_id,
        "thread_id": "",
        "user_id": str(chat_id),
        "correlation_id": cid,
    }
    r.xadd(DISPATCH_STREAM, {"payload": json.dumps(event)})
    logger.info(
        "life_reflect_dispatched: chat_id=%s user_id=%s", chat_id, user_id
    )


def run_life_reflect(*, force: bool = False) -> dict[str, Any]:
    """Entry point for the cron (and dry/test fire)."""
    stats: dict[str, Any] = {
        "enabled": LIFE_REFLECT_ENABLED,
        "dispatched": 0,
        "skipped_rate": 0,
        "skipped_disabled": 0,
        "targets": 0,
        "force": force,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if not LIFE_REFLECT_ENABLED and not force:
        stats["skipped_disabled"] = 1
        logger.info("life_reflect_skipped_disabled")
        return stats

    targets = _list_targets()
    stats["targets"] = len(targets)
    for t in targets:
        chat_id = str(t["telegram_user_id"])
        if not force and not _rate_ok(chat_id):
            stats["skipped_rate"] += 1
            continue
        snapshot = _build_snapshot(int(t["user_id"])) if t["user_id"] else (
            "(no structured snapshot)"
        )
        try:
            _dispatch(chat_id, int(t["user_id"]), snapshot)
            _mark_dispatched(chat_id)
            stats["dispatched"] += 1
        except Exception as exc:
            logger.error("life_reflect_dispatch_error: chat_id=%s err=%s", chat_id, exc)
    logger.info("life_reflect_done: %s", stats)
    return stats


def life_reflect_job() -> None:
    run_life_reflect(force=False)


def register_life_reflect_jobs(scheduler) -> None:
    """Idempotent cron registration. Hours UTC via LIFE_REFLECT_CRON_HOURS_UTC."""
    from apscheduler.triggers.cron import CronTrigger

    if not LIFE_REFLECT_ENABLED:
        existing = scheduler.get_job(LIFE_REFLECT_JOB_ID)
        if existing:
            try:
                scheduler.remove_job(LIFE_REFLECT_JOB_ID)
                logger.info("life_reflect_job_removed_disabled: %s", LIFE_REFLECT_JOB_ID)
            except Exception as exc:
                logger.warning("life_reflect_remove_error: %s", exc)
        else:
            logger.info("life_reflect_job_not_registered_disabled")
        return

    hours = []
    for part in LIFE_REFLECT_CRON_HOURS_UTC.split(","):
        part = part.strip()
        if part.isdigit():
            hours.append(int(part))
    if not hours:
        hours = [4, 13]

    scheduler.add_job(
        life_reflect_job,
        trigger=CronTrigger(hour=",".join(str(h) for h in hours), minute=15, timezone="UTC"),
        id=LIFE_REFLECT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "life_reflect_job_registered: id=%s hours_utc=%s min_interval_h=%s",
        LIFE_REFLECT_JOB_ID,
        hours,
        LIFE_REFLECT_MIN_INTERVAL_HOURS,
    )
