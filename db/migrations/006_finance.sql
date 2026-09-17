CREATE TABLE IF NOT EXISTS finance_categories (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'variable', -- 'fixed' | 'variable' | 'income'
  monthly_limit_mvr NUMERIC(14,2) NOT NULL DEFAULT 0,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS finance_transactions (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  category_id BIGINT REFERENCES finance_categories(id),
  tx_type TEXT NOT NULL, -- 'expense' | 'income'
  amount_mvr NUMERIC(14,2) NOT NULL,
  merchant TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  tx_date DATE NOT NULL DEFAULT (CURRENT_DATE),
  receipt_image_path TEXT, -- nullable; retain until user deletes (phase 2)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_finance_tx_user_date ON finance_transactions(user_id, tx_date DESC);

CREATE TABLE IF NOT EXISTS finance_pending_logs (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT NOT NULL,
  telegram_user_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL REFERENCES users(id),
  payload_json JSONB NOT NULL, -- parsed tx fields awaiting confirm
  status TEXT NOT NULL DEFAULT 'pending', -- pending|confirmed|cancelled|expired
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_finance_pending_chat ON finance_pending_logs(status, chat_id);
