-- New-member welcome: a game-chat greeting posted once per member who
-- joins the alliance. Keyed off the roster "joined" event (catches every
-- join: auto-accept, admin in-game, or the Discord accept button).
--
-- welcomed_at marks a joined event whose welcome has been posted. EXISTING
-- rows are baselined as already-welcomed, so enabling the feature never
-- greets the whole current roster — only members who join afterwards.

ALTER TABLE member_events ADD COLUMN welcomed_at TEXT;

UPDATE member_events SET welcomed_at = occurred_at WHERE welcomed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_member_events_welcome
    ON member_events (event_type, welcomed_at);
