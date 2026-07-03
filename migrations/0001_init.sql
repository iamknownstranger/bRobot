-- 0001_init: full initial schema for the freebie-agent ledger.
-- Applied only via `freebie-worker --migrate` (the worker refuses to start
-- while this is pending).

CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    dedupe_hash   TEXT NOT NULL UNIQUE,      -- sha256 of normalized url + title
    raw_title     TEXT NOT NULL,
    raw_body      TEXT NOT NULL DEFAULT '',
    fetched_at    TEXT NOT NULL,
    extract_json  TEXT,
    extract_error TEXT,
    status        TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new','extracted','scored','queued','digested',
                                    'dropped','claimed','expired')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_items_status ON items(status);
CREATE INDEX idx_items_source ON items(source_id);

CREATE TABLE scores (
    id             INTEGER PRIMARY KEY,
    item_id        INTEGER NOT NULL REFERENCES items(id),
    score          INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    reason         TEXT NOT NULL,
    prompt_version TEXT NOT NULL,            -- git short-sha of prompts/ at scoring time
    model_tag      TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_scores_item ON scores(item_id);

CREATE TABLE claims (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    state         TEXT NOT NULL DEFAULT 'proposed'
                  CHECK (state IN ('proposed','approved','skipped','claimed',
                                   'arrived','failed')),
    operator_note TEXT,
    worth_it      INTEGER,                   -- nullable bool (0/1), post-hoc feedback
    proposed_at   TEXT,
    approved_at   TEXT,
    skipped_at    TEXT,
    claimed_at    TEXT,
    arrived_at    TEXT,
    failed_at     TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_claims_item ON claims(item_id);
CREATE INDEX idx_claims_state ON claims(state);

CREATE TABLE deadlines (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER REFERENCES items(id),
    kind         TEXT NOT NULL
                 CHECK (kind IN ('trial_cancel','coupon_expiry','points_expiry',
                                 'shipping_check','custom')),
    due_at       TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending','nagged','done','missed')),
    nags_sent    TEXT NOT NULL DEFAULT '[]', -- JSON list of nag hour-marks already sent
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_deadlines_due ON deadlines(state, due_at);

CREATE TABLE sources (
    id             TEXT PRIMARY KEY,          -- mirrors sources.yaml id
    type           TEXT NOT NULL,
    url            TEXT NOT NULL DEFAULT '',
    schedule       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','shadow','paused','killed')),
    items_seen     INTEGER NOT NULL DEFAULT 0,
    items_queued   INTEGER NOT NULL DEFAULT 0,
    items_claimed  INTEGER NOT NULL DEFAULT 0,
    last_ok_at     TEXT,
    last_error     TEXT,
    last_page_hash TEXT,                      -- http_page fetcher: last content hash
    shadow_since   TEXT,                      -- when the source entered shadow state
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Append-only: every notification sent, button tapped, free-text message,
-- intent resolved, critic action, pipeline counter tick. Critic raw material.
CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    item_id    INTEGER,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_events_kind_time ON events(kind, created_at);

CREATE TABLE proposals (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL
                CHECK (kind IN ('prompt_diff','profile_diff','source_change',
                                'threshold_change')),
    diff        TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    eval_before REAL,
    eval_after  REAL,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','applied','rejected','expired')),
    git_commit  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Outbox: pending outbound bot messages written by the worker/critic and
-- polled by the bot process (both share this SQLite file; see DECISIONS.md).
CREATE TABLE outbox (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                -- notify|digest|nag|question|proposal
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sent_at    TEXT
);
CREATE INDEX idx_outbox_unsent ON outbox(sent_at) WHERE sent_at IS NULL;

-- Open questions the agent asked the operator; TTL-expired => no_answer.
CREATE TABLE questions (
    id         INTEGER PRIMARY KEY,
    topic      TEXT NOT NULL,
    item_id    INTEGER,
    payload    TEXT NOT NULL DEFAULT '{}',
    state      TEXT NOT NULL DEFAULT 'open'
               CHECK (state IN ('open','answered','no_answer')),
    asked_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_at TEXT
);
