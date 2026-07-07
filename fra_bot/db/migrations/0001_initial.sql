-- Initial schema.
--
-- Conventions:
--  * All timestamps are UTC ISO-8601 strings (seconds precision).
--  * "raw_*" columns keep the text exactly as MissionChief displayed it,
--    so parsing bugs can be fixed retroactively.
--  * Discord-facing rows carry a posted_at column: NULL means "not yet
--    announced". Publishers mark rows instead of keeping a watermark, so
--    a crash can never skip or double-post entries.

-- ----------------------------------------------------------------------
-- Bookkeeping
-- ----------------------------------------------------------------------

CREATE TABLE scraper_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | success | failed
    pages       INTEGER NOT NULL DEFAULT 0,
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    rows_new    INTEGER NOT NULL DEFAULT 0,
    message     TEXT
);
CREATE INDEX idx_scrape_runs_scraper ON scrape_runs (scraper, started_at);

-- ----------------------------------------------------------------------
-- Members
-- ----------------------------------------------------------------------

-- Current roster: one row per MissionChief user we have ever seen.
CREATE TABLE members (
    mc_user_id        INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    role              TEXT,
    earned_credits    INTEGER,
    contribution_rate REAL,     -- alliance contribution / tax, percent
    raw_member_since  TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    left_at           TEXT
);
CREATE INDEX idx_members_active ON members (is_active);
CREATE INDEX idx_members_name ON members (name);

-- Hourly history, used for earned-credit deltas and reports.
CREATE TABLE member_snapshots (
    run_id            INTEGER NOT NULL REFERENCES scrape_runs (id),
    mc_user_id        INTEGER NOT NULL,
    name              TEXT NOT NULL,
    role              TEXT,
    earned_credits    INTEGER,
    contribution_rate REAL,
    taken_at          TEXT NOT NULL,
    PRIMARY KEY (mc_user_id, run_id)
);
CREATE INDEX idx_member_snapshots_taken ON member_snapshots (taken_at);

-- Detected changes, each announced to Discord at most once.
CREATE TABLE member_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mc_user_id  INTEGER,
    name        TEXT NOT NULL,
    event_type  TEXT NOT NULL,   -- joined | left | role_changed | contribution_changed | name_changed
    old_value   TEXT,
    new_value   TEXT,
    occurred_at TEXT NOT NULL,
    posted_at   TEXT
);
CREATE INDEX idx_member_events_pending ON member_events (posted_at) WHERE posted_at IS NULL;

-- ----------------------------------------------------------------------
-- Applications
-- ----------------------------------------------------------------------

CREATE TABLE applications (
    application_id INTEGER PRIMARY KEY,   -- id from /verband/bewerbungen/annehmen/<id>
    applicant_name TEXT NOT NULL,
    mc_user_id     INTEGER,               -- from /profile/<id> link, when present
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    resolved_at    TEXT,                  -- application no longer listed
    posted_at      TEXT
);
CREATE INDEX idx_applications_open ON applications (resolved_at) WHERE resolved_at IS NULL;

-- ----------------------------------------------------------------------
-- Alliance logs
-- ----------------------------------------------------------------------

CREATE TABLE alliance_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    signature           TEXT NOT NULL,    -- sha256 over the row's visible content
    occurrence_index    INTEGER NOT NULL DEFAULT 1,  -- disambiguates identical rows
    raw_timestamp       TEXT NOT NULL,
    event_at            TEXT,             -- normalized UTC, NULL if unparseable
    action_key          TEXT NOT NULL,
    description         TEXT NOT NULL,
    executed_name       TEXT,
    executed_mc_id      INTEGER,
    affected_name       TEXT,
    affected_type       TEXT,             -- user | building | mission | vehicle | ''
    affected_mc_id      INTEGER,
    contribution_amount INTEGER,
    scraped_at          TEXT NOT NULL,
    posted_at           TEXT,
    UNIQUE (signature, occurrence_index)
);
CREATE INDEX idx_alliance_logs_pending ON alliance_logs (posted_at) WHERE posted_at IS NULL;
CREATE INDEX idx_alliance_logs_action ON alliance_logs (action_key);
CREATE INDEX idx_alliance_logs_event_at ON alliance_logs (event_at);

-- ----------------------------------------------------------------------
-- Treasury
-- ----------------------------------------------------------------------

-- Time series of the alliance's total funds.
CREATE TABLE treasury_balance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    total_funds INTEGER NOT NULL,
    scraped_at  TEXT NOT NULL
);
CREATE INDEX idx_treasury_balance_time ON treasury_balance (scraped_at);

-- Income top lists. MissionChief resets these at midnight America/New_York,
-- so snapshots are keyed by the NY-local period they belong to:
-- period_key = '2026-07-06' for daily, '2026-07' for monthly.
CREATE TABLE income_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    period     TEXT NOT NULL,             -- daily | monthly
    period_key TEXT NOT NULL,
    taken_at   TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    username   TEXT NOT NULL,
    mc_user_id INTEGER,
    amount     INTEGER NOT NULL
);
CREATE INDEX idx_income_snapshots_lookup ON income_snapshots (period, period_key, taken_at);

-- Expense ledger. MissionChief shows this newest-first with visually
-- identical rows that are REAL distinct events, so there is deliberately
-- no unique constraint: correctness comes from the sequence-anchored
-- sync in treasury_sync.py, which always inserts oldest-first. That
-- makes ascending id == chronological order.
CREATE TABLE expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signature   TEXT NOT NULL,            -- sha256(raw_date|username|amount|description)
    raw_date    TEXT NOT NULL,
    event_at    TEXT,                     -- normalized UTC, NULL when ambiguous
    username    TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    description TEXT,
    scraped_at  TEXT NOT NULL
);
CREATE INDEX idx_expenses_signature ON expenses (signature);
CREATE INDEX idx_expenses_event_at ON expenses (event_at);
CREATE INDEX idx_expenses_username ON expenses (username);

-- Staging area for the initial expenses backfill (3150+ pages). Pages
-- are walked 1..last (newest to oldest) in resumable chunks and rows
-- appended here in DISPLAY order (newest first). When the walk reaches
-- the last page, everything is copied into expenses reversed (oldest
-- first) and this table is emptied. Progress lives in scraper_state.
CREATE TABLE expenses_backfill_staging (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signature   TEXT NOT NULL,
    raw_date    TEXT NOT NULL,
    event_at    TEXT,
    username    TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    description TEXT
);
