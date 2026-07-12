-- Vehicles-database forum: one Discord forum post per vehicle from the
-- community LSS-Manager catalog (vehicles.ts). Mirrors the missions forum:
-- vehicle_key -> thread_id prevents duplicate posts; content_hash (data +
-- format version) drives in-place re-renders; data_hash (raw data only)
-- decides whether a real game-side change gets an "updated" note, so a
-- format bump can re-render every post without a ping storm. A row with an
-- empty content_hash was adopted from an existing thread (DB-loss recovery)
-- and gets its content refreshed on the next sync.
CREATE TABLE IF NOT EXISTS vehicles_forum_posts (
    vehicle_key   TEXT PRIMARY KEY,
    thread_id     INTEGER NOT NULL,
    name          TEXT,
    content_hash  TEXT NOT NULL DEFAULT '',
    data_hash     TEXT,
    posted_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT
);
