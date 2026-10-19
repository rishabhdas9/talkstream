CREATE TABLE voice_tickets (
    ticket_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    access_expires_at TEXT NOT NULL
);
CREATE INDEX ix_voice_tickets_user ON voice_tickets (user_id);
CREATE INDEX ix_voice_tickets_expiry ON voice_tickets (expires_at);
