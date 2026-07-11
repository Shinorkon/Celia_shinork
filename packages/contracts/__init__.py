"""
Shared event contracts and typed DTOs for the agent orchestration platform.
All inter-service communication must use these envelopes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


AgentRole = Literal[
    "frontoffice",
    "planner",
    "executor",
    "scheduler",
    "comms",
    "document",
    "ops-monitor",
    "qa",
]


class TriggerType(str, Enum):
    COMMAND = "command"
    MENTION = "mention"
    CLASSIFIER = "classifier"
    NONE = "none"


class RunStatus(str, Enum):
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"


class JobType(str, Enum):
    ONCE = "once"
    CRON = "cron"
    INTERVAL = "interval"


# ---------------------------------------------------------------------------
# Core event envelopes
# ---------------------------------------------------------------------------


class IngressMessageAccepted(BaseModel):
    """Emitted when telegram-ingress accepts a message."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    user_id: str
    role: str | None = None
    chat_id: str
    thread_id: str | None = None
    text: str
    trigger_type: TriggerType
    reason: str
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))


class OrchestrationTaskDispatched(BaseModel):
    """Emitted when orchestrator routes a task to a worker."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    run_id: str
    thread_id: str
    agent_role: AgentRole
    task_type: str
    input_ref: str | None = None
    text: str = ""
    user_id: str = ""
    chat_id: str = ""
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))


class WorkerTaskCompleted(BaseModel):
    """Emitted when a worker finishes processing."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    run_id: str
    agent_role: AgentRole
    status: str
    output: str = ""
    artifacts: list[str] = Field(default_factory=list)
    needs_approval: bool = False
    policy_reason: str | None = None
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))


class NotificationSendRequested(BaseModel):
    """Emitted when scheduler or worker requests a notification."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    target_user_id: str
    chat_id: str
    thread_id: str | None = None
    text: str
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))


# ---------------------------------------------------------------------------
# Stream constants
# ---------------------------------------------------------------------------


class StreamNames:
    INGRESS_ACCEPTED = "ingress.accepted"
    ORCHESTRATION_DISPATCHED = "orchestration.dispatched"
    WORKER_COMPLETED = "worker.completed"
    NOTIFICATION_REQUESTED = "notification.requested"
    DEAD_LETTER = "dead.letter"


class ConsumerGroups:
    ORCHESTRATOR = "orchestrator-group"
    WORKER = "worker-group"
    NOTIFICATION = "notification-group"


# ---------------------------------------------------------------------------
# API request/response DTOs
# ---------------------------------------------------------------------------


class PolicyEvalRequest(BaseModel):
    command: str = Field(min_length=1)
    context: dict[str, str] | None = None


class PolicyEvalResponse(BaseModel):
    allowed: bool
    reason_code: str
    normalized_command: str


class CreateJobRequest(BaseModel):
    job_type: JobType
    text: str = Field(min_length=1)
    run_at: datetime | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    target_user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    next_run: str | None = None
