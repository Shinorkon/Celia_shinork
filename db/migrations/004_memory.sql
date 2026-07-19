-- Persistent shared memory: goals, decisions, project state, and standing
-- preferences that outlive a single chat thread. Distinct from `messages`
-- (per-chat history, last N turns) - this is memory the memory-writer role
-- explicitly decided was worth keeping, not a transcript.

CREATE TABLE IF NOT EXISTS memory_items (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'goal' | 'decision' | 'project_state' | 'preference'
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  tags TEXT[] NOT NULL DEFAULT '{}',
  project_ref TEXT,                -- e.g. 'agent_orchestration_platform', or NULL for general
  status TEXT NOT NULL DEFAULT 'active',        -- 'active' | 'superseded' | 'archived'
  superseded_by BIGINT REFERENCES memory_items(id),
  source_run_ref TEXT,
  confidence TEXT NOT NULL DEFAULT 'stated',    -- 'stated' | 'inferred'
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_items_kind_status ON memory_items(kind, status);
CREATE INDEX IF NOT EXISTS idx_memory_items_tags ON memory_items USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_memory_items_project ON memory_items(project_ref);
