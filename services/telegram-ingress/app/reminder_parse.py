"""Natural-language parse for reminders + dated tasks (Life OS slice 2).

Timezone: Indian/Maldives (MVT, UTC+5). All returned datetimes are timezone-aware.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

USER_TZ = ZoneInfo("Indian/Maldives")
UTC = timezone.utc

Kind = Literal["once", "cron"]


@dataclass
class ReminderSpec:
    kind: Kind
    title: str
    run_at: Optional[datetime] = None  # UTC aware for once
    cron_expr: Optional[str] = None  # 5-field, interpreted in USER_TZ
    local_when: str = ""  # human label for quiet confirm


@dataclass
class TaskSpec:
    title: str
    due_at: Optional[datetime] = None  # UTC aware
    list_name: Optional[str] = None
    local_when: str = ""


_DOW = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
    "mon": "mon",
    "tue": "tue",
    "tues": "tue",
    "wed": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "fri": "fri",
    "sat": "sat",
    "sun": "sun",
}

_REMIND_HEAD = re.compile(
    r"(?i)^(?:please\s+)?(?:remind\s+me|set\s+(?:a\s+)?reminder|reminder)\s*"
    r"(?:to\s+|about\s+|for\s+|that\s+)?"
)
_EVERY_DOW = re.compile(
    r"(?i)\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
    r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?"
)
_IN_REL = re.compile(
    r"(?i)\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b"
)
_AT_CLOCK = re.compile(
    r"(?i)\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
)
_NAMED_DAY = re.compile(
    r"(?i)\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
)
_DUE = re.compile(r"(?i)\bdue\b")

_TASK_HEAD = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"todo[:\s]+|"
    r"to-?do[:\s]+|"
    r"add\s+(?:a\s+)?(?:task|todo|to-?do)\s*[:\s]*|"
    r"task[:\s]+|"
    r"add\s+to\s+(?:my\s+)?(?:tasks?|todos?)\s*[:\s]*"
    r")(.+)$"
)
_TASK_COMPLETE = re.compile(
    r"(?i)^(?:(?:complete|finish|done|check\s*off|mark\s+done)\s+(?:task\s+|todo\s+)?(.+)"
    r"|(.+?)\s+(?:is\s+)?(?:done|complete|finished))\s*$"
)
_TASK_DELETE = re.compile(
    r"(?i)^(?:(?:delete|remove|drop|cancel)\s+(?:task\s+|todo\s+)(.+))\s*$"
)
_LIST_REMINDERS = re.compile(
    r"(?i)^(?:(?:list|show|what(?:'s|\s+are)|my)\s+reminders?"
    r"|reminders?\s*(?:list|please)?|/reminders?)\s*$"
    r"|^(?:what(?:'s|\s+is)\s+due(?:\s+(?:today|soon))?)\s*$"
)
_LIST_TASKS = re.compile(
    r"(?i)^(?:(?:list|show|my)\s+(?:tasks?|todos?|to-?dos?)"
    r"|tasks?\s*(?:list|please)?|/tasks?|/todos?)\s*$"
)
_CANCEL_REM = re.compile(
    r"(?i)^(?:(?:cancel|delete|drop|stop|remove)\s+(?:the\s+)?(?:reminder\s+)?(.+)"
    r"|cancel\s+reminder\s+#?(\d+))\s*$"
)
_SNOOZE = re.compile(
    r"(?i)^(?:snooze(?:\s+(?:that|it|reminder))?(?:\s+(?:for\s+)?(\d+)\s*"
    r"(minutes?|mins?|hours?|hrs?))?|snooze)\s*$"
)
_EDIT_REM = re.compile(
    r"(?i)^(?:(?:edit|change|reschedule|move)\s+(?:the\s+)?reminder\s+(.+))\s*$"
)


def now_local(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(USER_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=USER_TZ)
    return now.astimezone(USER_TZ)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=USER_TZ)
    return dt.astimezone(UTC)


def _parse_hour(h: int, minute: int, ampm: Optional[str]) -> tuple[int, int]:
    if ampm:
        ap = ampm.lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
    elif h <= 6:  # bare small hour → assume PM for life reminders? keep as-is morning
        pass
    return h % 24, minute


def _next_named_day(local: datetime, name: str) -> datetime:
    name = name.lower()
    if name == "today":
        return local
    if name == "tonight":
        return local.replace(hour=20, minute=0, second=0, microsecond=0)
    if name == "tomorrow":
        return (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    target = {
        "mon": 0,
        "tue": 1,
        "wed": 2,
        "thu": 3,
        "fri": 4,
        "sat": 5,
        "sun": 6,
    }[_DOW[name]]
    days_ahead = (target - local.weekday()) % 7
    if days_ahead == 0:
        # later today if clock still ahead; else next week — caller sets time
        days_ahead = 0
    return (local + timedelta(days=days_ahead)).replace(second=0, microsecond=0)


def _strip_when_clauses(text: str) -> str:
    t = text
    t = _EVERY_DOW.sub(" ", t)
    t = _IN_REL.sub(" ", t)
    t = _AT_CLOCK.sub(" ", t)
    t = re.sub(
        r"(?i)\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
        " ",
        t,
    )
    t = re.sub(r"(?i)\bdue\b", " ", t)
    t = re.sub(r"(?i)\b(?:to|about|for|that|me|please|at|on)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,!:;-")
    return t


def parse_reminder(text: str, *, now: Optional[datetime] = None) -> Optional[ReminderSpec]:
    raw = (text or "").strip()
    if not raw:
        return None
    if not re.search(r"(?i)\bremind|\breminder\b", raw):
        return None
    # Drop head
    body = _REMIND_HEAD.sub("", raw).strip()
    if not body:
        body = raw
    local = now_local(now)

    m_every = _EVERY_DOW.search(raw)
    if m_every:
        dow = _DOW[m_every.group(1).lower()]
        hour, minute = 9, 0
        if m_every.group(2):
            hour, minute = _parse_hour(
                int(m_every.group(2)),
                int(m_every.group(3) or 0),
                m_every.group(4),
            )
        title = _strip_when_clauses(body) or "reminder"
        cron = f"{minute} {hour} * * {dow}"
        label = f"every {m_every.group(1).lower()}" + (
            f" at {hour:02d}:{minute:02d} MVT" if True else ""
        )
        return ReminderSpec(kind="cron", title=title[:120], cron_expr=cron, local_when=label)

    m_in = _IN_REL.search(raw)
    if m_in:
        n = int(m_in.group(1))
        unit = m_in.group(2).lower()
        if unit.startswith("min"):
            delta = timedelta(minutes=n)
            label = f"in {n} min"
        elif unit.startswith("hour") or unit.startswith("hr"):
            delta = timedelta(hours=n)
            label = f"in {n} hour" + ("s" if n != 1 else "")
        elif unit.startswith("day"):
            delta = timedelta(days=n)
            label = f"in {n} day" + ("s" if n != 1 else "")
        else:
            delta = timedelta(weeks=n)
            label = f"in {n} week" + ("s" if n != 1 else "")
        run_local = local + delta
        title = _strip_when_clauses(body) or "reminder"
        return ReminderSpec(
            kind="once",
            title=title[:120],
            run_at=to_utc(run_local),
            local_when=label,
        )

    # Absolute-ish: day + optional clock
    m_day = _NAMED_DAY.search(raw)
    m_clock = _AT_CLOCK.search(raw)
    if m_day or m_clock:
        base = local
        if m_day:
            base = _next_named_day(local, m_day.group(1))
            if m_day.group(1).lower() == "tonight" and not m_clock:
                run_local = base
            elif m_day.group(1).lower() == "tomorrow" and not m_clock:
                run_local = base
            else:
                hour, minute = (9, 0)
                if m_clock:
                    hour, minute = _parse_hour(
                        int(m_clock.group(1)),
                        int(m_clock.group(2) or 0),
                        m_clock.group(3),
                    )
                run_local = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if run_local <= local:
                    run_local = run_local + timedelta(days=7 if m_day and m_day.group(1).lower() in _DOW else 1)
        else:
            hour, minute = _parse_hour(
                int(m_clock.group(1)),
                int(m_clock.group(2) or 0),
                m_clock.group(3),
            )
            run_local = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_local <= local:
                run_local = run_local + timedelta(days=1)
        title = _strip_when_clauses(body) or "reminder"
        label = run_local.strftime("%a %d %b %H:%M MVT")
        return ReminderSpec(
            kind="once",
            title=title[:120],
            run_at=to_utc(run_local),
            local_when=label,
        )

    return None


def parse_task(text: str, *, now: Optional[datetime] = None) -> Optional[TaskSpec]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _TASK_HEAD.match(raw)
    if not m:
        # "buy milk due friday" without todo head — only if due present and not list/remind
        if not _DUE.search(raw):
            return None
        if re.search(r"(?i)\bremind|\blist\b|\bspent\b|\bbudget\b", raw):
            return None
        rest = raw
    else:
        rest = m.group(1).strip()

    local = now_local(now)
    due_at = None
    label = ""
    list_name = None

    lm = re.search(r"(?i)\b(?:on|in)\s+(?:my\s+)?([A-Za-z][\w\s-]{0,30}?)\s+(?:list|project)\b", rest)
    if lm:
        list_name = lm.group(1).strip().title()
        rest = rest[: lm.start()] + rest[lm.end() :]

    m_in = _IN_REL.search(rest)
    m_day = _NAMED_DAY.search(rest)
    m_clock = _AT_CLOCK.search(rest)
    if m_in:
        n = int(m_in.group(1))
        unit = m_in.group(2).lower()
        if unit.startswith("min"):
            delta = timedelta(minutes=n)
        elif unit.startswith("hour") or unit.startswith("hr"):
            delta = timedelta(hours=n)
        elif unit.startswith("day"):
            delta = timedelta(days=n)
        else:
            delta = timedelta(weeks=n)
        due_local = local + delta
        due_at = to_utc(due_local)
        label = due_local.strftime("%a %d %b %H:%M MVT")
        rest = _IN_REL.sub(" ", rest)
    elif m_day or m_clock or _DUE.search(rest):
        base = local
        if m_day:
            base = _next_named_day(local, m_day.group(1))
        hour, minute = 17, 0
        if m_clock:
            hour, minute = _parse_hour(
                int(m_clock.group(1)),
                int(m_clock.group(2) or 0),
                m_clock.group(3),
            )
        due_local = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due_local <= local:
            due_local = due_local + timedelta(days=1)
        due_at = to_utc(due_local)
        label = due_local.strftime("%a %d %b %H:%M MVT")
        rest = _NAMED_DAY.sub(" ", rest)
        rest = _AT_CLOCK.sub(" ", rest)
        rest = _DUE.sub(" ", rest)

    title = re.sub(r"\s+", " ", rest).strip(" .,!:;-")
    if not title:
        return None
    return TaskSpec(title=title[:160], due_at=due_at, list_name=list_name, local_when=label)


def looks_like_reminder(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _LIST_REMINDERS.match(t) or _SNOOZE.match(t):
        return True
    if _CANCEL_REM.match(t) and re.search(r"(?i)remind", t):
        return True
    if _EDIT_REM.match(t):
        return True
    if parse_reminder(t) is not None:
        return True
    return bool(re.search(r"(?i)^(?:please\s+)?remind\b", t))


def looks_like_task(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _LIST_TASKS.match(t):
        return True
    if _TASK_COMPLETE.match(t) and re.search(r"(?i)\b(?:task|todo|to-?do)\b", t):
        return True
    if _TASK_DELETE.match(t):
        return True
    if _TASK_HEAD.match(t):
        return True
    # dated task without head
    if _DUE.search(t) and not re.search(r"(?i)\bremind|\blist\b|\bspent\b", t):
        return parse_task(t) is not None
    return False


def parse_cancel_query(text: str) -> Optional[str]:
    m = _CANCEL_REM.match((text or "").strip())
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def parse_complete_query(text: str) -> Optional[str]:
    m = _TASK_COMPLETE.match((text or "").strip())
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def parse_delete_task_query(text: str) -> Optional[str]:
    m = _TASK_DELETE.match((text or "").strip())
    if not m:
        return None
    return (m.group(1) or "").strip() or None


def parse_snooze_delta(text: str) -> timedelta:
    m = _SNOOZE.match((text or "").strip())
    if not m or not m.group(1):
        return timedelta(minutes=30)
    n = int(m.group(1))
    unit = (m.group(2) or "minutes").lower()
    if unit.startswith("hour") or unit.startswith("hr"):
        return timedelta(hours=n)
    return timedelta(minutes=n)


def is_list_reminders(text: str) -> bool:
    return bool(_LIST_REMINDERS.match((text or "").strip()))


def is_list_tasks(text: str) -> bool:
    return bool(_LIST_TASKS.match((text or "").strip()))


def bundle_window_key(run_at_utc: datetime, window_sec: int = 120) -> str:
    """Bucket run_at into windows for cheap same-slot bundling."""
    ts = int(run_at_utc.timestamp())
    bucket = ts - (ts % window_sec)
    return f"b{bucket}"
