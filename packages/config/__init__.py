"""
Centralized configuration loader with profile support (dev/stage/prod),
secret loading, and validators.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    port: int
    host: str = "0.0.0.0"
    log_level: str = "info"


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str

    @property
    def url(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.dbname}"
        )

    @property
    def async_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.dbname}"
        )


@dataclass(frozen=True)
class RedisConfig:
    url: str
    db: int = 0

    @property
    def connection_url(self) -> str:
        base = self.url.rstrip("/")
        return f"{base}/{self.db}"


@dataclass(frozen=True)
class LiteLLMConfig:
    base_url: str
    master_key: str


@dataclass(frozen=True)
class TelegramConfig:
    bot_username: str
    allowed_user_ids: set[int] = field(default_factory=set)
    api_token: str = ""


@dataclass(frozen=True)
class WorkerConfig:
    retry_max: int = 3
    retry_backoff_ms: int = 200
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout_seconds: int = 30
    task_timeout_seconds: int = 120


@dataclass(frozen=True)
class SecurityConfig:
    policy_gateway_url: str
    deny_by_default: bool = True
    max_command_length: int = 4096
    approval_timeout_minutes: int = 30


@dataclass(frozen=True)
class PlatformConfig:
    profile: str
    db: DatabaseConfig
    redis: RedisConfig
    litellm: LiteLLMConfig
    telegram: TelegramConfig
    worker: WorkerConfig
    security: SecurityConfig
    ingress_stream: str = "ingress.accepted"
    dispatch_stream: str = "orchestration.dispatched"
    completion_stream: str = "worker.completed"
    notification_stream: str = "notification.requested"
    dead_letter_stream: str = "dead.letter"

    _instance: ClassVar[PlatformConfig | None] = None

    @classmethod
    def load(cls) -> PlatformConfig:
        if cls._instance is not None:
            return cls._instance

        profile = os.getenv("PROFILE", "dev")
        cls._instance = cls(
            profile=profile,
            db=DatabaseConfig(
                host=os.getenv("POSTGRES_HOST", "postgres"),
                port=int(os.getenv("POSTGRES_PORT", "5432")),
                dbname=os.getenv("POSTGRES_DB", "agent_platform"),
                user=os.getenv("POSTGRES_USER", "agent_user"),
                password=_require("POSTGRES_PASSWORD"),
            ),
            redis=RedisConfig(
                url=os.getenv("REDIS_URL", "redis://redis:6379"),
                db=int(os.getenv("REDIS_DB", "0")),
            ),
            litellm=LiteLLMConfig(
                base_url=os.getenv(
                    "LITELLM_BASE_URL", "http://litellm:4000/v1"
                ),
                master_key=os.getenv("LITELLM_MASTER_KEY", "local-dev-key"),
            ),
            telegram=TelegramConfig(
                bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "agent_bot"),
                allowed_user_ids=_parse_int_set(
                    os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")
                ),
                api_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            ),
            worker=WorkerConfig(
                retry_max=int(os.getenv("WORKER_RETRY_MAX", "3")),
                retry_backoff_ms=int(
                    os.getenv("WORKER_RETRY_BACKOFF_MS", "200")
                ),
                circuit_breaker_threshold=int(
                    os.getenv("WORKER_CB_THRESHOLD", "5")
                ),
                circuit_breaker_timeout_seconds=int(
                    os.getenv("WORKER_CB_TIMEOUT_SECONDS", "30")
                ),
                task_timeout_seconds=int(
                    os.getenv("WORKER_TASK_TIMEOUT_SECONDS", "120")
                ),
            ),
            security=SecurityConfig(
                policy_gateway_url=os.getenv(
                    "POLICY_GATEWAY_URL",
                    "http://policy-gateway:8000/v1/policy/command/evaluate",
                ),
                deny_by_default=_parse_bool(
                    os.getenv("SECURITY_DENY_BY_DEFAULT", "true")
                ),
                max_command_length=int(
                    os.getenv("SECURITY_MAX_COMMAND_LENGTH", "4096")
                ),
                approval_timeout_minutes=int(
                    os.getenv("SECURITY_APPROVAL_TIMEOUT_MINUTES", "30")
                ),
            ),
            ingress_stream=os.getenv(
                "INGRESS_STREAM", "ingress.accepted"
            ),
            dispatch_stream=os.getenv(
                "DISPATCH_STREAM", "orchestration.dispatched"
            ),
            completion_stream=os.getenv(
                "COMPLETION_STREAM", "worker.completed"
            ),
            notification_stream=os.getenv(
                "NOTIFICATION_STREAM", "notification.requested"
            ),
            dead_letter_stream=os.getenv(
                "DEAD_LETTER_STREAM", "dead.letter"
            ),
        )
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None


def _require(key: str) -> str:
    value = os.getenv(key, "")
    if not value:
        raise ConfigError(f"Missing required env var: {key}")
    return value


def _parse_int_set(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.add(int(part))
    return result


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes", "on")
