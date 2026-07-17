-- Game-data sync: what the member's own userscript reports about their
-- account (buildings, vehicles, coordinates). One row per MC account.

CREATE TABLE IF NOT EXISTS game_sync (
    mc_user_id INTEGER PRIMARY KEY,
    discord_user_id INTEGER,        -- resolved via the verified link
    mc_name TEXT,
    building_count INTEGER NOT NULL DEFAULT 0,
    vehicle_count INTEGER NOT NULL DEFAULT 0,
    buildings_json TEXT,            -- {"by_type": {...}, "coords": [[lat,lng]..]}
    vehicles_json TEXT,             -- {"by_type": {...}}
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_game_sync_discord ON game_sync (discord_user_id);
