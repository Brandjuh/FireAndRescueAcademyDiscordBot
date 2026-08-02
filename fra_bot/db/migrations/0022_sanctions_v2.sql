-- Sanction manager v2: reason catalogue, real mute expiry, provenance,
-- edit audit, and a per-sanction history trail.
--
-- New sanction columns:
--   reason_category  CoC rule code ("1.1" … "4.2") when the reason came
--                    from the catalogue; NULL for free-text reasons.
--   expires_at       real expiry for timed mutes ("Mute 5m" … "Mute 14d");
--                    the game lifts a timed chat ban itself, the bot only
--                    books the transition to status 'expired'.
--   source           who/what created the record:
--                    manual | panel | web | game_log | tax | escalation
--   edited_at/by     last edit stamp (edits also land in sanction_history).

ALTER TABLE sanctions ADD COLUMN reason_category TEXT;
ALTER TABLE sanctions ADD COLUMN expires_at TEXT;
ALTER TABLE sanctions ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE sanctions ADD COLUMN edited_at TEXT;
ALTER TABLE sanctions ADD COLUMN edited_by TEXT;

-- Readable audit trail per sanction (created / edited / approved /
-- dismissed / revoked / expired / mute_set / mute_failed / …).
CREATE TABLE IF NOT EXISTS sanction_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sanction_id INTEGER NOT NULL REFERENCES sanctions (id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sanction_history_sanction
    ON sanction_history (sanction_id);
CREATE INDEX IF NOT EXISTS idx_sanctions_source ON sanctions (source);
CREATE INDEX IF NOT EXISTS idx_sanctions_expires ON sanctions (expires_at)
    WHERE expires_at IS NOT NULL;

-- Backfill provenance for rows the game-log review imported before this
-- migration (they are recognisable by their synthetic admin name).
UPDATE sanctions SET source = 'game_log'
    WHERE admin_name LIKE 'MissionChief log:%';
