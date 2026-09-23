"""Minimal durable list artifacts for Phase A (Redis).

Keys:
  celia:list:active:{chat_id} -> list_id
  celia:list:{list_id}        -> JSON {list_id, chat_id, title, items, updated_at}
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

# In-process fallback when Redis is down (tests / brief outages)
_MEM_LISTS: dict[str, dict[str, Any]] = {}
_MEM_ACTIVE: dict[str, str] = {}


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


def create_list(chat_id: str, title: str = "List", items: Optional[list[dict]] = None) -> dict:
    list_id = uuid.uuid4().hex[:12]
    doc = {
        "list_id": list_id,
        "chat_id": str(chat_id),
        "title": (title or "List").strip() or "List",
        "items": list(items or []),
        "updated_at": time.time(),
    }
    r = _redis()
    if r is not None:
        try:
            r.set(_list_key(list_id), json.dumps(doc), ex=_LIST_TTL_SEC)
            r.set(_active_key(chat_id), list_id, ex=_ACTIVE_TTL_SEC)
            return doc
        except Exception as exc:
            logger.error("list_store_create_redis_error: %s", exc)
    _MEM_LISTS[list_id] = doc
    _MEM_ACTIVE[str(chat_id)] = list_id
    return doc


def get_list(list_id: str) -> Optional[dict]:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_list_key(list_id))
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.error("list_store_get_redis_error: %s", exc)
    return _MEM_LISTS.get(list_id)


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


def append_items(list_id: str, new_items: list[dict]) -> Optional[dict]:
    doc = get_list(list_id)
    if doc is None:
        return None
    items = list(doc.get("items") or [])
    for it in new_items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        qty = int(it.get("qty") or 1)
        merged = False
        for existing in items:
            if (existing.get("name") or "").strip().lower() == name.lower():
                existing["qty"] = int(existing.get("qty") or 1) + qty
                merged = True
                break
        if not merged:
            items.append({"name": name, "qty": qty})
    doc["items"] = items
    doc["updated_at"] = time.time()
    r = _redis()
    if r is not None:
        try:
            r.set(_list_key(list_id), json.dumps(doc), ex=_LIST_TTL_SEC)
            chat_id = doc.get("chat_id")
            if chat_id:
                r.set(_active_key(str(chat_id)), list_id, ex=_ACTIVE_TTL_SEC)
            return doc
        except Exception as exc:
            logger.error("list_store_append_redis_error: %s", exc)
    _MEM_LISTS[list_id] = doc
    if doc.get("chat_id"):
        _MEM_ACTIVE[str(doc["chat_id"])] = list_id
    return doc


def clear_memory_for_tests() -> None:
    _MEM_LISTS.clear()
    _MEM_ACTIVE.clear()
