-- Per-action-key log routing: mirror each alliance-log type to extra
-- channels. A second publisher stream drains routed_at IS NULL rows, so it
-- needs its own marker independent of posted_at (the main feed).
--
-- Anti-flood: back-fill routed_at = posted_at. Rows already announced to the
-- main channel (posted_at set) are stamped routed (never mirrored as
-- history); rows still pending at upgrade (posted_at NULL) stay routable and
-- get mirrored right after they post, so the two feeds stay in lockstep.
ALTER TABLE alliance_logs ADD COLUMN routed_at TEXT;

UPDATE alliance_logs SET routed_at = posted_at;

CREATE INDEX IF NOT EXISTS idx_alliance_logs_unrouted
    ON alliance_logs (routed_at) WHERE routed_at IS NULL;
