CREATE TABLE tool_receipts (
    call_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT NOT NULL REFERENCES usage_sessions(id),
    turn_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    state TEXT NOT NULL CHECK (state IN ('pending', 'succeeded', 'failed', 'unknown')),
    receipt_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_tool_receipts_owner ON tool_receipts(user_id, call_id);
CREATE INDEX ix_tool_receipts_session_turn ON tool_receipts(session_id, turn_id);
