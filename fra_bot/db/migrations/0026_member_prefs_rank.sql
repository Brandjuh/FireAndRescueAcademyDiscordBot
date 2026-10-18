-- rank_announce = 0: the member's credit-rank promotions are not
-- announced (no @-mention in the promotion channel). The rank ROLE is
-- still assigned either way.
ALTER TABLE member_prefs ADD COLUMN rank_announce INTEGER NOT NULL DEFAULT 1;
