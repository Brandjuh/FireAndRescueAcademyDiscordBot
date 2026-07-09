"""MemberSync: link Discord members to MissionChief accounts.

Modelled on the reference bot's flow, but built on OUR OWN scraped roster
(the ``members`` table) instead of an external database. Proof of alliance
membership is the member's Discord server nickname exactly matching a
roster name (case-insensitive), or a user-supplied MC id that exists in
the roster — no token challenge, same as the reference bot.

Fresh alliance joins take a sync cycle to appear in the roster, so misses
go to a bounded retry queue (re-checked every couple of minutes, expiring
after :data:`QUEUE_MAX_ATTEMPTS`). An hourly prune removes the verified
role once a linked member leaves the alliance — gated on roster health so
a broken scrape can never mass-derole the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from ..db.database import Database
from ..db.repos import LinksRepo, MembersRepo

log = logging.getLogger(__name__)

QUEUE_MAX_ATTEMPTS = 30          # ~1 hour at the 2-minute loop interval
MIN_SAFE_ROSTER_COUNT = 100      # prune safety gate (alliance has ~950)


@dataclass(frozen=True)
class VerifyOutcome:
    outcome: str                 # already_verified | already_queued | approved | queued
    mc_user_id: int | None = None
    mc_name: str | None = None
    attempts: int = 0


class MemberSyncService:
    def __init__(self, db: Database) -> None:
        self._db = db
        self.links = LinksRepo(db)
        self.members = MembersRepo(db)

    # -- roster lookup ---------------------------------------------------

    async def lookup(
        self, display_name: str | None, mc_user_id: int | None
    ) -> aiosqlite.Row | None:
        """An ACTIVE roster member by MC id, else by exact (case-insensitive)
        name match against the Discord nickname — the reference bot's rule."""
        if mc_user_id:
            async with self._db.conn.execute(
                "SELECT * FROM members WHERE mc_user_id = ? AND is_active = 1",
                (int(mc_user_id),),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                return row
        if display_name:
            async with self._db.conn.execute(
                "SELECT * FROM members WHERE lower(name) = lower(?) AND is_active = 1",
                (display_name.strip(),),
            ) as cur:
                return await cur.fetchone()
        return None

    # -- member-initiated verification ------------------------------------

    async def request_verification(
        self,
        discord_id: int,
        display_name: str,
        mc_user_id: int | None,
        guild_id: int | None,
    ) -> VerifyOutcome:
        link = await self.links.get_by_discord(discord_id)
        if link is not None and link["status"] == "approved":
            return VerifyOutcome("already_verified", link["mc_user_id"])

        queued = await self.links.queue_get(discord_id)
        if queued is not None:
            return VerifyOutcome(
                "already_queued", queued["mc_user_id"], attempts=queued["attempts"]
            )

        member = await self.lookup(display_name, mc_user_id)
        if member is not None:
            await self.links.upsert(
                discord_id, member["mc_user_id"], status="approved", reviewer_id=0
            )
            return VerifyOutcome("approved", member["mc_user_id"], member["name"])

        await self.links.queue_add(
            discord_id, mc_user_id=mc_user_id,
            display_name=display_name, guild_id=guild_id,
        )
        return VerifyOutcome("queued", mc_user_id)

    async def approve_manual(
        self, discord_id: int, mc_user_id: int, reviewer_id: int
    ) -> None:
        await self.links.upsert(
            discord_id, mc_user_id, status="approved", reviewer_id=reviewer_id
        )
        await self.links.queue_remove(discord_id)

    # -- background queue ---------------------------------------------------

    async def process_queue(
        self, display_names: dict[int, str | None]
    ) -> list[VerifyOutcome | tuple]:
        """One pass over the retry queue.

        ``display_names`` maps the queued Discord ids to their CURRENT
        server nickname (None = no longer in the guild). Returns a list of
        ``(discord_id, outcome, mc_user_id)`` tuples with outcome one of
        ``approved`` / ``expired`` / ``gone``.
        """
        results: list[tuple] = []
        for row in await self.links.queue_all():
            discord_id = row["discord_id"]
            name = display_names.get(discord_id)
            if discord_id not in display_names or name is None:
                await self.links.queue_remove(discord_id)
                results.append((discord_id, "gone", None))
                continue
            member = await self.lookup(name, row["mc_user_id"])
            if member is not None:
                await self.links.upsert(
                    discord_id, member["mc_user_id"], status="approved", reviewer_id=0
                )
                await self.links.queue_remove(discord_id)
                results.append((discord_id, "approved", member["mc_user_id"]))
                continue
            if row["attempts"] + 1 >= QUEUE_MAX_ATTEMPTS:
                await self.links.queue_remove(discord_id)
                results.append((discord_id, "expired", None))
            else:
                await self.links.queue_bump(discord_id)
        return results

    # -- alliance-leave prune -------------------------------------------------

    async def prune_candidates(self) -> list[tuple[int, int]]:
        """Approved links whose MC member is no longer active in the
        alliance → the verified role must go. Empty when the roster looks
        unhealthy (too few active members = probably a broken scrape)."""
        active = await self.members.active_members()
        if len(active) < MIN_SAFE_ROSTER_COUNT:
            log.warning(
                "membersync: prune skipped — only %d active members in the "
                "roster (safety floor %d); scrape problem?",
                len(active), MIN_SAFE_ROSTER_COUNT,
            )
            return []
        return [
            (link["discord_id"], link["mc_user_id"])
            for link in await self.links.all_approved()
            if link["mc_user_id"] not in active
        ]
