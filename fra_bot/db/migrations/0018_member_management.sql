-- Member management: self-managed profiles + the central member-action
-- log admins browse (per-member view + admin feed).

CREATE TABLE IF NOT EXISTS member_profiles (
    discord_user_id INTEGER PRIMARY KEY,
    timezone   TEXT,          -- free text ("CET", "Europe/Amsterdam", "UTC-5")
    playtimes  TEXT,          -- when they usually play
    bio        TEXT,          -- about me
    specialties TEXT,         -- what they focus on in the game
    birthday   TEXT,          -- "DD-MM" or "DD-MM-YYYY" (validated on input)
    vehicles   TEXT,          -- self-reported fleet info
    buildings  TEXT,          -- self-reported buildings info
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Every bot-side action a member performs (requests, verify, profile
-- edits, panel clicks, sanctions received). posted_at drives the admin
-- feed publisher; the per-member timeline reads it all back.
CREATE TABLE IF NOT EXISTS member_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id INTEGER,
    mc_user_id INTEGER,
    actor_name TEXT,          -- display name at the time of the action
    action TEXT NOT NULL,     -- short key, e.g. "training_requested"
    detail TEXT,              -- human-readable specifics
    created_at TEXT NOT NULL,
    posted_at TEXT            -- NULL = not yet shown in the admin feed
);

CREATE INDEX IF NOT EXISTS idx_member_actions_discord
    ON member_actions (discord_user_id);
CREATE INDEX IF NOT EXISTS idx_member_actions_mc
    ON member_actions (mc_user_id);
CREATE INDEX IF NOT EXISTS idx_member_actions_pending
    ON member_actions (posted_at) WHERE posted_at IS NULL;
