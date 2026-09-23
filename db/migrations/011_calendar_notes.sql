-- Life OS slice 4: Celia-only calendar (thin). Notes use memory_items kind=note.

CREATE TABLE IF NOT EXISTS calendar_events (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  starts_at TIMESTAMPTZ NOT NULL,
  ends_at TIMESTAMPTZ NOT NULL,
  title TEXT NOT NULL,
  location TEXT,
  entity_ids BIGINT[] NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'telegram',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT calendar_events_range_check CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_user_starts
  ON calendar_events(user_id, starts_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_calendar_events_user_range
  ON calendar_events(user_id, starts_at, ends_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_calendar_events_entities
  ON calendar_events USING GIN(entity_ids);
