CREATE TABLE cost_periods (
    kind TEXT NOT NULL CHECK (kind IN ('user_day', 'global_day', 'global_month')),
    owner TEXT NOT NULL,
    period TEXT NOT NULL,
    spent_micro_inr INTEGER NOT NULL DEFAULT 0 CHECK (spent_micro_inr >= 0),
    held_micro_inr INTEGER NOT NULL DEFAULT 0 CHECK (held_micro_inr >= 0),
    PRIMARY KEY (kind, owner, period)
);
CREATE TABLE cost_envelopes (
    id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    maximum_micro_inr INTEGER NOT NULL CHECK (maximum_micro_inr > 0),
    spent_micro_inr INTEGER NOT NULL DEFAULT 0 CHECK (spent_micro_inr >= 0),
    held_micro_inr INTEGER NOT NULL DEFAULT 0 CHECK (held_micro_inr >= 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'closed')),
    day_key TEXT NOT NULL,
    month_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (spent_micro_inr + held_micro_inr <= maximum_micro_inr)
);
CREATE TABLE cost_attempts (
    id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT REFERENCES cost_envelopes(session_id),
    job_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    bound_json TEXT NOT NULL,
    rate_card_json TEXT NOT NULL,
    maximum_micro_inr INTEGER NOT NULL CHECK (maximum_micro_inr >= 0),
    actual_micro_inr INTEGER CHECK (actual_micro_inr >= 0),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'unknown', 'disputed', 'settled')),
    outcome TEXT,
    usage_json TEXT,
    day_key TEXT NOT NULL,
    month_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((session_id IS NULL) != (job_id IS NULL))
);
CREATE INDEX ix_cost_attempts_scope ON cost_attempts (user_id, job_id, state);
CREATE INDEX ix_cost_attempts_state ON cost_attempts (state);
