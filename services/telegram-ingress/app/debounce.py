"""Per-chat debounce buffer for Telegram ingress (Phase A).

Collapses rapid consecutive messages and media-group parts into one turn
before finance/list routing or orchestrator publish.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Quiet window before flush (ms). Spec: ~800–1500ms.
DEFAULT_DEBOUNCE_MS = 1100
# Album / media_group parts often arrive after per-photo downloads; give them
# a slightly longer quiet window so N photos become one turn.
MEDIA_GROUP_DEBOUNCE_MS = 2200


@dataclass
class BufferedUpdate:
    user_id: int
    chat_id: str
    thread_id: str
    chat_type: str
    text: str
    image_data_url: str = ""
    media_group_id: Optional[str] = None
    message_id: Optional[int] = None
    received_at: float = field(default_factory=time.time)


FlushCallback = Callable[[str, list[BufferedUpdate]], None]


class ChatDebouncer:
    """Thread-safe per-chat_id debounce.

    Each add() resets the quiet timer. When the timer fires, all buffered
    updates for that chat are handed to on_flush as one turn.
    """

    def __init__(
        self,
        on_flush: FlushCallback,
        *,
        delay_ms: int = DEFAULT_DEBOUNCE_MS,
        media_group_delay_ms: int = MEDIA_GROUP_DEBOUNCE_MS,
    ) -> None:
        self._on_flush = on_flush
        self._delay_s = max(0.05, float(delay_ms) / 1000.0)
        self._media_group_delay_s = max(
            self._delay_s, float(media_group_delay_ms) / 1000.0
        )
        self._lock = threading.Lock()
        self._buffers: dict[str, list[BufferedUpdate]] = {}
        self._timers: dict[str, threading.Timer] = {}

    @property
    def delay_ms(self) -> int:
        return int(self._delay_s * 1000)

    def _delay_for(self, buf: list[BufferedUpdate], update: BufferedUpdate) -> float:
        """Longer quiet window for media groups / multi-photo bursts."""
        if update.media_group_id or any(u.media_group_id for u in buf):
            return self._media_group_delay_s
        # Several images already buffered (rapid one-by-one sends) — hold longer.
        n_img = sum(1 for u in buf if (u.image_data_url or "").strip())
        if n_img >= 1 and (update.image_data_url or "").strip():
            return self._media_group_delay_s
        return self._delay_s

    def add(self, update: BufferedUpdate) -> None:
        chat_id = str(update.chat_id)
        with self._lock:
            buf = self._buffers.setdefault(chat_id, [])
            buf.append(update)
            old = self._timers.pop(chat_id, None)
            if old is not None:
                old.cancel()
            delay_s = self._delay_for(buf, update)
            timer = threading.Timer(delay_s, self._flush_safe, args=(chat_id,))
            timer.daemon = True
            self._timers[chat_id] = timer
            timer.start()
            logger.debug(
                "debounce_buffered chat_id=%s n=%s delay_ms=%s media_group=%s",
                chat_id,
                len(buf),
                int(delay_s * 1000),
                update.media_group_id,
            )

    def _flush_safe(self, chat_id: str) -> None:
        try:
            self.flush_now(chat_id)
        except Exception as exc:
            logger.exception("debounce_flush_error chat_id=%s: %s", chat_id, exc)

    def flush_now(self, chat_id: str) -> list[BufferedUpdate]:
        """Cancel timer and flush buffer for chat_id. Returns items flushed."""
        chat_id = str(chat_id)
        with self._lock:
            timer = self._timers.pop(chat_id, None)
            if timer is not None:
                timer.cancel()
            items = self._buffers.pop(chat_id, [])
        if items:
            logger.info(
                "debounce_flush chat_id=%s n=%s texts=%s images=%s",
                chat_id,
                len(items),
                [(u.text or "")[:40] for u in items],
                sum(1 for u in items if (u.image_data_url or "").strip()),
            )
            self._on_flush(chat_id, items)
        return items

    def pending_count(self, chat_id: str) -> int:
        with self._lock:
            return len(self._buffers.get(str(chat_id), []))

    def cancel_all(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
            self._buffers.clear()


def merge_buffered_texts(updates: list[BufferedUpdate]) -> str:
    """Join non-empty texts/captions from a debounce turn (preserve order)."""
    parts: list[str] = []
    for u in updates:
        t = (u.text or "").strip()
        if t and t not in parts:
            parts.append(t)
    return "\n".join(parts)


def merge_buffered_images(updates: list[BufferedUpdate]) -> str:
    """Prefer the last non-empty image data URL in the turn (legacy single-image)."""
    last = ""
    for u in updates:
        if (u.image_data_url or "").strip():
            last = u.image_data_url.strip()
    return last


def collect_buffered_images(updates: list[BufferedUpdate]) -> list[str]:
    """All non-empty image data URLs in turn order (media groups / multi-photo)."""
    out: list[str] = []
    for u in updates:
        url = (u.image_data_url or "").strip()
        if url:
            out.append(url)
    return out


def coalesce_media_group_meta(updates: list[BufferedUpdate]) -> Optional[str]:
    for u in updates:
        if u.media_group_id:
            return u.media_group_id
    return None
