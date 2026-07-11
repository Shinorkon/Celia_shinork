"""
Hardening utilities: dead-letter queues, idempotency keys, retry/backoff.
All services import from here for consistent resilience patterns.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dead-letter queue publisher
# ---------------------------------------------------------------------------


class DeadLetter:
    """Publish failed events to a dead-letter stream for later replay."""

    def __init__(self, redis_client: Any, stream_name: str = "dead.letter") -> None:
        self._redis = redis_client
        self._stream = stream_name

    def publish(
        self,
        original_payload: dict[str, Any],
        error: str,
        source: str,
        correlation_id: str = "",
        retry_count: int = 0,
    ) -> str:
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "error": error,
            "correlation_id": correlation_id,
            "retry_count": str(retry_count),
            "original_payload": json.dumps(original_payload, default=str),
        }
        msg_id = self._redis.xadd(self._stream, {"payload": json.dumps(event)})
        logger.warning(
            "dead_letter_published",
            extra={
                "extra_fields": {
                    "source": source,
                    "error": error,
                    "correlation_id": correlation_id,
                    "dlq_id": msg_id,
                }
            },
        )
        return msg_id


# ---------------------------------------------------------------------------
# Idempotency key store
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """Redis-backed idempotency using set-if-not-exists with TTL."""

    def __init__(
        self,
        redis_client: Any,
        prefix: str = "idem",
        ttl_seconds: int = 3600,
    ) -> None:
        self._redis = redis_client
        self._prefix = prefix
        self._ttl = ttl_seconds

    def is_duplicate(self, key: str) -> bool:
        full_key = f"{self._prefix}:{key}"
        # SETNX returns 1 if key was set, 0 if already exists
        was_set = self._redis.set(full_key, "1", nx=True, ex=self._ttl)
        return not was_set

    def mark_processed(self, key: str) -> None:
        full_key = f"{self._prefix}:{key}"
        self._redis.set(full_key, "1", ex=self._ttl)

    def clear(self, key: str) -> None:
        full_key = f"{self._prefix}:{key}"
        self._redis.delete(full_key)


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------


def retry_with_backoff(
    max_retries: int = 3,
    base_ms: int = 200,
    max_ms: int = 10_000,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay_ms = min(base_ms * (2**attempt), max_ms)
                    logger.warning(
                        f"retry_attempt_{attempt + 1}",
                        extra={
                            "extra_fields": {
                                "function": func.__name__,
                                "error": str(exc),
                                "delay_ms": delay_ms,
                            }
                        },
                    )
                    time.sleep(delay_ms / 1000)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Circuit breaker (simple counting breaker)
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Opens after consecutive failures, auto-resets after timeout."""

    def __init__(
        self,
        name: str,
        threshold: int = 5,
        timeout_seconds: int = 30,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.timeout_seconds = timeout_seconds
        self._failures = 0
        self._last_failure_time: float = 0.0
        self._open = False

    @property
    def is_open(self) -> bool:
        if not self._open:
            return False
        if time.monotonic() - self._last_failure_time >= self.timeout_seconds:
            self._open = False
            self._failures = 0
            logger.info(f"circuit_half_open", extra={"extra_fields": {"breaker": self.name}})
            return False
        return True

    def success(self) -> None:
        self._failures = 0
        self._open = False

    def failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self.threshold:
            self._open = True
            logger.error(
                f"circuit_open",
                extra={
                    "extra_fields": {
                        "breaker": self.name,
                        "failures": self._failures,
                    }
                },
            )
