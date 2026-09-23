-- Life OS slice 3: optional merchant entity link on finance rows.
ALTER TABLE finance_transactions
  ADD COLUMN IF NOT EXISTS entity_ids BIGINT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_finance_tx_entities
  ON finance_transactions USING GIN(entity_ids);
