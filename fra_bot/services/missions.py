"""Custom "Own mission" scheduler.

Members supply the full parameter set for a large scale alliance mission —
type, footprint and location — via the Discord panel/slash command or a
structured board post. Requests land in ``scheduled_missions`` and this
service starts them one at a time at the next FREE mission slot
(cooldown-aware), reusing the large-mission HTTP form path with a hard
free-only guard so it can never spend coins.

Safety: the scheduler only runs when ``automation.mission.enabled`` is set,
board parsing only when ``automation.mission.board_enabled`` is set, and a
real start only happens when ``automation.dry_run`` is off. In dry-run a
mission is marked ``skipped`` with what *would* have been started.
"""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import MembersRepo, MissionsRepo, RunsRepo, StateRepo
from ..geo.geocoder import GeocodeError, Geocoder
from ..geo.maps_links import find_maps_links
from ..mc.board import REPLY_MARKER, BoardClient
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.events import (
    EVENT_KINDS,
    build_custom_mission_payload,
    is_free_submit,
    next_free_at,
    parse_event_form,
)
from ..mc.parsers.mission_spec import MissionSpecError, parse_mission_spec

log = logging.getLogger(__name__)

# Custom missions are large scale alliance missions.
MISSION_KIND = "large"
# Give up on a mission after this many transient-error retries (condition
# waits for the cooldown don't count).
MAX_ATTEMPTS = 12


class MissionScheduler:
    def __init__(
        self,
        cfg: Config,
        client: MissionChiefClient,
        db: Database,
        geocoder: Geocoder,
        *,
        start_lock: asyncio.Lock | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db
        self.geocoder = geocoder
        self.missions = MissionsRepo(db)
        self.members = MembersRepo(db)
        self.runs = RunsRepo(db)
        self.state = StateRepo(db)
        self.board = BoardClient(client)
        self._auto = cfg.automation.mission
        # Shared with EventsService: both start large missions on the same
        # alliance-wide free cooldown, so their check-then-start must be
        # serialized to avoid two starts slipping through one free window.
        self._start_lock = start_lock or asyncio.Lock()

    # -- public entry points --------------------------------------------

    @property
    def thread_id(self) -> int:
        return self._auto.thread_id

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    async def enqueue_discord(
        self,
        spec,
        *,
        requester_name: str | None,
        requester_mc_id: int | None,
        discord_user_id: int | None,
        channel_id: int | None,
    ) -> int:
        """Create a Discord-sourced mission request. Returns its id."""
        return await self.missions.create(
            source="discord",
            mission_type_id=spec.mission_type_id,
            poi_type=spec.poi_type,
            size=spec.size,
            shape=spec.shape,
            amount=spec.amount,
            location_text=spec.location_text,
            requester_name=requester_name,
            requester_mc_id=requester_mc_id,
            discord_user_id=discord_user_id,
            channel_id=channel_id,
        )

    async def poll(self) -> None:
        """Scan the board (if enabled) then advance the queue."""
        run_id = await self.runs.start("missions")
        try:
            scanned = 0
            if self._auto.board_enabled:
                scanned = await self._scan_board()
            executed = await self._process_queue()
            await self.runs.finish(
                run_id, status="success", pages=1,
                rows_parsed=scanned, rows_new=executed,
            )
        except MissionChiefError as exc:
            await self.runs.finish(run_id, status="failed", message=str(exc))
            raise

    # -- board scanning --------------------------------------------------

    def _cursor_key(self) -> str:
        # Independent from the events poller's board_posts cursor so the two
        # can share thread 15293 without stepping on each other's dedup.
        return f"mission_board_last_post:{self.thread_id}"

    async def _scan_board(self) -> int:
        raw = await self.state.get(self._cursor_key())
        last_seen = int(raw) if raw else None
        baseline = last_seen is None
        page, fresh = await self.board.fetch_new_posts(self.thread_id, last_seen)

        created = 0
        max_post = last_seen or 0
        for post in sorted(fresh, key=lambda p: p.post_id):
            max_post = max(max_post, post.post_id)
            if baseline:
                continue  # first contact: set the cursor, enqueue nothing
            is_own = (
                page.current_user_id is not None
                and post.author_mc_id == page.current_user_id
            )
            if is_own or post.content.startswith(REPLY_MARKER):
                continue
            try:
                spec = parse_mission_spec(post.content)
            except MissionSpecError as exc:
                log.info("mission: post %s not enqueued (%s)", post.post_id, exc)
                continue
            except Exception:
                log.exception("mission: error parsing post %s", post.post_id)
                continue
            if spec is None:
                continue
            new_id = await self.missions.create_from_board(
                self.thread_id, post.post_id,
                {
                    "mission_type_id": spec.mission_type_id,
                    "poi_type": spec.poi_type,
                    "size": spec.size,
                    "shape": spec.shape,
                    "amount": spec.amount,
                    "location_text": spec.location_text,
                },
                requester_name=post.author_name,
                requester_mc_id=post.author_mc_id,
            )
            if new_id is not None:
                created += 1

        # Advance the cursor only after the whole page is handled; a crash
        # mid-scan re-reads, and the unique board index dedups re-enqueues.
        if max_post and (last_seen is None or max_post > last_seen):
            await self.state.set(self._cursor_key(), str(max_post))
        if baseline:
            log.info(
                "mission: thread %s baseline set (%d posts, none enqueued)",
                self.thread_id, len(fresh),
            )
        return created

    # -- queue processing ------------------------------------------------

    async def _process_queue(self) -> int:
        """Handle at most ONE mission per poll.

        The free-mission cooldown is alliance-wide, so only one mission can
        ever start per window; touching the form for every queued item would
        just burst requests. We process the first claimable mission and stop,
        letting the interval pace the rest. Maxed-out rows are retired without
        touching MissionChief, so they don't count as the poll's one mission.
        """
        for mission in await self.missions.claimable():
            if mission["attempts"] >= MAX_ATTEMPTS:
                if await self.missions.claim(mission["id"]):
                    await self.missions.set_status(
                        mission["id"], "failed",
                        f"gave up after {mission['attempts']} failed attempts",
                    )
                continue
            if not await self.missions.claim(mission["id"]):
                continue  # another poll won the claim
            first_attempt = mission["status"] == "pending"
            try:
                await self._execute(mission, announce=first_attempt)
            except MissionChiefError as exc:
                await self.missions.set_status(
                    mission["id"], "waiting",
                    f"MissionChief error ({exc}); will retry",
                    bump_attempts=True, announce=False,
                )
            except Exception:
                log.exception("mission: unexpected error on mission %s", mission["id"])
                current = await self.missions.get(mission["id"])
                if current is not None and current["status"] == "processing":
                    await self.missions.set_status(
                        mission["id"], "failed", "internal error while processing",
                    )
            return 1
        return 0

    async def _execute(self, mission: aiosqlite.Row, *, announce: bool) -> bool:
        """Drive one mission to a terminal or 'waiting' state. The returned
        bool (a start was attempted) is informational; the queue processes
        one mission per poll regardless."""
        requester = mission["requester_name"] or "member"

        lat, lng = mission["latitude"], mission["longitude"]
        address = mission["address"] or ""
        if lat is None or lng is None:
            # Board requests are self-serve: gate on contribution rate.
            if mission["source"] == "board":
                rate = await self._contribution_rate(mission["requester_mc_id"])
                if rate is not None and rate < self._auto.min_contribution_rate:
                    await self.missions.set_status(
                        mission["id"], "skipped",
                        f"contribution {rate:g}% below {self._auto.min_contribution_rate:g}%",
                    )
                    await self._notify_board(mission,
                        f"@{requester}: mission not accepted — your alliance "
                        f"contribution ({rate:g}%) is below "
                        f"{self._auto.min_contribution_rate:g}%."
                    )
                    return False
            try:
                resolved = await self._resolve(mission["location_text"] or "")
            except GeocodeError as exc:
                await self.missions.set_status(
                    mission["id"], "failed", f"geocoding failed: {exc}",
                )
                return False
            lat, lng, address = resolved.latitude, resolved.longitude, resolved.address or ""
            await self.missions.set_status(
                mission["id"], "processing", "geocoded",
                latitude=lat, longitude=lng, address=address, announce=False,
            )

        # Hold the shared cooldown lock across the whole check-then-start so a
        # concurrent event/mission start can't consume the same free window.
        async with self._start_lock:
            return await self._attempt_start(
                mission, requester, lat, lng, address, announce=announce
            )

    async def _attempt_start(
        self, mission: aiosqlite.Row, requester: str,
        lat: float, lng: float, address: str, *, announce: bool,
    ) -> bool:
        new_path = EVENT_KINDS[MISSION_KIND]["new_path"]
        try:
            form = parse_event_form(
                await self.client.fetch_page(f"{new_path}?tlat={lat}&tlng={lng}")
            )
        except MissionChiefError as exc:
            await self.missions.set_status(
                mission["id"], "waiting", f"could not load mission form ({exc}); will retry",
                bump_attempts=True, announce=False,
            )
            return False

        eligible_at = next_free_at(MISSION_KIND, form.last_free_at)
        if eligible_at and eligible_at > utcnow_iso():
            await self.missions.set_status(
                mission["id"], "waiting",
                f"next free mission at {eligible_at}; queued",
                next_attempt_at=eligible_at, announce=announce,
            )
            if announce:
                await self._notify_board(mission,
                    f"@{requester}: your mission at {address or 'the location'} is "
                    f"queued — next free alliance mission at {eligible_at} UTC."
                )
            return False

        if form.action is None or form.authenticity_token is None:
            await self.missions.set_status(
                mission["id"], "waiting", "mission form incomplete; will retry",
                bump_attempts=True, announce=False,
            )
            return False

        if not is_free_submit(form):
            await self.missions.set_status(
                mission["id"], "failed", "refusing to start: form would spend coins",
            )
            return True  # hard free-only guard — never spend coins

        if self.dry_run:
            await self.missions.set_status(
                mission["id"], "skipped",
                f"dry-run: would start {self._describe(mission)} at {lat:.5f},{lng:.5f}",
            )
            await self._notify_board(mission,
                f"@{requester}: mission resolved to {address or 'the location'} "
                f"({lat:.5f}, {lng:.5f}). [dry-run — not started]"
            )
            return True

        free_before = form.last_free_at
        body = build_custom_mission_payload(
            form,
            latitude=lat, longitude=lng, address=address,
            mission_type_id=mission["mission_type_id"],
            poi_type=mission["poi_type"], size=mission["size"],
            shape=mission["shape"], amount=mission["amount"],
        )
        try:
            status, _, _ = await self.client.post_form(
                form.action, body,
                referer=self.client.url(new_path),
                ajax=True, csrf_token=form.authenticity_token,
                allow_redirects=False,
            )
        except MissionChiefError as exc:
            await self.missions.set_status(
                mission["id"], "waiting", f"start request failed ({exc}); will retry",
                bump_attempts=True, announce=False,
            )
            return False

        if status >= 400:
            await self.missions.set_status(
                mission["id"], "failed",
                f"MissionChief rejected the start (HTTP {status})",
            )
            await self._notify_board(mission,
                f"@{requester}: I couldn't start the mission automatically "
                f"(HTTP {status}). An admin will handle it."
            )
            return True

        verified = await self._verify_started(new_path, lat, lng, free_before)
        if verified is False:
            await self.missions.set_status(
                mission["id"], "failed",
                "start submitted but MissionChief shows no new mission — verify manually",
            )
            await self._notify_board(mission,
                f"@{requester}: I submitted the mission but couldn't confirm it "
                "started. An admin will check."
            )
            return True

        note = "" if verified else " (could not verify)"
        await self.missions.set_status(
            mission["id"], "done",
            f"large scale mission started at {lat:.5f},{lng:.5f}{note}",
        )
        await self._notify_board(mission,
            f"🚨 Large scale alliance mission started for {requester} at "
            f"{address or 'the requested location'}!"
        )
        return True

    async def _verify_started(
        self, new_path: str, lat: float, lng: float, free_before: str | None
    ) -> bool | None:
        """Confirm a start via an advanced free-mission cooldown. True =
        confirmed, False = cooldown unchanged (not started), None = unknown."""
        try:
            check = parse_event_form(
                await self.client.fetch_page(f"{new_path}?tlat={lat}&tlng={lng}")
            )
        except MissionChiefError:
            return None
        free_after = check.last_free_at
        if free_after is None:
            return None
        if free_before is None:
            return True
        return free_after > free_before

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _describe(mission: aiosqlite.Row) -> str:
        mt = mission["mission_type_id"]
        prefix = f"type {mt}" if mt is not None else "default type"
        return f"{prefix} (size {mission['size']}, amount {mission['amount']})"

    async def _resolve(self, location_text: str):
        if not location_text:
            raise GeocodeError("no location given")
        if find_maps_links(location_text):
            return await self.geocoder.resolve_maps_link(location_text)
        return await self.geocoder.search(location_text)

    async def _contribution_rate(self, mc_user_id: int | None) -> float | None:
        if mc_user_id is None:
            return None
        active = await self.members.active_members()
        row = active.get(mc_user_id)
        return row["contribution_rate"] if row is not None else None

    async def _notify_board(self, mission: aiosqlite.Row, content: str) -> None:
        """Reply on the board — only for board-sourced requests. Discord
        requests are notified in Discord by the publisher, so posting to the
        board for them would just be noise. Skipped in dry-run / when
        replies are disabled."""
        if mission["source"] != "board":
            return
        if not self.cfg.automation.reply_to_board:
            return
        if self.dry_run:
            log.info("mission DRY-RUN board reply:\n%s", content)
            return
        try:
            await self.board.post_reply(self.thread_id, content)
        except MissionChiefError as exc:
            log.warning("mission: board reply failed: %s", exc)
