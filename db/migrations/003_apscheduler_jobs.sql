-- APScheduler SQLAlchemyJobStore backing table.
-- The scheduler service auto-creates this table on startup,
-- but this migration guarantees it exists with the right schema.

CREATE TABLE IF NOT EXISTS apscheduler_jobs (
    id VARCHAR(191) NOT NULL,
    next_run_time DOUBLE PRECISION,
    job_state BYTEA NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_apscheduler_next_run_time
    ON apscheduler_jobs(next_run_time);
