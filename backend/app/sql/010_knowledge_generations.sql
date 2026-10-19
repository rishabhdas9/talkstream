ALTER TABLE knowledge_reindex_jobs ADD COLUMN generation_id VARCHAR(36);
ALTER TABLE knowledge_reindex_jobs ADD COLUMN profile_snapshot TEXT;
ALTER TABLE knowledge_reindex_jobs ADD COLUMN source_snapshot TEXT;

CREATE TABLE knowledge_deletion_events (
    id VARCHAR(36) PRIMARY KEY NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36) UNIQUE NOT NULL,
    created_at DATETIME NOT NULL,
    completed_at DATETIME
);
