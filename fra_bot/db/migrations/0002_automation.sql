-- Phase 2: alliance board automation (trainings, buildings, events).

-- Raw board posts we have seen, per thread. Baseline + dedup live here
-- (not in memory), so restarts never re-process or skip posts.
CREATE TABLE IF NOT EXISTS board_posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id     INTEGER NOT NULL,
    post_id       INTEGER NOT NULL,    -- MissionChief /alliance_posts/<id>
    author_name   TEXT,
    author_mc_id  INTEGER,
    raw_timestamp TEXT,
    content       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE (thread_id, post_id)
);

-- One row per member request extracted from a board post.
-- status: pending -> done | failed | skipped | waiting
--   waiting = valid request that must wait (funds, cooldown); retried.
CREATE TABLE IF NOT EXISTS automation_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,      -- training | building | event
    thread_id      INTEGER NOT NULL,
    post_id        INTEGER NOT NULL,
    requester_name TEXT,
    requester_mc_id INTEGER,
    payload        TEXT,               -- JSON: parsed request details / results
    status         TEXT NOT NULL DEFAULT 'pending',
    status_detail  TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,              -- for waiting requests
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    posted_at      TEXT                -- Discord notification marker
);
CREATE INDEX IF NOT EXISTS idx_automation_requests_open
    ON automation_requests (status) WHERE status IN ('pending', 'waiting');
CREATE INDEX IF NOT EXISTS idx_automation_requests_pending_post
    ON automation_requests (posted_at) WHERE posted_at IS NULL;
