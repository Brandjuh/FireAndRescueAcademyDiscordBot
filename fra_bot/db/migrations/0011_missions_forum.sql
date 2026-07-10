-- Missions-database forum: one Discord forum post per mission from
-- missionchief.com/einsaetze.json. The mapping mission_key -> thread_id is
-- what prevents duplicate posts; content_hash detects data changes so the
-- existing starter message is edited in place instead of reposted. A row
-- with an empty content_hash was adopted from an existing thread (DB-loss
-- recovery) and gets its content refreshed on the next sync.
CREATE TABLE IF NOT EXISTS missions_forum_posts (
    mission_key   TEXT PRIMARY KEY,
    thread_id     INTEGER NOT NULL,
    name          TEXT,
    content_hash  TEXT NOT NULL DEFAULT '',
    posted_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT
);
