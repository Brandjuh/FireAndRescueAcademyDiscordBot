-- MemberSync: Discord <-> MissionChief identity links, built on OUR OWN
-- member roster (the members table) — no external database.
--
-- links: one row per Discord account (re-verifying overwrites), and one
-- MC account can only be claimed once (UNIQUE). reviewer_id 0 = auto.
CREATE TABLE IF NOT EXISTS member_links (
    discord_id  INTEGER PRIMARY KEY,
    mc_user_id  INTEGER NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'approved',   -- approved | denied
    reviewer_id INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Verification queue: members whose nickname didn't match the roster yet
-- (fresh joins take a sync cycle to appear). Re-checked every couple of
-- minutes; expires after a bounded number of attempts.
CREATE TABLE IF NOT EXISTS verify_queue (
    discord_id   INTEGER PRIMARY KEY,
    mc_user_id   INTEGER,             -- optional user-supplied hint
    display_name TEXT,
    guild_id     INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    enqueued_at  TEXT NOT NULL
);
