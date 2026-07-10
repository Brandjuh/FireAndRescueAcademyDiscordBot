-- Member tax (alliance donation) warning state — the reference bot's
-- MessageManager system, ported. One row per member the system ever
-- touched: how many warnings they got, when the last one was sent, when
-- they were flagged/kicked, and when they resolved it. A member who fixes
-- their donation gets RESET (count back to 0) so warnings stop immediately
-- and a later dip starts over at warning 1 — the old bot never reset,
-- which is why it kept warning members who had already fixed their tax.
CREATE TABLE IF NOT EXISTS tax_warnings (
    mc_user_id      INTEGER PRIMARY KEY,
    username        TEXT,
    warning_count   INTEGER NOT NULL DEFAULT 0,
    last_warning_at TEXT,
    kick_flagged_at TEXT,
    kicked_at       TEXT,
    resolved_at     TEXT,
    updated_at      TEXT NOT NULL
);
