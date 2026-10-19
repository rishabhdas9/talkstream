CREATE TABLE recovery_holds (
    id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    until_day TEXT,
    created_at TEXT NOT NULL
);
