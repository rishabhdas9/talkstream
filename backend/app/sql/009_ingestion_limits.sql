ALTER TABLE knowledge_sources ADD COLUMN source_sha256 VARCHAR(64);
ALTER TABLE knowledge_sources ADD COLUMN extraction_version VARCHAR(128) NOT NULL DEFAULT 'legacy-unknown';
ALTER TABLE knowledge_sources ADD COLUMN chunking_version VARCHAR(128) NOT NULL DEFAULT 'legacy-unknown';
CREATE TABLE knowledge_ingestions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved','started','stored','complete','failed','released')),
    source_id TEXT REFERENCES knowledge_sources(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_knowledge_ingestions_user_day ON knowledge_ingestions(user_id,day_key,state);
