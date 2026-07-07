-- Custom "Own mission" / large scale alliance mission scheduling.
--
-- Members supply the full parameter set themselves (via a Discord panel /
-- slash command, or a structured board post) and the mission is queued to
-- start at the next FREE mission slot (cooldown-aware). One queue serves
-- both sources; the scheduler starts them one at a time.
--
-- status: pending -> waiting -> processing -> done | failed | skipped | cancelled
--   waiting = valid, but must wait for the free-mission cooldown; retried.
CREATE TABLE IF NOT EXISTS scheduled_missions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,          -- discord | board
    -- Mission parameters, all member-supplied. Blank mission_type_id lets
    -- MissionChief pick from the form; coins is pinned to 0 (free-only).
    mission_type_id INTEGER,
    poi_type        INTEGER NOT NULL DEFAULT 0,
    size            INTEGER NOT NULL DEFAULT 1,
    shape           TEXT NOT NULL DEFAULT 'circle',
    amount          INTEGER NOT NULL DEFAULT 1,
    coins           INTEGER NOT NULL DEFAULT 0,
    -- Location: the raw request, then the resolved coordinates.
    location_text   TEXT,
    latitude        REAL,
    longitude       REAL,
    address         TEXT,
    -- Who asked, and where to notify.
    requester_name  TEXT,
    requester_mc_id INTEGER,
    discord_user_id INTEGER,
    channel_id      INTEGER,                -- Discord channel for updates
    board_thread_id INTEGER,               -- board source dedup
    board_post_id   INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending',
    status_detail   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    posted_at       TEXT,                  -- Discord notification marker
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- A board post can only ever enqueue one mission (dedup on re-scan). NULLs
-- are distinct in SQLite, so Discord-sourced rows (NULL board ids) are
-- unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_missions_board_post
    ON scheduled_missions (board_thread_id, board_post_id)
    WHERE board_post_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_scheduled_missions_open
    ON scheduled_missions (status) WHERE status IN ('pending', 'waiting');

CREATE INDEX IF NOT EXISTS idx_scheduled_missions_unposted
    ON scheduled_missions (posted_at) WHERE posted_at IS NULL;
