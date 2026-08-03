-- In-game system messages already posted to the system-message channel.
-- Their ids are a SEPARATE namespace from dm_conversations.conversation_id
-- (the inbox links them as /messages/system_message/<id>), hence an own
-- table instead of reusing the mirror's.

CREATE TABLE IF NOT EXISTS dm_system_messages (
    system_id TEXT PRIMARY KEY,
    subject TEXT,
    posted_at TEXT,
    created_at TEXT NOT NULL
);
