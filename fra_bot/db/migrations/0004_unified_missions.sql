-- Unified mission/event system.
--
-- A single queue (scheduled_missions) now carries the full request model:
-- kind (event | large), source (preset | custom | saved), the custom "Own
-- mission" data, an optional saved-mission name, and whether the request is
-- recurring. Recurring requests are promoted to the rotation list.
--
-- The rotation list (mission_rotation) is an admin-managed set of locations
-- the bot cycles through forever, one per free mission slot, filling the gaps
-- when no member request is pending (member-first priority). The scheduler
-- can also report which rotation entry is up next and where — for the
-- eventpinger.
--
-- These ALTERs run exactly once (version-tracked, transactional DDL), so a
-- crash mid-migration rolls back cleanly and replays from scratch.
ALTER TABLE scheduled_missions ADD COLUMN kind TEXT NOT NULL DEFAULT 'large';
ALTER TABLE scheduled_missions ADD COLUMN mission_source TEXT NOT NULL DEFAULT 'preset';
ALTER TABLE scheduled_missions ADD COLUMN preset_type_id INTEGER;
ALTER TABLE scheduled_missions ADD COLUMN caption TEXT;
ALTER TABLE scheduled_missions ADD COLUMN custom_values TEXT;   -- JSON {key: int}
ALTER TABLE scheduled_missions ADD COLUMN saved_name TEXT;
ALTER TABLE scheduled_missions ADD COLUMN recurring INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_missions ADD COLUMN rotation_id INTEGER;  -- source rotation entry, if any

-- Admin rotation list: locations the bot auto-starts and keeps cycling.
CREATE TABLE IF NOT EXISTS mission_rotation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    location_text   TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'large',      -- event | large
    mission_source  TEXT NOT NULL DEFAULT 'preset',     -- preset | custom | saved
    preset_type_id  INTEGER,
    caption         TEXT,
    custom_values   TEXT,                               -- JSON {key: int}
    saved_name      TEXT,
    -- Cached geocode so "what's next and where" is answerable without a
    -- fresh lookup, and so a bad location is caught once.
    latitude        REAL,
    longitude       REAL,
    address         TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT,
    last_started_at TEXT,                               -- UTC ISO of last start
    start_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Next-up selection: active entries, oldest-started first (NULLs first).
CREATE INDEX IF NOT EXISTS idx_mission_rotation_next
    ON mission_rotation (active, last_started_at);
