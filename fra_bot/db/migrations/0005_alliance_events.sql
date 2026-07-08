-- Alliance-event options for the unified mission system.
--
-- An event request/rotation entry (kind = 'event') carries its own knobs,
-- from the real /missionAllianceEventNew form: which event type (0-7, or a
-- random pick), the Area (size), Shape and Call volume (amount). These are
-- NULL/irrelevant for large scale missions.
--
-- ALTERs run exactly once (version-tracked, transactional DDL).
-- scheduled_missions already has a `shape` column from migration 0003's
-- earlier model; the event shape reuses it, so it is not re-added here.
ALTER TABLE scheduled_missions ADD COLUMN event_type_id INTEGER;
ALTER TABLE scheduled_missions ADD COLUMN event_random INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_missions ADD COLUMN area TEXT;
ALTER TABLE scheduled_missions ADD COLUMN call_volume TEXT;

ALTER TABLE mission_rotation ADD COLUMN event_type_id INTEGER;
ALTER TABLE mission_rotation ADD COLUMN event_random INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mission_rotation ADD COLUMN area TEXT;
ALTER TABLE mission_rotation ADD COLUMN shape TEXT;
ALTER TABLE mission_rotation ADD COLUMN call_volume TEXT;
