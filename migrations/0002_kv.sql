-- 0002_kv: small key/value state store for operator toggles that must
-- survive restarts and be visible to both processes (pause window, muted
-- categories). Not for secrets — those never enter the DB.

CREATE TABLE kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
