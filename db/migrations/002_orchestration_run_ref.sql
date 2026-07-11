ALTER TABLE orchestration_runs
ADD COLUMN IF NOT EXISTS run_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_orchestration_runs_run_ref
ON orchestration_runs(run_ref);