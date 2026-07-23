-- Backfill: tax warnings sent BEFORE they were wired into the central
-- member-action log. One action row per member currently carrying an
-- unresolved warning, stamped with the last warning time, so historical
-- warnings (e.g. sent weeks ago) show up in the dossier/timeline too.
-- Guarded against double-insert if this migration ever re-runs.

INSERT INTO member_actions
    (discord_user_id, mc_user_id, actor_name, action, detail, created_at,
     posted_at)
SELECT NULL, w.mc_user_id, w.username, 'tax_warning_sent',
       'tax warning ' || w.warning_count || ' (backfilled)',
       w.last_warning_at,
       w.last_warning_at   -- already announced in its day: never re-feed
FROM tax_warnings w
WHERE w.warning_count > 0
  AND NOT EXISTS (
      SELECT 1 FROM member_actions a
      WHERE a.mc_user_id = w.mc_user_id AND a.action = 'tax_warning_sent'
  );
