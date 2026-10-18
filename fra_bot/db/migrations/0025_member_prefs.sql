-- Per-member notification preferences, keyed by MissionChief id so they
-- hold for board requesters too. No row = defaults (in-game PMs on).
-- ingame_pms = 0: the bot sends no in-game PM for request notifications
-- (training/event/building started, reminders, closed-DM fallbacks).
-- Tax warnings and removal notices are not optional and ignore this.
CREATE TABLE IF NOT EXISTS member_prefs (
    mc_user_id  INTEGER PRIMARY KEY,
    ingame_pms  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);
