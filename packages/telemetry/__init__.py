"""
Structured telemetry: JSON logging with correlation IDs, OpenTelemetry-
compatible trace context, and Prometheus-leaning metrics stubs.

All side-effects (log emission, span recording) go through this module
so unit tests can swap in no-op collectors.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Correlation context (thread-local, async-safe)
# ---------------------------------------------------------------------------

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
_run_id: ContextVar[str] = ContextVar("run_id", default="")
_service_name: ContextVar[str] = ContextVar("telemetry.service", default="")


def set_correlation_id(cid: str | None = None) -> str:
    value = cid or str(uuid.uuid4())
    _correlation_id.set(value)
    return value


def get_correlation_id() -> str:
    return _correlation_id.get() or ""


def set_run_id(rid: str) -> None:
    _run_id.set(rid)


def get_run_id() -> str:
    return _run_id.get() or ""


def set_service_name(name: str) -> None:
    _service_name.set(name)


def get_service_name() -> str:
    return _service_name.get() or "unknown"


# ---------------------------------------------------------------------------
# Structured JSON logger
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": get_service_name(),
            "correlation_id": get_correlation_id(),
            "run_id": get_run_id(),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info and record.exc_info[1]:
            payload["error"] = str(record.exc_info[1])

        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)

        return json.dumps(payload, default=str)


class _MetricsBuffer:
    """In-memory metrics aggregator. Replace with Prometheus in prod."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        self.histograms: dict[str, list[float]] = defaultdict(list)

    def incr(self, name: str, value: int = 1) -> None:
        self.counters[f"{get_service_name()}:{name}"] += value

    def observe(self, name: str, value: float) -> None:
        self.histograms[f"{get_service_name()}:{name}"].append(value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "histograms": {
                k: {
                    "count": len(v),
                    "avg": sum(v) / len(v) if v else 0,
                    "min": min(v) if v else 0,
                    "max": max(v) if v else 0,
                }
                for k, v in self.histograms.items()
            },
        }

    def reset(self) -> None:
        self.counters.clear()
        self.histograms.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_logging(service_name: str, level: str = "info") -> None:
    set_service_name(service_name)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Silence noisy libraries
    for lib in ("urllib3", "httpx", "apscheduler", "redis"):
        logging.getLogger(lib).setLevel(logging.WARNING)


_metrics = _MetricsBuffer()


def counter(name: str, value: int = 1) -> None:
    _metrics.incr(name, value)


def histogram(name: str, value: float) -> None:
    _metrics.observe(name, value)


def metrics_snapshot() -> dict[str, Any]:
    return _metrics.snapshot()


def metrics_reset() -> None:
    _metrics.reset()


@dataclass
class Span:
    name: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    attrs: dict[str, str] = field(default_factory=dict)
    _start_ns: int = field(default_factory=time.monotonic_ns)

    def end(self) -> dict[str, Any]:
        elapsed_ms = (time.monotonic_ns() - self._start_ns) / 1_000_000
        histogram(f"span.{self.name}.duration_ms", elapsed_ms)
        payload = {
            "span": self.name,
            "correlation_id": self.correlation_id,
            "duration_ms": round(elapsed_ms, 3),
            "attrs": self.attrs,
        }
        logging.getLogger("telemetry.span").info(
            "span closed", extra={"extra_fields": payload}
        )
        return payload


_span_factory: Callable[[str], Span] = Span


def start_span(name: str) -> Span:
    return _span_factory(name)


def override_span_factory(factory: Callable[[str], Span]) -> None:
    global _span_factory
    _span_factory = factory
