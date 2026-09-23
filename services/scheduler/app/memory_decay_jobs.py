"""Memory decay / expire job — working TTL + episodic salience soft-archive."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import psycopg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://agent_user:agent_pass@postgres:5432/agent_platform"
)

# Real soft-archive when salience drops; dry-run only logs candidates if set.
MEMORY_DECAY_DRY_RUN = os.getenv("MEMORY_DECAY_DRY_RUN", "0").lower() in (
    "1",
    "true",
    "yes",
)

DECAY_JOB_ID = "memory-decay-nightly"


def _conn():
    return psycopg.connect(DATABASE_URL)


def run_memory_decay() -> dict:
    """Expire working segment; decay episodic salience; soft-archive cold episodic."""
    stats = {
        "working_expired": 0,
        "episodic_decayed": 0,
        "episodic_archived": 0,
        "dry_run": MEMORY_DECAY_DRY_RUN,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # 1) Soft-expire working past expire_at
                if MEMORY_DECAY_DRY_RUN:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM memory_items
                        WHERE segment = 'working'
                          AND forgotten_at IS NULL
                          AND expire_at IS NOT NULL
                          AND expire_at < NOW()
                        """
                    )
                    stats["working_expired"] = int(cur.fetchone()[0])
                else:
                    cur.execute(
                        """
                        UPDATE memory_items
                        SET forgotten_at = NOW(),
                            status = 'archived',
                            updated_at = NOW()
                        WHERE segment = 'working'
                          AND forgotten_at IS NULL
                          AND expire_at IS NOT NULL
                          AND expire_at < NOW()
                        """
                    )
                    stats["working_expired"] = cur.rowcount

                # 2) Episodic salience decay: salience *= 0.995 ^ days_since_access
                cur.execute(
                    """
                    SELECT id, salience,
                           EXTRACT(EPOCH FROM (NOW() - COALESCE(last_accessed_at, created_at)))
                             / 86400.0 AS days
                    FROM memory_items
                    WHERE segment = 'episodic'
                      AND forgotten_at IS NULL
                      AND status = 'active'
                    """
                )
                rows = cur.fetchall()
                for mid, sal, days in rows:
                    days_f = float(days or 0)
                    if days_f < 1:
                        continue
                    new_sal = float(sal or 0.5) * (0.995 ** days_f)
                    stats["episodic_decayed"] += 1
                    if MEMORY_DECAY_DRY_RUN:
                        logger.info(
                            "memory_decay_dry_run: id=%s salience %.3f -> %.3f days=%.1f",
                            mid,
                            sal,
                            new_sal,
                            days_f,
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE memory_items
                            SET salience = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            (max(0.0, new_sal), mid),
                        )

                # 3) Soft-archive cold episodic (salience < 0.15, age > 180d)
                if MEMORY_DECAY_DRY_RUN:
                    cur.execute(
                        """
                        SELECT id, title, salience FROM memory_items
                        WHERE segment = 'episodic'
                          AND forgotten_at IS NULL
                          AND status = 'active'
                          AND salience < 0.15
                          AND created_at < NOW() - INTERVAL '180 days'
                        """
                    )
                    cold = cur.fetchall()
                    stats["episodic_archived"] = len(cold)
                    for row in cold:
                        logger.info(
                            "memory_archive_dry_run: id=%s title=%s salience=%s",
                            row[0],
                            row[1],
                            row[2],
                        )
                else:
                    cur.execute(
                        """
                        UPDATE memory_items
                        SET status = 'archived',
                            forgotten_at = COALESCE(forgotten_at, NOW()),
                            updated_at = NOW()
                        WHERE segment = 'episodic'
                          AND forgotten_at IS NULL
                          AND status = 'active'
                          AND salience < 0.15
                          AND created_at < NOW() - INTERVAL '180 days'
                        """
                    )
                    stats["episodic_archived"] = cur.rowcount

            if not MEMORY_DECAY_DRY_RUN:
                conn.commit()
            else:
                conn.rollback()
        logger.info("memory_decay_done: %s", stats)
    except Exception as exc:
        logger.error("memory_decay_error: %s", exc)
        stats["error"] = str(exc)
    return stats


def register_memory_decay_jobs(scheduler) -> None:
    """Nightly 02:30 UTC (~07:30 MVT)."""
    from apscheduler.triggers.cron import CronTrigger

    try:
        existing = scheduler.get_job(DECAY_JOB_ID)
        if existing:
            logger.info("memory_decay_job_exists: %s", DECAY_JOB_ID)
            return
        scheduler.add_job(
            run_memory_decay,
            trigger=CronTrigger(hour=2, minute=30, timezone="UTC"),
            id=DECAY_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("memory_decay_job_registered: %s", DECAY_JOB_ID)
    except Exception as exc:
        logger.error("memory_decay_register_error: %s", exc)
