CREATE TABLE IF NOT EXISTS finance_savings_goals (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  target_mvr NUMERIC(14,2) NOT NULL,
  saved_mvr NUMERIC(14,2) NOT NULL DEFAULT 0,
  monthly_target_mvr NUMERIC(14,2) NOT NULL DEFAULT 0,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS finance_savings_ledger (
  id BIGSERIAL PRIMARY KEY,
  goal_id BIGINT NOT NULL REFERENCES finance_savings_goals(id),
  user_id BIGINT NOT NULL REFERENCES users(id),
  amount_mvr NUMERIC(14,2) NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- optional recurring fixed amounts (scaffold; Falulaan list may come later)
CREATE TABLE IF NOT EXISTS finance_fixed_expenses (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  amount_mvr NUMERIC(14,2) NOT NULL,
  category_id BIGINT REFERENCES finance_categories(id),
  cadence TEXT NOT NULL DEFAULT 'monthly', -- monthly for MVP
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, name)
);
