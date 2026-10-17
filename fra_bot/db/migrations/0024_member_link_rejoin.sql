-- A linked member who left the Discord server and came back under a
-- nickname that no longer matches their linked MissionChief name. Set on
-- join, cleared by re-verifying (nickname matches the roster again).
-- While set, the link grants nothing: no "already verified", no hourly
-- auto-restore of the Verified role.
ALTER TABLE member_links ADD COLUMN rejoin_pending_at TEXT;
