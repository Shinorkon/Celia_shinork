from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from redis import Redis
import psycopg

from packages.telemetry import init_logging, set_correlation_id, get_correlation_id, counter

SERVICE_NAME = os.getenv("SERVICE_NAME", "scheduler")
init_logging(SERVICE_NAME)
logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
NOTIFICATION_STREAM = os.getenv("NOTIFICATION_STREAM", "notification.requested")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

# SQLAlchemyJobStore needs the psycopg (v3) driver prefix
_SCHEDULER_DB_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# ---------------------------------------------------------------------------
# APScheduler with PostgreSQL-backed SQLAlchemyJobStore for durability
# ---------------------------------------------------------------------------

jobstores = {
    "default": SQLAlchemyJobStore(
        url=_SCHEDULER_DB_URL,
        tablename="apscheduler_jobs",
        engine_options={"pool_size": 5, "max_overflow": 10},
    )
}

scheduler = BackgroundScheduler(timezone="UTC", jobstores=jobstores)

app = FastAPI(title=SERVICE_NAME)


class HealthResponse(BaseModel):
    service: str
    status: str
    timestamp: str


class CreateJobRequest(BaseModel):
    job_type: Literal["once", "cron"]
    text: str = Field(min_length=1)
    run_at: datetime | None = None
    cron_expr: str | None = None
    target_user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    next_run: str | None = None


def _redis() -> Redis:
    return Redis.from_url(REDIS_URL, decode_responses=True)


def _publish_notification(payload: dict[str, str]) -> None:
    r = _redis()
    cid = get_correlation_id() or str(uuid.uuid4())
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": cid,
        **payload,
    }
    r.xadd(NOTIFICATION_STREAM, {"payload": json.dumps(event)})
    counter("scheduler.notification_published")


def _job_callback(
    scheduler_job_id: str,
    text: str,
    target_user_id: str,
    chat_id: str,
    thread_id: str,
) -> None:
    _publish_notification(
        {
            "target_user_id": target_user_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "text": text,
            "priority": "normal",
        }
    )
    try:
        with _db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = CASE WHEN job_type = 'once' THEN 'completed' ELSE status END
                    WHERE payload_jsonb ->> 'scheduler_job_id' = %s
                    """,
                    (scheduler_job_id,),
                )
            conn.commit()
    except Exception:
        pass


def _db_connect():
    return psycopg.connect(DATABASE_URL)


def _insert_job_record(
    job_id: str,
    payload: CreateJobRequest,
    target_user_id: str,
    chat_id: str,
    thread_id: str,
) -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduled_jobs(owner_user_id, job_type, cron_expr, run_at, payload_jsonb, status)
                VALUES (
                    (SELECT id FROM users WHERE telegram_user_id = %s LIMIT 1),
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    'active'
                )
                """,
                (
                    int(target_user_id) if target_user_id and target_user_id.isdigit() else None,
                    payload.job_type,
                    payload.cron_expr,
                    payload.run_at,
                    json.dumps(
                        {
                            "scheduler_job_id": job_id,
                            "text": payload.text,
                            "target_user_id": target_user_id,
                            "chat_id": chat_id,
                            "thread_id": thread_id,
                        }
                    ),
                ),
            )
        conn.commit()


def _set_job_status(job_id: str, status: str) -> None:
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scheduled_jobs
                SET status = %s
                WHERE payload_jsonb ->> 'scheduler_job_id' = %s
                """,
                (status, job_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Lifecycle – APScheduler with SQLAlchemyJobStore auto-loads jobs on start
# ---------------------------------------------------------------------------


@app.on_event("startup")
def startup() -> None:
    if not scheduler.running:
        scheduler.start()


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


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


@app.post("/jobs", response_model=JobResponse)
def create_job(payload: CreateJobRequest) -> JobResponse:
    job_id = str(uuid.uuid4())
    target_user_id = payload.target_user_id or ""
    chat_id = payload.chat_id or ""
    thread_id = payload.thread_id or ""

    if payload.job_type == "once":
        if payload.run_at is None:
            raise HTTPException(status_code=400, detail="run_at is required for once jobs")
        trigger = DateTrigger(run_date=payload.run_at)
    else:
        if not payload.cron_expr:
            raise HTTPException(status_code=400, detail="cron_expr is required for cron jobs")
        parts = payload.cron_expr.split()
        if len(parts) != 5:
            raise HTTPException(status_code=400, detail="cron_expr must have 5 fields")
        minute, hour, day, month, dow = parts
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone="UTC",
        )

    job = scheduler.add_job(
        _job_callback,
        trigger=trigger,
        id=job_id,
        kwargs={
            "scheduler_job_id": job_id,
            "text": payload.text,
            "target_user_id": target_user_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
        },
        replace_existing=False,
    )
    _insert_job_record(job_id, payload, target_user_id, chat_id, thread_id)
    next_run = job.next_run_time.isoformat() if job.next_run_time else None
    return JobResponse(job_id=job_id, status="active", next_run=next_run)


@app.get("/jobs", response_model=list[JobResponse])
def list_jobs() -> list[JobResponse]:
    results: list[JobResponse] = []
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_jsonb ->> 'scheduler_job_id' AS scheduler_job_id, status
                FROM scheduled_jobs
                ORDER BY id DESC
                LIMIT 200
                """
            )
            rows = cur.fetchall()

    for scheduler_job_id, status in rows:
        job = scheduler.get_job(scheduler_job_id)
        results.append(
            JobResponse(
                job_id=scheduler_job_id,
                status=status,
                next_run=job.next_run_time.isoformat() if job and job.next_run_time else None,
            )
        )
    return results


@app.post("/jobs/{job_id}/pause", response_model=JobResponse)
def pause_job(job_id: str) -> JobResponse:
    scheduler.pause_job(job_id)
    _set_job_status(job_id, "paused")
    job = scheduler.get_job(job_id)
    return JobResponse(
        job_id=job_id,
        status="paused",
        next_run=job.next_run_time.isoformat() if job and job.next_run_time else None,
    )


@app.post("/jobs/{job_id}/resume", response_model=JobResponse)
def resume_job(job_id: str) -> JobResponse:
    scheduler.resume_job(job_id)
    _set_job_status(job_id, "active")
    job = scheduler.get_job(job_id)
    return JobResponse(
        job_id=job_id,
        status="active",
        next_run=job.next_run_time.isoformat() if job and job.next_run_time else None,
    )


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str) -> dict[str, str]:
    scheduler.remove_job(job_id)
    _set_job_status(job_id, "cancelled")
    return {"job_id": job_id, "status": "cancelled"}
