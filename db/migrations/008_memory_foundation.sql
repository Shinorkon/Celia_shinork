-- Life OS slice 1: segmented memory foundation (additive).
-- No pgvector: skip embedding column for v1 (keyword + recency + tags).

ALTER TABLE memory_items
  ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS segment TEXT NOT NULL DEFAULT 'semantic',
  ADD COLUMN IF NOT EXISTS entity_ids BIGINT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS importance REAL NOT NULL DEFAULT 0.5,
  ADD COLUMN IF NOT EXISTS salience REAL NOT NULL DEFAULT 0.5,
  ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS access_count INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS expire_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS forgotten_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS correct_of BIGINT REFERENCES memory_items(id),
  ADD COLUMN IF NOT EXISTS source_chat_id TEXT,
  ADD COLUMN IF NOT EXISTS source_message_ids TEXT[];

-- segment check (additive; default already semantic)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'memory_items_segment_check'
  ) THEN
    ALTER TABLE memory_items
      ADD CONSTRAINT memory_items_segment_check
      CHECK (segment IN ('episodic', 'semantic', 'procedural', 'working'));
  END IF;
END $$;

-- Generated tsvector for keyword retrieval (title + body)
ALTER TABLE memory_items
  ADD COLUMN IF NOT EXISTS search_tsv tsvector
  GENERATED ALWAYS AS (
    to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))
  ) STORED;

CREATE INDEX IF NOT EXISTS idx_memory_user_segment_status
  ON memory_items(user_id, segment, status)
  WHERE forgotten_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_tags
  ON memory_items USING GIN(tags);

CREATE INDEX IF NOT EXISTS idx_memory_entities
  ON memory_items USING GIN(entity_ids);

CREATE INDEX IF NOT EXISTS idx_memory_tsv
  ON memory_items USING GIN(search_tsv);

CREATE INDEX IF NOT EXISTS idx_memory_expire
  ON memory_items(expire_at)
  WHERE expire_at IS NOT NULL AND forgotten_at IS NULL;

CREATE TABLE IF NOT EXISTS memory_entities (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  entity_type TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  aliases TEXT[] NOT NULL DEFAULT '{}',
  attrs_jsonb JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(user_id, entity_type, canonical_name)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_aliases
  ON memory_entities USING GIN(aliases);

CREATE TABLE IF NOT EXISTS memory_corrections (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  target_id BIGINT NOT NULL REFERENCES memory_items(id),
  action TEXT NOT NULL,
  replacement_id BIGINT REFERENCES memory_items(id),
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_corrections_user
  ON memory_corrections(user_id, created_at DESC);

-- Backfill: map legacy kind → segment; attach owner user_id where single-owner legacy
UPDATE memory_items
SET segment = CASE
  WHEN kind IN ('preference', 'goal', 'fact', 'note') THEN 'semantic'
  WHEN kind IN ('habit') THEN 'procedural'
  WHEN kind IN ('decision', 'project_state', 'event') THEN 'episodic'
  WHEN kind IN ('correction') THEN 'semantic'
  ELSE segment
END
WHERE segment = 'semantic'
  AND kind IN ('decision', 'project_state', 'event', 'preference', 'goal', 'fact', 'habit', 'note', 'correction');

-- Prefer authorized owner for orphan legacy rows (Life OS v1 is owner-scoped)
UPDATE memory_items mi
SET user_id = u.id
FROM users u
WHERE mi.user_id IS NULL
  AND u.telegram_user_id = 929388047;

-- Soft-forget obvious junk that polluted semantic/episodic (list qty + receipt totals)
UPDATE memory_items
SET forgotten_at = NOW(),
    status = 'archived',
    updated_at = NOW()
WHERE forgotten_at IS NULL
  AND (
    title ILIKE '%quantity%'
    OR title ILIKE '%spending total%'
    OR title ILIKE '%receipt processed%'
    OR title ILIKE '%updated spending%'
    OR (kind = 'preference' AND body ~* '\d+\s*(units?|pcs|pieces|x)\b')
    OR (kind = 'project_state' AND body ~* '(total spending|receipt from|mvr)')
    OR (kind = 'goal' AND title ILIKE '%total spending%')
  );
