-- Sanctions register (reference bot: sanctionmanager).
-- The bot RECORDS and announces sanctions; it never executes kicks/bans
-- itself (same as the reference cog — enforcement stays a human act).

CREATE TABLE IF NOT EXISTS sanctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mc_user_id INTEGER,             -- target's MissionChief id, when known
    mc_username TEXT,               -- target's name as recorded
    discord_user_id INTEGER,        -- target's Discord id, when known
    admin_discord_id INTEGER NOT NULL,
    admin_name TEXT NOT NULL,
    sanction_type TEXT NOT NULL,    -- the reference bot's type labels
    reason TEXT NOT NULL,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | revoked
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_sanctions_mc ON sanctions (mc_user_id);
CREATE INDEX IF NOT EXISTS idx_sanctions_discord ON sanctions (discord_user_id);
CREATE INDEX IF NOT EXISTS idx_sanctions_status ON sanctions (status);
