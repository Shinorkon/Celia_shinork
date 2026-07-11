from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
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


class UserItem(BaseModel):
    id: int
    telegram_user_id: int
    role: str
    is_active: bool
    created_at: str


class UserCreate(BaseModel):
    telegram_user_id: int


class UserUpdate(BaseModel):
    is_active: bool


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


# ---------------------------------------------------------------------------
# User management — control who can use the bot
# ---------------------------------------------------------------------------


@app.get("/users", response_model=list[UserItem])
def list_users() -> list[UserItem]:
    items: list[UserItem] = []
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, telegram_user_id, role, is_active,
                           COALESCE(TO_CHAR(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '')
                    FROM users
                    ORDER BY created_at DESC
                    LIMIT 500
                    """
                )
                rows = cur.fetchall()
        for uid, tid, role, active, created in rows:
            items.append(
                UserItem(
                    id=uid,
                    telegram_user_id=tid,
                    role=role or "visitor",
                    is_active=bool(active),
                    created_at=created,
                )
            )
    except Exception:
        pass
    return items


@app.post("/users", response_model=UserItem)
def authorize_user(body: UserCreate) -> UserItem:
    """Authorize a Telegram user (upsert to role='authorized', is_active=TRUE)."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users(telegram_user_id, role, is_active)
                VALUES (%s, 'authorized', TRUE)
                ON CONFLICT (telegram_user_id) DO UPDATE
                SET role = 'authorized', is_active = TRUE
                RETURNING id, telegram_user_id, role, is_active,
                          COALESCE(TO_CHAR(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '')
                """,
                (body.telegram_user_id,),
            )
            row = cur.fetchone()
            conn.commit()
    uid, tid, role, active, created = row
    return UserItem(
        id=uid, telegram_user_id=tid, role=role,
        is_active=bool(active), created_at=created,
    )


@app.put("/users/{user_id}", response_model=UserItem)
def update_user(user_id: int, body: UserUpdate) -> UserItem:
    """Toggle a user's is_active status."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET is_active = %s
                WHERE id = %s
                RETURNING id, telegram_user_id, role, is_active,
                          COALESCE(TO_CHAR(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '')
                """,
                (body.is_active, user_id),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=404, detail="User not found")
    uid, tid, role, active, created = row
    return UserItem(
        id=uid, telegram_user_id=tid, role=role,
        is_active=bool(active), created_at=created,
    )


@app.delete("/users/{user_id}")
def remove_user(user_id: int) -> dict[str, str]:
    """Soft-delete a user (set is_active=FALSE, role='visitor')."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_active = FALSE, role = 'visitor' WHERE id = %s",
                (user_id,),
            )
            conn.commit()
    return {"status": "ok"}
