-- In-game DM mirror: every MissionChief private-message conversation gets
-- one Discord forum thread. last_activity is the newest mirrored message
-- timestamp (data-message-time, ISO) — the dedup marker for rescans;
-- mirrored_count is bookkeeping for diagnostics.
CREATE TABLE IF NOT EXISTS dm_conversations (
    conversation_id TEXT PRIMARY KEY,
    username        TEXT,
    subject         TEXT,
    thread_id       INTEGER,
    mirrored_count  INTEGER NOT NULL DEFAULT 0,
    last_activity   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dm_conversations_thread
    ON dm_conversations(thread_id);
