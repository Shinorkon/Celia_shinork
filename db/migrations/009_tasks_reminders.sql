-- Life OS slice 2: dated tasks + reminders (Postgres). Redis shopping lists unchanged.

CREATE TABLE IF NOT EXISTS task_lists (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  list_id BIGINT REFERENCES task_lists(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  due_at TIMESTAMPTZ,
  reminder_id BIGINT,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_status_due
  ON tasks(user_id, status, due_at)
  WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_tasks_list
  ON tasks(list_id)
  WHERE status = 'open';

CREATE TABLE IF NOT EXISTS reminders (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  chat_id TEXT NOT NULL,
  thread_id TEXT,
  title TEXT NOT NULL,
  body TEXT,
  kind TEXT NOT NULL DEFAULT 'once',
  run_at TIMESTAMPTZ,
  cron_expr TEXT,
  timezone TEXT NOT NULL DEFAULT 'Indian/Maldives',
  scheduler_job_id TEXT,
  task_id BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'active',
  snooze_until TIMESTAMPTZ,
  bundle_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reminders_user_status_run
  ON reminders(user_id, status, run_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_reminders_scheduler_job
  ON reminders(scheduler_job_id)
  WHERE scheduler_job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reminders_bundle
  ON reminders(user_id, bundle_key)
  WHERE bundle_key IS NOT NULL AND status = 'active';

-- Optional back-link from tasks.reminder_id once reminders exist
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'tasks_reminder_id_fkey'
  ) THEN
    ALTER TABLE tasks
      ADD CONSTRAINT tasks_reminder_id_fkey
      FOREIGN KEY (reminder_id) REFERENCES reminders(id) ON DELETE SET NULL;
  END IF;
END $$;
