"""MemberSync: link Discord members to MissionChief accounts.

Modelled on the reference bot's flow, but built on OUR OWN scraped roster
(the ``members`` table) instead of an external database. Proof of alliance
membership is the member's Discord server nickname exactly matching a
roster name (case-insensitive), or a user-supplied MC id that exists in
the roster — no token challenge, same as the reference bot.

Fresh alliance joins take a roster sweep to appear, so a roster miss is
answered with evidence instead of blind polling:

1. the **join logs** are checked — first our stored alliance logs, then a
   LIVE fetch of the newest log page. A matching "added to the alliance"
   entry carries the member's MC id, so verification completes instantly;
2. a definitive miss (logs reachable, no join by that name) means the
   nickname almost certainly doesn't match — the member is told so
   immediately instead of being parked in a queue that cannot succeed;
3. only when the logs cannot be checked (circuit breaker, network) does
   the bounded retry queue take over, with an ETA computed from the
   actual roster-sweep schedule.

An hourly prune removes the verified role once a linked member leaves the
alliance — gated on roster health so a broken scrape can never mass-derole
the server, and skipping fresh links the roster hasn't swept yet.

A link survives leaving the Discord server, so a member who comes BACK
under a different nickname used to be "already verified" (and auto-
restored) without the nickname ever being checked again. On rejoin the
nickname must match the linked MissionChief name; otherwise the link is
put on hold (``rejoin_pending_at``) and grants nothing until the member
renames and re-verifies — the same name proof as a first verification.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import aiosqlite

from ..db.database import Database
from ..db.repos import LinksRepo, MembersRepo, RunsRepo
from ..mc.parsers.logs import parse_logs_page

log = logging.getLogger(__name__)

# The retry window must straddle one FULL hourly members sweep, including
# its jitter (±15%) and the sweep's own runtime (~47 pages at 4-9s): a
# member who joins the alliance right after a sweep appears in the roster
# up to ~75 minutes later. 45 attempts at the 2-minute loop = 90 minutes.
QUEUE_MAX_ATTEMPTS = 45
MIN_SAFE_ROSTER_COUNT = 100      # prune safety gate (alliance has ~950)
# Stored join logs are trusted this far back for instant verification.
JOIN_LOG_WINDOW_HOURS = 48
# A link made from the join logs predates its roster row by up to a sweep;
# the prune must not treat that gap as "left the alliance".
PRUNE_GRACE_HOURS = 3

LOGS_PATH = "/alliance_logfiles"


@dataclass(frozen=True)
class VerifyOutcome:
    outcome: str                 # already_verified | already_queued | approved
    #                            | approved_from_logs | name_mismatch | queued
    #                            | rename_required (link on rejoin hold)
    mc_user_id: int | None = None
    mc_name: str | None = None
    attempts: int = 0
    contribution_rate: float | None = None
    roster_eta: dt.datetime | None = None
    #: rename_required: the linked MC name the nickname must match (None
    #: when the roster no longer knows the linked account).
    expected_name: str | None = None


class MemberSyncService:
    def __init__(self, db: Database, mc=None, cfg=None) -> None:
        self._db = db
        self._mc = mc          # MissionChief client for the live log check
        self._cfg = cfg        # for the roster-sweep interval (ETA)
        self.links = LinksRepo(db)
        self.members = MembersRepo(db)
        self.runs = RunsRepo(db)

    # -- roster lookup ---------------------------------------------------

    async def lookup(
        self, display_name: str | None, mc_user_id: int | None = None
    ) -> aiosqlite.Row | None:
        """An ACTIVE roster member by exact (case-insensitive) name match
        against the Discord nickname.

        The name is the ONLY self-service proof: a user-supplied MC id
        would let anyone claim any account (id + a mismatching nickname
        proves nothing). Exceptions go through the admin `!link` command.
        ``mc_user_id`` is accepted for backward compatibility but ignored.
        """
        if display_name:
            async with self._db.conn.execute(
                "SELECT * FROM members WHERE lower(name) = lower(?) AND is_active = 1",
                (display_name.strip(),),
            ) as cur:
                return await cur.fetchone()
        return None

    # -- join-log evidence (the smart path on a roster miss) ---------------

    async def find_join_log(self, display_name: str) -> aiosqlite.Row | None:
        """A recent "added to the alliance" log entry for this name, from
        the stored logs (synced every 15 minutes)."""
        since = (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=JOIN_LOG_WINDOW_HOURS)
        ).isoformat(timespec="seconds")
        async with self._db.conn.execute(
            "SELECT * FROM alliance_logs WHERE action_key = 'added_to_alliance' "
            "AND lower(affected_name) = lower(?) AND affected_mc_id IS NOT NULL "
            "AND (event_at IS NULL OR event_at >= ?) "
            "ORDER BY id DESC LIMIT 1",
            (display_name.strip(), since),
        ) as cur:
            return await cur.fetchone()

    async def live_join_lookup(
        self, display_name: str
    ) -> tuple[bool, dict | None]:
        """Fetch the NEWEST alliance-log page and look for a join by this
        name. Returns (checked, match): checked=False means the logs could
        not be read (breaker/network) and nothing can be concluded."""
        if self._mc is None:
            return False, None
        try:
            html = await self._mc.fetch_page(f"{LOGS_PATH}?page=1")
            page = parse_logs_page(html)
        except Exception as exc:  # noqa: BLE001 — any failure = inconclusive
            log.warning("membersync: live join check failed: %s", exc)
            return False, None
        if not page.has_table:
            return False, None
        wanted = display_name.strip().casefold()
        for row in page.rows:
            if (
                row.get("action_key") == "added_to_alliance"
                and str(row.get("affected_name") or "").strip().casefold() == wanted
                and row.get("affected_mc_id")
            ):
                return True, {
                    "mc_user_id": int(row["affected_mc_id"]),
                    "name": str(row.get("affected_name")),
                }
        return True, None

    def next_roster_eta(self, last_run: aiosqlite.Row | None) -> dt.datetime:
        """When the roster should next contain a fresh join: last members
        sweep + interval (with jitter headroom) + the sweep's own runtime."""
        interval_min = 60
        if self._cfg is not None:
            interval_min = int(self._cfg.sync.members_interval)
        now = dt.datetime.now(dt.timezone.utc)
        base = now
        if last_run is not None and last_run["started_at"]:
            try:
                base = dt.datetime.fromisoformat(last_run["started_at"])
                if base.tzinfo is None:
                    base = base.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                base = now
        eta = base + dt.timedelta(minutes=interval_min * 1.15 + 10)
        # Never promise the past; the next tick is at least a few min out.
        return max(eta, now + dt.timedelta(minutes=5))

    # -- member-initiated verification ------------------------------------

    async def request_verification(
        self,
        discord_id: int,
        display_name: str,
        mc_user_id: int | None,
        guild_id: int | None,
    ) -> VerifyOutcome:
        link = await self.links.get_by_discord(discord_id)
        on_hold = (
            link is not None and link["status"] == "approved"
            and link["rejoin_pending_at"] is not None
        )
        if link is not None and link["status"] == "approved" and not on_hold:
            return VerifyOutcome("already_verified", link["mc_user_id"])
        # A link on rejoin hold proves nothing by itself: the member is
        # verified exactly like a first-timer (nickname vs roster), and the
        # approval's upsert clears the hold.

        queued = await self.links.queue_get(discord_id)
        if queued is not None:
            return VerifyOutcome(
                "already_queued", queued["mc_user_id"], attempts=queued["attempts"]
            )

        member = await self.lookup(display_name)
        if member is not None:
            await self.links.upsert(
                discord_id, member["mc_user_id"], status="approved", reviewer_id=0
            )
            return VerifyOutcome(
                "approved", member["mc_user_id"], member["name"],
                contribution_rate=member["contribution_rate"],
            )

        # Roster miss: a fresh join won't be swept for up to an hour, but
        # the alliance logs know it within minutes. Stored logs first
        # (free), then one paced live fetch of the newest page.
        join = await self.find_join_log(display_name)
        if join is not None:
            await self.links.upsert(
                discord_id, int(join["affected_mc_id"]),
                status="approved", reviewer_id=0,
            )
            return VerifyOutcome(
                "approved_from_logs", int(join["affected_mc_id"]),
                str(join["affected_name"]),
            )
        checked, match = await self.live_join_lookup(display_name)
        if match is not None:
            await self.links.upsert(
                discord_id, match["mc_user_id"], status="approved", reviewer_id=0
            )
            return VerifyOutcome(
                "approved_from_logs", match["mc_user_id"], match["name"]
            )
        if on_hold and self._same_name(
            display_name, await self._linked_name(link["mc_user_id"])
        ):
            # Already renamed to the linked name (its account just isn't on
            # the active roster): the rename IS the proof — lift the hold
            # instead of asking for a rename that already happened.
            await self.links.clear_rejoin_pending(discord_id)
            return VerifyOutcome("already_verified", link["mc_user_id"])
        if on_hold:
            # An existing member who rejoined the server: the fix is a
            # rename, not waiting for a roster sweep — never queue them.
            return VerifyOutcome(
                "rename_required", link["mc_user_id"],
                expected_name=await self._linked_name(link["mc_user_id"]),
            )
        if checked:
            # The logs are readable and show no join by this name: the
            # nickname almost certainly doesn't match — say so instead of
            # parking them in a queue that cannot succeed.
            return VerifyOutcome("name_mismatch")

        # Logs unreachable: fall back to the retry queue, with an honest
        # ETA based on the actual sweep schedule.
        await self.links.queue_add(
            discord_id, mc_user_id=None,
            display_name=display_name, guild_id=guild_id,
        )
        last_run = await self.runs.last_success("members")
        return VerifyOutcome(
            "queued", mc_user_id, roster_eta=self.next_roster_eta(last_run)
        )

    # -- server rejoin ---------------------------------------------------------

    async def _linked_name(self, mc_user_id: int) -> str | None:
        row = await self.members.get(mc_user_id)
        return str(row["name"]) if row is not None and row["name"] else None

    @staticmethod
    def _same_name(display_name: str | None, expected: str | None) -> bool:
        return bool(
            expected and display_name
            and display_name.strip().casefold() == expected.casefold()
        )

    async def check_rejoin(
        self, discord_id: int, display_name: str | None
    ) -> VerifyOutcome | None:
        """A member (re)joined the Discord server. ``None`` when they have no
        approved link (nothing to guard). ``already_verified`` when their
        nickname still matches the linked MC name (the hold, if any, is
        lifted); otherwise the link goes on hold → ``rename_required``."""
        link = await self.links.get_by_discord(discord_id)
        if link is None or link["status"] != "approved":
            return None
        expected = await self._linked_name(link["mc_user_id"])
        if self._same_name(display_name, expected):
            await self.links.clear_rejoin_pending(discord_id)
            return VerifyOutcome("already_verified", link["mc_user_id"], expected)
        await self.links.mark_rejoin_pending(discord_id)
        return VerifyOutcome(
            "rename_required", link["mc_user_id"], expected_name=expected
        )

    async def resolve_rejoin_hold(
        self, discord_id: int, display_name: str | None
    ) -> VerifyOutcome | None:
        """A held member changed their nickname: approve when it now matches
        the linked name or an active roster name (roster only — no
        MissionChief traffic on a Discord event). ``None`` when there is no
        hold or still no match."""
        link = await self.links.get_by_discord(discord_id)
        if (
            link is None or link["status"] != "approved"
            or link["rejoin_pending_at"] is None
        ):
            return None
        expected = await self._linked_name(link["mc_user_id"])
        if self._same_name(display_name, expected):
            await self.links.clear_rejoin_pending(discord_id)
            return VerifyOutcome("approved", link["mc_user_id"], expected)
        member = await self.lookup(display_name)
        if member is None:
            return None
        await self.links.upsert(
            discord_id, member["mc_user_id"], status="approved", reviewer_id=0
        )
        await self.links.queue_remove(discord_id)
        return VerifyOutcome(
            "approved", member["mc_user_id"], member["name"],
            contribution_rate=member["contribution_rate"],
        )

    async def approve_manual(
        self, discord_id: int, mc_user_id: int, reviewer_id: int
    ) -> None:
        await self.links.upsert(
            discord_id, mc_user_id, status="approved", reviewer_id=reviewer_id
        )
        await self.links.queue_remove(discord_id)

    # -- backfill (existing Discord members) --------------------------------

    async def backfill_matches(
        self, display_names: dict[int, str | None]
    ) -> list[tuple[int, aiosqlite.Row]]:
        """Discord members without an approved link whose nickname matches
        an active roster member — the candidates for `!verifyall`, so
        existing members never have to run `!verify` themselves."""
        linked = {
            link["discord_id"] for link in await self.links.all_approved()
            if link["rejoin_pending_at"] is None
        }
        matches: list[tuple[int, aiosqlite.Row]] = []
        for discord_id, name in display_names.items():
            if discord_id in linked or not name:
                continue
            member = await self.lookup(name, None)
            if member is not None:
                matches.append((discord_id, member))
        return matches

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
            member = await self.lookup(name)
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
        # A link made from the join logs exists BEFORE its roster row does
        # (the sweep is hourly) — a fresh link must not read as "left".
        grace_cutoff = (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=PRUNE_GRACE_HOURS)
        ).isoformat(timespec="seconds")
        return [
            (link["discord_id"], link["mc_user_id"])
            for link in await self.links.all_approved()
            if link["mc_user_id"] not in active
            and (link["updated_at"] or "") < grace_cutoff
        ]

    async def restore_candidates(self) -> list[tuple[int, int]]:
        """Approved links whose MC member IS active in the alliance — the
        old bot's hourly auto-restore: anyone with a valid link should
        carry the verified role, so an accidentally removed role heals
        itself and a member who rejoins the alliance gets it back without
        re-verifying. The cog only acts on members missing the role."""
        active = await self.members.active_members()
        if len(active) < MIN_SAFE_ROSTER_COUNT:
            return []
        # A link on rejoin hold is skipped: restoring it is exactly the
        # leave-and-rejoin bypass of the nickname check.
        return [
            (link["discord_id"], link["mc_user_id"])
            for link in await self.links.all_approved()
            if link["mc_user_id"] in active and link["rejoin_pending_at"] is None
        ]
