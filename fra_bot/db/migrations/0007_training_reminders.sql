-- Discord training reminders: when a member requests a training through
-- Discord and opts in, they get pinged once the course should be finished
-- (start + the catalog's duration in days). Swept periodically; posted_at
-- marks delivery so a crash can at worst repeat one ping.
CREATE TABLE IF NOT EXISTS training_reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id INTEGER NOT NULL,
    channel_id      INTEGER,
    training        TEXT NOT NULL,
    due_at          TEXT NOT NULL,
    request_id      INTEGER,
    created_at      TEXT NOT NULL,
    posted_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_training_reminders_due
    ON training_reminders (due_at) WHERE posted_at IS NULL;
