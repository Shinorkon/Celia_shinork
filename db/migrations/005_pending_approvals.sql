-- Confirm-first tier: a command that needs the user's explicit go-ahead
-- before it runs. Kept as its own table rather than overloading
-- orchestration_runs/checkpoints, since it needs to carry the original
-- command through a Telegram round-trip (a separate inbound message,
-- resolved asynchronously, possibly never answered at all).

CREATE TABLE IF NOT EXISTS pending_approvals (
  id BIGSERIAL PRIMARY KEY,
  run_ref TEXT NOT NULL,
  command TEXT NOT NULL,
  policy_reason TEXT,
  chat_id TEXT NOT NULL,
  thread_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'denied' | 'expired'
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pending_approvals_status_chat ON pending_approvals(status, chat_id);
