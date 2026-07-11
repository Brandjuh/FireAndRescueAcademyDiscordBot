-- Split "the game changed this mission" from "the bot re-rendered this
-- post": content_hash covers data + format version (drives re-renders),
-- data_hash covers the raw game data only. An in-thread "mission updated"
-- message is posted only when data_hash changes — a format bump re-renders
-- 1200 posts silently instead of spamming 1200 update messages. NULL means
-- legacy/unknown: the next sync fills it in without announcing.
ALTER TABLE missions_forum_posts ADD COLUMN data_hash TEXT;
