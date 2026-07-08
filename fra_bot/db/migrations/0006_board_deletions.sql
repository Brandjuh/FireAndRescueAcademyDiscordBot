-- Board post cleanup: request posts we've handled are removed after a grace
-- period (12h) so the request boards don't fill up with stale posts.
--
-- Rows are added only in LIVE mode, when a board-sourced request reaches a
-- terminal state (done/failed/skipped) — never in dry-run, where the other
-- bot still owns the board. A periodic sweep deletes rows whose due_at has
-- passed; failures back off and are dropped after a few attempts.
CREATE TABLE IF NOT EXISTS board_pending_deletions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    reason     TEXT,
    due_at     TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (thread_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_board_deletions_due
    ON board_pending_deletions (due_at);
