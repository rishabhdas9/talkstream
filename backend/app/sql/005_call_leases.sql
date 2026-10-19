CREATE TABLE call_leases (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    envelope_id TEXT NOT NULL UNIQUE REFERENCES cost_envelopes(id),
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    day_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_call_leases_user_state ON call_leases (user_id, state);
CREATE INDEX ix_call_leases_user_day ON call_leases (user_id, day_key);
CREATE INDEX ix_call_leases_state ON call_leases (state);
