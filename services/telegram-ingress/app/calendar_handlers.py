"""Ingress-local calendar (Life OS slice 4). Create/update=confirm; list/agenda=auto."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from app.side_effect_policy import policy_for
from app import calendar_store as store
from app.calendar_parse import (
    is_agenda_query,
    looks_like_calendar,
    parse_agenda,
    parse_event,
    parse_update,
)
from app.reminder_parse import USER_TZ

logger = logging.getLogger(__name__)

SendFn = Callable[[str, str, str], bool]

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_PENDING_TTL_SEC = int(os.getenv("CALENDAR_PENDING_TTL_SEC", "300"))
_PENDING_KEY = "celia:calendar:pending:{chat_id}"

_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "ok", "okay", "sure", "do it", "go ahead"}
_NO = {"no", "n", "nope", "nah", "cancel", "stop", "don't", "dont"}

_CAL_PENDING: dict[str, dict] = {}


def clear_calendar_for_tests() -> None:
    _CAL_PENDING.clear()


def _redis():
    try:
        from redis import Redis

        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("calendar_pending_redis_unavailable: %s", exc)
        return None


def _key(chat_id: str) -> str:
    return _PENDING_KEY.format(chat_id=chat_id)


def get_pending(chat_id: str) -> Optional[dict]:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_key(chat_id))
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("calendar_pending_get_error: %s", exc)
    data = _CAL_PENDING.get(chat_id)
    if data and data.get("expires_at", 0) < time.time():
        _CAL_PENDING.pop(chat_id, None)
        return None
    return data


def set_pending(chat_id: str, payload: dict) -> None:
    payload = {**payload, "expires_at": time.time() + _PENDING_TTL_SEC}
    _CAL_PENDING[chat_id] = payload
    r = _redis()
    if r is not None:
        try:
            r.setex(_key(chat_id), _PENDING_TTL_SEC, json.dumps(payload, default=str))
        except Exception as exc:
            logger.warning("calendar_pending_set_error: %s", exc)


def clear_pending(chat_id: str) -> None:
    _CAL_PENDING.pop(chat_id, None)
    r = _redis()
    if r is not None:
        try:
            r.delete(_key(chat_id))
        except Exception:
            pass


def _fmt_when(dt) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(USER_TZ).strftime("%a %d %b %H:%M MVT")


def _fmt_line(ev: dict) -> str:
    when = _fmt_when(ev.get("starts_at"))
    loc = f" @ {ev['location']}" if ev.get("location") else ""
    return f"#{ev['id']} {when} — {ev['title']}{loc}"


def _parse_dt(val) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _apply_pending(
    pending: dict,
    chat_id: str,
    telegram_user_id: int,
    thread_id: str,
    send: SendFn,
) -> str:
    db_user_id = store.ensure_user(telegram_user_id)
    if db_user_id is None:
        send(chat_id, "Couldn't reach the calendar.", thread_id)
        return "calendar_store_unavailable"

    action = pending.get("action")
    if action == "create":
        conflicts = store.find_conflicts(
            db_user_id,
            _parse_dt(pending["starts_at"]),
            _parse_dt(pending["ends_at"]),
        )
        ev = store.create_event(
            db_user_id=db_user_id,
            title=pending["title"],
            starts_at=_parse_dt(pending["starts_at"]),
            ends_at=_parse_dt(pending["ends_at"]),
            location=pending.get("location"),
        )
        if not ev:
            send(chat_id, "Couldn't save that event.", thread_id)
            return "calendar_create_failed"
        extra = ""
        if conflicts:
            extra = f" (overlaps #{conflicts[0]['id']} {conflicts[0]['title']})"
        send(
            chat_id,
            f"Booked #{ev['id']} {ev['title']} — {_fmt_when(ev['starts_at'])}{extra}.",
            thread_id,
        )
        return "calendar_created"

    if action == "update":
        event_id = int(pending["event_id"])
        conflicts = store.find_conflicts(
            db_user_id,
            _parse_dt(pending["starts_at"]),
            _parse_dt(pending["ends_at"]),
            exclude_id=event_id,
        )
        ev = store.update_event(
            db_user_id=db_user_id,
            event_id=event_id,
            title=pending.get("title"),
            starts_at=_parse_dt(pending["starts_at"]),
            ends_at=_parse_dt(pending["ends_at"]),
            location=pending.get("location"),
        )
        if not ev:
            send(chat_id, "Couldn't update that.", thread_id)
            return "calendar_update_failed"
        extra = f" (overlaps #{conflicts[0]['id']})" if conflicts else ""
        send(chat_id, f"Updated #{ev['id']} — {_fmt_when(ev['starts_at'])}{extra}.", thread_id)
        return "calendar_updated"

    send(chat_id, "Nothing pending.", thread_id)
    return "calendar_pending_unknown"


def try_handle_calendar(
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

    pending = get_pending(chat_id)
    if pending is not None:
        low = t.lower()
        if low in _YES:
            clear_pending(chat_id)
            return _apply_pending(pending, chat_id, telegram_user_id, thread_id, send)
        if low in _NO:
            clear_pending(chat_id)
            send(chat_id, "Okay, skipped.", thread_id)
            return "calendar_cancelled"
        clear_pending(chat_id)

    if not looks_like_calendar(t):
        return None

    db_user_id = store.ensure_user(telegram_user_id)
    if db_user_id is None:
        send(chat_id, "Couldn't reach the calendar.", thread_id)
        return "calendar_store_unavailable"

    if is_agenda_query(t):
        _ = policy_for("cal.list")
        spec = parse_agenda(t)
        rows = store.list_events(
            db_user_id,
            start=spec.start_local.astimezone(timezone.utc),
            end=spec.end_local.astimezone(timezone.utc),
        )
        if not rows:
            send(chat_id, f"Nothing on {spec.label}.", thread_id)
            return "calendar_agenda_empty"
        lines = [_fmt_line(r) for r in rows]
        body = "\n".join(lines) if len(lines) > 2 else "; ".join(lines)
        prefix = spec.label[:1].upper() + spec.label[1:]
        send(
            chat_id,
            f"{prefix}:\n{body}" if len(lines) > 2 else f"{prefix}: {body}.",
            thread_id,
        )
        return "calendar_agenda"

    upd = parse_update(t)
    if upd is not None:
        query, spec = upd
        cands = store.find_events(db_user_id, query)
        if not cands:
            send(chat_id, "No matching event.", thread_id)
            return "calendar_update_miss"
        target = cands[0]
        conflicts = store.find_conflicts(
            db_user_id, spec.starts_at, spec.ends_at, exclude_id=target["id"]
        )
        if policy_for("cal.update") == "confirm":
            set_pending(
                chat_id,
                {
                    "action": "update",
                    "event_id": target["id"],
                    "title": spec.title or target["title"],
                    "starts_at": spec.starts_at.isoformat(),
                    "ends_at": spec.ends_at.isoformat(),
                    "location": spec.location or target.get("location"),
                },
            )
            conflict_note = ""
            if conflicts:
                conflict_note = f" Overlaps #{conflicts[0]['id']} {conflicts[0]['title']}."
            send(
                chat_id,
                f"Move #{target['id']} {target['title']} → {spec.local_when}?{conflict_note}",
                thread_id,
            )
            return "calendar_update_pending"
        ev = store.update_event(
            db_user_id=db_user_id,
            event_id=target["id"],
            title=spec.title,
            starts_at=spec.starts_at,
            ends_at=spec.ends_at,
            location=spec.location,
        )
        send(chat_id, f"Updated #{ev['id']}." if ev else "Couldn't update.", thread_id)
        return "calendar_updated" if ev else "calendar_update_failed"

    spec = parse_event(t)
    if spec is None:
        send(chat_id, "Say when — e.g. Friday 3pm dentist.", thread_id)
        return "calendar_clarify"

    conflicts = store.find_conflicts(db_user_id, spec.starts_at, spec.ends_at)
    if policy_for("cal.create") == "confirm":
        set_pending(
            chat_id,
            {
                "action": "create",
                "title": spec.title,
                "starts_at": spec.starts_at.isoformat(),
                "ends_at": spec.ends_at.isoformat(),
                "location": spec.location,
            },
        )
        conflict_note = ""
        if conflicts:
            conflict_note = f" Overlaps #{conflicts[0]['id']} {conflicts[0]['title']}."
        loc = f" @ {spec.location}" if spec.location else ""
        send(
            chat_id,
            f"Add {spec.title}{loc} — {spec.local_when}?{conflict_note}",
            thread_id,
        )
        return "calendar_create_pending"

    ev = store.create_event(
        db_user_id=db_user_id,
        title=spec.title,
        starts_at=spec.starts_at,
        ends_at=spec.ends_at,
        location=spec.location,
    )
    send(
        chat_id,
        f"Booked #{ev['id']} {spec.title}." if ev else "Couldn't save that.",
        thread_id,
    )
    return "calendar_created" if ev else "calendar_create_failed"
