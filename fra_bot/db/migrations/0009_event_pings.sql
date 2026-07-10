-- Event-ping outbox: the scheduler records every REAL alliance
-- mission/event start here (never dry-run); the EventPinger cog delivers
-- them as role pings in the configured Discord channel. An outbox (rather
-- than a direct send) keeps Discord out of the scheduler and survives a
-- restart between start and ping.
CREATE TABLE IF NOT EXISTS event_pings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,             -- 'large' | 'event'
    name       TEXT,                      -- mission caption / event type name
    address    TEXT,                      -- resolved address shown + region source
    latitude   REAL,
    longitude  REAL,
    created_at TEXT NOT NULL,
    posted_at  TEXT                       -- NULL = not yet delivered
);

CREATE INDEX IF NOT EXISTS idx_event_pings_unposted
    ON event_pings (posted_at) WHERE posted_at IS NULL;
