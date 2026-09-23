"""NL parse for Celia-only calendar (Life OS slice 4). TZ: Indian/Maldives."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

from app.reminder_parse import (
    USER_TZ,
    _AT_CLOCK,
    _DOW,
    _NAMED_DAY,
    _next_named_day,
    _parse_hour,
    now_local,
    to_utc,
)

RangeKind = Literal["today", "week", "custom"]


@dataclass
class EventSpec:
    title: str
    starts_at: datetime  # UTC aware
    ends_at: datetime  # UTC aware
    location: Optional[str] = None
    local_when: str = ""


@dataclass
class AgendaSpec:
    kind: RangeKind
    start_local: datetime
    end_local: datetime
    label: str


_ADD_HEAD = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"add\s+(?:an?\s+)?(?:event|appointment|meeting)\s*(?:for\s+|called\s+|titled\s+)?"
    r"|schedule\s+(?:an?\s+)?(?:event|appointment|meeting)?\s*"
    r"|put\s+(?:an?\s+)?(?:event\s+)?"
    r"|calendar\s*[:\-]\s*"
    r"|book\s+(?:an?\s+)?"
    r")(.+)$"
)

_AGENDA_RE = re.compile(
    r"(?i)^(?:"
    r"what(?:'s|\s+is|\s+are)\s+on(?:\s+(?:my\s+)?(?:calendar|schedule|agenda))?"
    r"(?:\s+(?:for\s+)?(?:today|tomorrow|this\s+week|the\s+week))?"
    r"|whats\s+on(?:\s+(?:my\s+)?(?:calendar|schedule|agenda))?"
    r"(?:\s+(?:for\s+)?(?:today|tomorrow|this\s+week|the\s+week))?"
    r"|agenda(?:\s+(?:for\s+)?(?:today|tomorrow|this\s+week|the\s+week))?"
    r"|calendar(?:\s+(?:today|this\s+week))?"
    r"|/(?:agenda|calendar|events)"
    r"|show\s+(?:my\s+)?(?:calendar|agenda|schedule|events)"
    r"|list\s+(?:my\s+)?(?:calendar|events|appointments)"
    r"|what(?:'s|\s+is)\s+on\s+this\s+week"
    r"|whats\s+on\s+this\s+week"
    r"|what(?:'s|\s+is)\s+on\s+today"
    r"|whats\s+on\s+today"
    r")\s*$"
)

_UPDATE_HEAD = re.compile(
    r"(?i)^(?:please\s+)?(?:"
    r"(?:move|reschedule|change|update)\s+(?:(?:the\s+)?(?:event|appointment|meeting)\s+)?"
    r"|edit\s+(?:(?:the\s+)?(?:event|appointment)\s+)?"
    r")(.+)$"
)

_DURATION = re.compile(
    r"(?i)\b(?:for\s+)?(\d+)\s*(hours?|hrs?|minutes?|mins?)\b"
)

_BARE_CLOCK = re.compile(
    r"(?i)(?<!\w)(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?!\w)"
    r"|(?<!\w)([01]?\d|2[0-3]):([0-5]\d)(?!\w)"
)


def looks_like_calendar(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _AGENDA_RE.match(t):
        return True
    if _ADD_HEAD.match(t):
        return True
    if _UPDATE_HEAD.match(t) and re.search(
        r"(?i)\b(?:event|appointment|meeting|calendar|to\s+(?:monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday|today|tomorrow)|\d{1,2}\s*(?:am|pm))\b",
        t,
    ):
        return True
    if re.search(
        r"(?i)\b(?:add|schedule|book)\b.+\b(?:event|appointment|meeting|calendar)\b", t
    ):
        return True
    if re.search(r"(?i)\bon\s+(?:my\s+)?calendar\b", t):
        return True
    if re.match(r"(?i)^(?:add|schedule)\s+event\b", t):
        return True
    return False


def is_agenda_query(text: str) -> bool:
    return bool(_AGENDA_RE.match((text or "").strip()))


def parse_agenda(text: str, *, now: Optional[datetime] = None) -> AgendaSpec:
    local = now_local(now)
    t = (text or "").strip().lower()
    if re.search(r"\btoday\b", t):
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return AgendaSpec("today", start, end, "today")
    if re.search(r"\btomorrow\b", t):
        start = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=1)
        return AgendaSpec("today", start, end, "tomorrow")
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days_to_sun = (6 - local.weekday()) % 7
    end = (start + timedelta(days=days_to_sun + 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if end <= start:
        end = start + timedelta(days=7)
    return AgendaSpec("week", start, end, "this week")


def _strip_when_and_meta(text: str) -> str:
    t = text
    t = _DURATION.sub(" ", t)
    t = _AT_CLOCK.sub(" ", t)
    t = _BARE_CLOCK.sub(" ", t)
    t = re.sub(
        r"(?i)\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
        " ",
        t,
    )
    t = re.sub(
        r"(?i)\b(?:on|at|for|from|until|till|to|the|an|a|please|called|titled)\b",
        " ",
        t,
    )
    t = re.sub(r"(?i)\b(?:event|appointment|meeting)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,!:;-")
    return t


def _extract_clock(raw: str) -> Optional[tuple[int, int]]:
    m = _AT_CLOCK.search(raw)
    if m:
        return _parse_hour(int(m.group(1)), int(m.group(2) or 0), m.group(3))
    m = _BARE_CLOCK.search(raw)
    if not m:
        return None
    if m.group(1) is not None:
        return _parse_hour(int(m.group(1)), int(m.group(2) or 0), m.group(3))
    return int(m.group(4)) % 24, int(m.group(5))


def parse_event(text: str, *, now: Optional[datetime] = None) -> Optional[EventSpec]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = _ADD_HEAD.match(raw)
    body = m.group(1).strip() if m else raw
    body = re.sub(r"(?i)\s+on\s+(?:my\s+)?calendar\s*$", "", body).strip()

    local = now_local(now)
    m_day = _NAMED_DAY.search(raw)
    clock = _extract_clock(raw)
    if not m_day and clock is None:
        return None

    base = local
    if m_day:
        base = _next_named_day(local, m_day.group(1))
    hour, minute = 9, 0
    if clock is not None:
        hour, minute = clock
    elif m_day and m_day.group(1).lower() == "tonight":
        hour, minute = 20, 0

    start_local = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start_local <= local:
        day_name = m_day.group(1).lower() if m_day else ""
        if day_name in _DOW:
            start_local = start_local + timedelta(days=7)
        else:
            start_local = start_local + timedelta(days=1)

    dur = timedelta(hours=1)
    m_dur = _DURATION.search(raw)
    if m_dur:
        n = int(m_dur.group(1))
        unit = m_dur.group(2).lower()
        if unit.startswith("min"):
            dur = timedelta(minutes=n)
        else:
            dur = timedelta(hours=n)

    end_local = start_local + dur
    location = None
    for loc_m in re.finditer(r"(?i)\bat\s+([A-Za-z][\w\s'&./-]{1,40})", body):
        frag = loc_m.group(1).strip()
        if re.match(r"(?i)^\d", frag):
            continue
        low = frag.lower()
        if low in _DOW or low in (
            "today",
            "tomorrow",
            "tonight",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ):
            continue
        if re.match(r"(?i)^\d{1,2}(?::\d{2})?\s*(am|pm)?$", frag):
            continue
        location = frag[:80]
        body = body[: loc_m.start()] + body[loc_m.end() :]
        break

    title = _strip_when_and_meta(body) or "event"
    if location and title.lower().endswith(location.lower()):
        title = title[: -len(location)].strip(" -–")
    title = title[:120] or "event"
    label = start_local.strftime("%a %d %b %H:%M MVT")
    return EventSpec(
        title=title,
        starts_at=to_utc(start_local),
        ends_at=to_utc(end_local),
        location=location,
        local_when=label,
    )


def parse_update(
    text: str, *, now: Optional[datetime] = None
) -> Optional[tuple[str, EventSpec]]:
    raw = (text or "").strip()
    m = _UPDATE_HEAD.match(raw)
    if not m:
        return None
    rest = m.group(1).strip()
    parts = re.split(r"(?i)\s+to\s+", rest, maxsplit=1)
    if len(parts) == 2:
        query, when_bit = parts[0].strip(), parts[1].strip()
        spec = parse_event(f"add event {when_bit} {query}", now=now)
        if spec:
            return query, spec
    spec = parse_event(f"add event {rest}", now=now)
    if not spec:
        return None
    query = _strip_when_and_meta(rest) or spec.title
    return query, spec
