from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel
from redis import Redis
import psycopg

from packages.telemetry import init_logging, counter

SERVICE_NAME = os.getenv("SERVICE_NAME", "admin-console-api")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
INGRESS_STREAM = os.getenv("INGRESS_STREAM", "ingress.accepted")
DISPATCH_STREAM = os.getenv("DISPATCH_STREAM", "orchestration.dispatched")
COMPLETION_STREAM = os.getenv("COMPLETION_STREAM", "worker.completed")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    service: str
    status: str
    timestamp: str


class QueueDepthResponse(BaseModel):
    ingress_pending: int
    orchestration_pending: int
    worker_pending: int


class RecentRun(BaseModel):
    run_id: str
    status: str
    agent_role: str
    started_at: str


class UsageSummary(BaseModel):
    month: str
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int


class BudgetItem(BaseModel):
    scope_type: str
    scope_id: str
    monthly_limit_usd: float
    spent_usd: float


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        service=SERVICE_NAME,
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE_NAME, "message": "ready"}


@app.get("/metrics/queue-depth", response_model=QueueDepthResponse)
def queue_depth() -> QueueDepthResponse:
    ingress = orchestration = worker = 0
    try:
        r = Redis.from_url(REDIS_URL, decode_responses=True)
        ingress = r.xlen(INGRESS_STREAM)
        orchestration = r.xlen(DISPATCH_STREAM)
        worker = r.xlen(COMPLETION_STREAM)
    except Exception:
        pass

    return QueueDepthResponse(
        ingress_pending=ingress,
        orchestration_pending=orchestration,
        worker_pending=worker,
    )


@app.get("/runs/recent", response_model=list[RecentRun])
def recent_runs() -> list[RecentRun]:
    results: list[RecentRun] = []
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(run_ref, id::text), status, COALESCE(current_agent, ''), started_at
                    FROM orchestration_runs
                    ORDER BY started_at DESC
                    LIMIT 50
                    """
                )
                rows = cur.fetchall()
        for run_ref, status, agent_role, started_at in rows:
            results.append(
                RecentRun(
                    run_id=run_ref,
                    status=status,
                    agent_role=agent_role or "",
                    started_at=started_at.isoformat() if started_at else "",
                )
            )
    except Exception:
        pass

    return results


@app.get("/usage/summary", response_model=UsageSummary)
def usage_summary() -> UsageSummary:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    total_cost = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(cost_usd), 0),
                        COALESCE(SUM(prompt_tokens), 0),
                        COALESCE(SUM(completion_tokens), 0)
                    FROM usage_events
                    WHERE TO_CHAR(created_at, 'YYYY-MM') = %s
                    """,
                    (month,),
                )
                row = cur.fetchone()
                if row:
                    total_cost = float(row[0] or 0)
                    prompt_tokens = int(row[1] or 0)
                    completion_tokens = int(row[2] or 0)
    except Exception:
        pass

    return UsageSummary(
        month=month,
        total_cost_usd=total_cost,
        total_prompt_tokens=prompt_tokens,
        total_completion_tokens=completion_tokens,
    )


@app.get("/budgets", response_model=list[BudgetItem])
def budgets() -> list[BudgetItem]:
    items: list[BudgetItem] = []
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT scope_type, scope_id, monthly_limit_usd, spent_usd
                    FROM budgets
                    ORDER BY id DESC
                    LIMIT 200
                    """
                )
                rows = cur.fetchall()
        for scope_type, scope_id, monthly_limit_usd, spent_usd in rows:
            items.append(
                BudgetItem(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    monthly_limit_usd=float(monthly_limit_usd),
                    spent_usd=float(spent_usd),
                )
            )
    except Exception:
        pass

    return items
