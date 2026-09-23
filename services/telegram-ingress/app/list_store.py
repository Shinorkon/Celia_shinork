"""Durable list artifacts (Phase A + B) via Redis with in-process fallback.

Keys:
  celia:list:active:{chat_id}    -> list_id
  celia:list:{list_id}           -> JSON doc
  celia:list:session:{chat_id}   -> JSON {list_id, started_at}  (collecting)

Doc shape:
  {
    list_id, chat_id, title,
    items: [{id, name, qty, done}],
    updated_at, collecting: bool
  }
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
_ACTIVE_TTL_SEC = 7 * 24 * 3600
_LIST_TTL_SEC = 30 * 24 * 3600
# Open-goal collecting window after "make a list" (Phase B).
_SESSION_TTL_SEC = int(os.getenv("LIST_SESSION_TTL_SEC", str(15 * 60)))

_MEM_LISTS: dict[str, dict[str, Any]] = {}
_MEM_ACTIVE: dict[str, str] = {}
_MEM_SESSION: dict[str, dict[str, Any]] = {}


def _redis():
    try:
        from redis import Redis

        return Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("list_store_redis_unavailable: %s", exc)
        return None


def _active_key(chat_id: str) -> str:
    return f"celia:list:active:{chat_id}"


def _list_key(list_id: str) -> str:
    return f"celia:list:{list_id}"


def _session_key(chat_id: str) -> str:
    return f"celia:list:session:{chat_id}"


def _new_item(name: str, qty: int = 1, *, done: bool = False) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name.strip(),
        "qty": max(1, int(qty or 1)),
        "done": bool(done),
    }


def _normalize_items(items: Optional[list]) -> list[dict]:
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        iid = (it.get("id") or "").strip() or uuid.uuid4().hex[:8]
        out.append(
            {
                "id": iid,
                "name": name,
                "qty": max(1, int(it.get("qty") or 1)),
                "done": bool(it.get("done")),
            }
        )
    return out


def _save_doc(doc: dict) -> dict:
    doc = dict(doc)
    doc["updated_at"] = time.time()
    doc["items"] = _normalize_items(doc.get("items"))
    list_id = doc["list_id"]
    chat_id = str(doc.get("chat_id") or "")
    r = _redis()
    if r is not None:
        try:
            r.set(_list_key(list_id), json.dumps(doc), ex=_LIST_TTL_SEC)
            if chat_id:
                r.set(_active_key(chat_id), list_id, ex=_ACTIVE_TTL_SEC)
            return doc
        except Exception as exc:
            logger.error("list_store_save_redis_error: %s", exc)
    _MEM_LISTS[list_id] = doc
    if chat_id:
        _MEM_ACTIVE[chat_id] = list_id
    return doc


def create_list(
    chat_id: str,
    title: str = "List",
    items: Optional[list[dict]] = None,
    *,
    collecting: bool = True,
) -> dict:
    list_id = uuid.uuid4().hex[:12]
    raw_items = []
    for it in items or []:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        raw_items.append(_new_item(name, int(it.get("qty") or 1), done=bool(it.get("done"))))
    doc = {
        "list_id": list_id,
        "chat_id": str(chat_id),
        "title": (title or "List").strip() or "List",
        "items": raw_items,
        "updated_at": time.time(),
        "collecting": bool(collecting),
    }
    saved = _save_doc(doc)
    if collecting:
        start_session(chat_id, list_id)
    return saved


def get_list(list_id: str) -> Optional[dict]:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_list_key(list_id))
            if raw:
                doc = json.loads(raw)
                doc["items"] = _normalize_items(doc.get("items"))
                return doc
        except Exception as exc:
            logger.error("list_store_get_redis_error: %s", exc)
    doc = _MEM_LISTS.get(list_id)
    if doc:
        doc = dict(doc)
        doc["items"] = _normalize_items(doc.get("items"))
    return doc


def get_active_list_id(chat_id: str) -> Optional[str]:
    r = _redis()
    if r is not None:
        try:
            lid = r.get(_active_key(chat_id))
            if lid:
                return lid
        except Exception as exc:
            logger.error("list_store_active_redis_error: %s", exc)
    return _MEM_ACTIVE.get(str(chat_id))


def get_active_list(chat_id: str) -> Optional[dict]:
    lid = get_active_list_id(chat_id)
    if not lid:
        return None
    return get_list(lid)


def has_active_list(chat_id: str) -> bool:
    return get_active_list_id(chat_id) is not None


def set_active_list(chat_id: str, list_id: str) -> None:
    r = _redis()
    if r is not None:
        try:
            r.set(_active_key(chat_id), list_id, ex=_ACTIVE_TTL_SEC)
            return
        except Exception as exc:
            logger.error("list_store_set_active_error: %s", exc)
    _MEM_ACTIVE[str(chat_id)] = list_id


def start_session(chat_id: str, list_id: str) -> None:
    payload = {"list_id": list_id, "started_at": time.time()}
    r = _redis()
    if r is not None:
        try:
            r.set(_session_key(chat_id), json.dumps(payload), ex=_SESSION_TTL_SEC)
            doc = get_list(list_id)
            if doc is not None:
                doc["collecting"] = True
                _save_doc(doc)
            return
        except Exception as exc:
            logger.error("list_store_session_start_error: %s", exc)
    _MEM_SESSION[str(chat_id)] = payload
    doc = get_list(list_id)
    if doc is not None:
        doc["collecting"] = True
        _save_doc(doc)


def end_session(chat_id: str) -> None:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_session_key(chat_id))
            if raw:
                try:
                    sid = json.loads(raw).get("list_id")
                    if sid:
                        doc = get_list(sid)
                        if doc is not None:
                            doc["collecting"] = False
                            _save_doc(doc)
                except Exception:
                    pass
            r.delete(_session_key(chat_id))
        except Exception as exc:
            logger.error("list_store_session_end_error: %s", exc)
    sess = _MEM_SESSION.pop(str(chat_id), None)
    if sess and sess.get("list_id"):
        doc = get_list(sess["list_id"])
        if doc is not None:
            doc["collecting"] = False
            _save_doc(doc)


def get_session(chat_id: str) -> Optional[dict]:
    """Return active collecting session if present and not expired."""
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_session_key(chat_id))
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.error("list_store_session_get_error: %s", exc)
    sess = _MEM_SESSION.get(str(chat_id))
    if not sess:
        return None
    if time.time() - float(sess.get("started_at") or 0) > _SESSION_TTL_SEC:
        _MEM_SESSION.pop(str(chat_id), None)
        return None
    return sess


def is_collecting(chat_id: str) -> bool:
    return get_session(chat_id) is not None


def touch_session(chat_id: str) -> None:
    """Refresh collecting TTL while user keeps adding items."""
    sess = get_session(chat_id)
    if not sess:
        return
    start_session(chat_id, sess["list_id"])


def open_item_count(doc: dict) -> int:
    return sum(1 for i in (doc.get("items") or []) if not i.get("done"))


def total_item_count(doc: dict) -> int:
    return len(doc.get("items") or [])


def append_items(list_id: str, new_items: list[dict]) -> Optional[dict]:
    doc = get_list(list_id)
    if doc is None:
        return None
    items = list(doc.get("items") or [])
    for it in new_items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        qty = max(1, int(it.get("qty") or 1))
        merged = False
        for existing in items:
            if existing.get("done"):
                continue
            if (existing.get("name") or "").strip().lower() == name.lower():
                existing["qty"] = int(existing.get("qty") or 1) + qty
                merged = True
                break
        if not merged:
            items.append(_new_item(name, qty))
    doc["items"] = items
    saved = _save_doc(doc)
    chat_id = saved.get("chat_id")
    if chat_id and is_collecting(str(chat_id)):
        touch_session(str(chat_id))
    return saved


def set_title(list_id: str, title: str) -> Optional[dict]:
    doc = get_list(list_id)
    if doc is None:
        return None
    doc["title"] = (title or "List").strip() or "List"
    return _save_doc(doc)


def _match_item(items: list[dict], query: str) -> Optional[dict]:
    q = (query or "").strip().lower()
    if not q:
        return None
    # Exact id
    for it in items:
        if it.get("id") == query.strip():
            return it
    # Exact name
    for it in items:
        if (it.get("name") or "").strip().lower() == q:
            return it
    # Substring (prefer open items)
    open_hits = [
        it
        for it in items
        if not it.get("done") and q in (it.get("name") or "").lower()
    ]
    if len(open_hits) == 1:
        return open_hits[0]
    if len(open_hits) > 1:
        # Prefer shortest name match
        open_hits.sort(key=lambda x: len(x.get("name") or ""))
        return open_hits[0]
    hits = [it for it in items if q in (it.get("name") or "").lower()]
    if hits:
        hits.sort(key=lambda x: len(x.get("name") or ""))
        return hits[0]
    return None


def mark_items(
    list_id: str,
    query: str,
    *,
    done: bool = True,
) -> tuple[Optional[dict], Optional[dict]]:
    """Mark matching item done/undone. Returns (doc, matched_item)."""
    doc = get_list(list_id)
    if doc is None:
        return None, None
    items = list(doc.get("items") or [])
    matched = _match_item(items, query)
    if matched is None:
        return doc, None
    matched["done"] = bool(done)
    doc["items"] = items
    return _save_doc(doc), matched


def remove_item(list_id: str, query: str) -> tuple[Optional[dict], Optional[dict]]:
    doc = get_list(list_id)
    if doc is None:
        return None, None
    items = list(doc.get("items") or [])
    matched = _match_item(items, query)
    if matched is None:
        return doc, None
    items = [it for it in items if it.get("id") != matched.get("id")]
    doc["items"] = items
    return _save_doc(doc), matched


def clear_memory_for_tests() -> None:
    _MEM_LISTS.clear()
    _MEM_ACTIVE.clear()
    _MEM_SESSION.clear()
