"""Unified mission/event scheduler.

One engine starts everything a member (or the admin rotation) can ask for:

* an alliance **event** or a **large** scale alliance mission,
* a **preset** mission, a member-supplied **custom** Own mission, or one
  picked from MissionChief's **saved** missions dropdown,
* **once** (a queue item) or **recurring** (promoted to the rotation list).

Two intakes feed the queue: the Discord panel/slash command and a structured
board post. A separate admin-managed **rotation list** (locations the bot
cycles forever, one per free slot) fills the gaps whenever no member request
is pending — member-first priority. The scheduler also exposes which mission
is up next and where, for the eventpinger.

Safety, unchanged from before: the scheduler only runs when
``automation.mission.enabled`` is set, board parsing only when
``board_enabled`` is set, a real start only happens when ``automation.dry_run``
is off, and a hard free-only guard means it can never spend coins. In dry-run
a mission is marked ``skipped`` with what *would* have been started.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass

import aiosqlite

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import (
    BoardDeletionRepo,
    MembersRepo,
    MissionsRepo,
    RotationRepo,
    RunsRepo,
    StateRepo,
)
from ..geo.geocoder import GeocodeError, Geocoder
from ..geo.maps_links import find_maps_links
from ..mc.board import (
    REPLY_MARKER,
    BoardClient,
    ensure_guide_post,
    guide_now,
    guide_updated_line,
)
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.events import (
    EVENT_KINDS,
    EVENT_TYPES,
    build_alliance_event_payload,
    build_event_payload,
    is_free_submit,
    next_free_at,
    parse_event_form,
    standard_event_ids,
)
from ..mc.parsers.missions_custom import (
    CustomMission,
    build_custom_mission_payload,
    find_saved_mission,
)
from ..mc.parsers.mission_spec import (
    PRESET_TYPE_IDS,
    MissionSpec,
    MissionSpecError,
    parse_board_request,
)
from .board_cleanup import deletion_due_at

log = logging.getLogger(__name__)

# A board request is done with the board once it reaches one of these.
_TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})

# Give up on a mission after this many transient-error retries (condition
# waits for the cooldown don't count).
MAX_ATTEMPTS = 12


@dataclass
class StartOutcome:
    """The result of one start attempt, source-agnostic so the member queue
    and the rotation can share the engine."""

    state: str  # started | waiting | refused | form_error | http_error | unverified | dry_run | not_found
    detail: str
    eligible_at: str | None = None
    verified: bool | None = None
    http_status: int | None = None


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
        self.rotation = RotationRepo(db)
        self.members = MembersRepo(db)
        self.runs = RunsRepo(db)
        self.state = StateRepo(db)
        self.deletions = BoardDeletionRepo(db)
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
        spec: MissionSpec,
        *,
        requester_name: str | None,
        requester_mc_id: int | None,
        discord_user_id: int | None,
        channel_id: int | None,
    ) -> int:
        """Create a Discord-sourced request. Returns the queue id.

        Recurring requests are queued too (recurring=1); the scheduler
        promotes them to the rotation list once they first start.
        """
        return await self.missions.create(
            source="discord",
            kind=spec.kind,
            mission_source=spec.source,
            preset_type_id=spec.preset_type_id,
            caption=spec.custom.caption if spec.custom else spec.saved_name,
            custom_values=_values_json(spec),
            saved_name=spec.saved_name,
            recurring=1 if spec.recurring else 0,
            event_type_id=spec.event_type_id,
            event_random=1 if spec.event_random else 0,
            area=spec.area,
            shape=spec.shape,
            call_volume=spec.call_volume,
            location_text=spec.location_text,
            requester_name=requester_name,
            requester_mc_id=requester_mc_id,
            discord_user_id=discord_user_id,
            channel_id=channel_id,
        )

    async def next_up(self) -> dict | None:
        """What the scheduler would start next, and where — for the
        eventpinger. Member requests come first (priority A), then the
        rotation. Returns None when nothing is queued or rotating."""
        pending = await self.missions.next_pending()
        if pending is not None:
            return {
                "origin": "request",
                "id": pending["id"],
                "kind": pending["kind"],
                "mission_source": pending["mission_source"],
                "location": pending["address"] or pending["location_text"],
                "requester": pending["requester_name"],
                "caption": pending["caption"],
            }
        entry = await self.rotation.next_entry()
        if entry is not None:
            return {
                "origin": "rotation",
                "id": entry["id"],
                "kind": entry["kind"],
                "mission_source": entry["mission_source"],
                "location": entry["address"] or entry["location_text"],
                "requester": None,
                "caption": entry["caption"],
            }
        return None

    async def poll(self) -> None:
        """Scan the request board(s), then advance the queue/rotation.

        A broken board must never starve the queue: each board scan is
        isolated, and the queue/rotation advance ALWAYS runs — otherwise a
        single unreachable thread would leave Discord-sourced requests
        pending forever."""
        run_id = await self.runs.start("missions")
        try:
            scanned = 0
            scan_errors: list[str] = []
            for thread_id, default_kind in self._request_boards():
                try:
                    scanned += await self._scan_board(thread_id, default_kind)
                except MissionChiefError as exc:
                    scan_errors.append(f"thread {thread_id}: {exc}")
                    log.warning(
                        "mission: board scan of thread %s failed (%s) — "
                        "still advancing the queue", thread_id, exc,
                    )
            executed = await self._advance()
            await self.runs.finish(
                run_id,
                status="success" if not scan_errors else "partial",
                pages=1, rows_parsed=scanned, rows_new=executed,
                message="; ".join(scan_errors) or None,
            )
        except MissionChiefError as exc:
            await self.runs.finish(run_id, status="failed", message=str(exc))
            raise

    # -- board scanning --------------------------------------------------

    def _request_boards(self) -> list[tuple[int, str]]:
        """The dedicated request boards to scan, each with its default kind.

        The 'events' board starts alliance EVENTS; the 'mission' board starts
        LARGE scale missions. Deduped by thread id (first wins) so a shared
        thread is never scanned twice."""
        boards: list[tuple[int, str]] = []
        events = self.cfg.automation.events
        mission = self.cfg.automation.mission
        if events.enabled and events.thread_id:
            boards.append((int(events.thread_id), "event"))
        if mission.board_enabled and mission.thread_id:
            boards.append((int(mission.thread_id), "large"))
        seen: set[int] = set()
        deduped: list[tuple[int, str]] = []
        for thread_id, kind in boards:
            if thread_id in seen:
                continue
            seen.add(thread_id)
            deduped.append((thread_id, kind))
        return deduped

    def _cursor_key(self, thread_id: int) -> str:
        return f"mission_board_last_post:{thread_id}"

    def _guide_id_key(self, thread_id: int) -> str:
        return f"mission_board_guide_id:{thread_id}"

    def _guide_hash_key(self, thread_id: int) -> str:
        return f"mission_board_guide_hash:{thread_id}"

    def _guide_refreshed_key(self, thread_id: int) -> str:
        return f"mission_board_guide_refreshed:{thread_id}"

    async def _ensure_guide(self, thread_id: int, default_kind: str) -> None:
        """Keep exactly one how-to-request guide post on the board: find our
        existing one and EDIT it in place, else create it — never duplicate.

        This is an informational forum post (not a game action), so it is
        maintained even in dry-run — only gated by ``reply_to_board``. The
        board is only re-written when the instructions change (or hourly, to
        freshen the "last updated" line)."""
        if not self.cfg.automation.reply_to_board:
            return
        static = _board_guide(default_kind, self._auto.min_contribution_rate)
        signature = hashlib.sha1(static.encode("utf-8")).hexdigest()[:12]
        now = guide_now()
        desired = f"{static}\n\n{guide_updated_line(now)}"
        try:
            await ensure_guide_post(
                self.board, self.state, thread_id,
                id_key=self._guide_id_key(thread_id),
                hash_key=self._guide_hash_key(thread_id),
                refreshed_key=self._guide_refreshed_key(thread_id),
                marker=_guide_marker(default_kind),
                desired=desired, signature=signature, now_epoch=now,
            )
        except MissionChiefError as exc:
            log.warning("mission: could not maintain guide on %s: %s", thread_id, exc)

    async def force_guide(
        self, thread_id: int, default_kind: str, *, repost: bool = False
    ) -> str:
        """Sync one request board's guide RIGHT NOW (bypassing the hourly
        throttle) and report what happened — for ``!fra guides``. ``repost``
        deletes the existing guide and creates a fresh one at the bottom of
        the thread, where members actually see it."""
        label = f"{default_kind} (thread {thread_id})"
        if not self.cfg.automation.reply_to_board:
            return f"➖ {label}: reply_to_board is off"
        try:
            if repost:
                stored = await self.state.get(self._guide_id_key(thread_id))
                target = int(stored) if stored else await self.board.find_bot_post(
                    thread_id, _guide_marker(default_kind)
                )
                if target:
                    await self.board.delete_post(thread_id, int(target))
                await self.state.delete(self._guide_id_key(thread_id))
            await self.state.delete(self._guide_hash_key(thread_id))
            await self.state.delete(self._guide_refreshed_key(thread_id))
            await self._ensure_guide(thread_id, default_kind)
        except MissionChiefError as exc:
            return f"❌ {label}: {exc}"
        post_id = await self.state.get(self._guide_id_key(thread_id))
        if post_id:
            url = self.client.url(f"/alliance_threads/{thread_id}")
            return f"✅ {label}: guide is post #{post_id} — {url}"
        reason = getattr(self.board, "last_error", None) or "see the log"
        return f"❌ {label}: could not create or edit the guide — {reason}"

    async def _scan_board(self, thread_id: int, default_kind: str) -> int:
        await self._ensure_guide(thread_id, default_kind)

        raw = await self.state.get(self._cursor_key(thread_id))
        last_seen = int(raw) if raw else None
        baseline = last_seen is None
        page, fresh = await self.board.fetch_new_posts(thread_id, last_seen)

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
                spec = parse_board_request(post.content, default_kind=default_kind)
            except MissionSpecError as exc:
                # A clear request with an unusable field — ask the poster to fix it.
                log.info("mission: post %s needs clarification (%s)", post.post_id, exc)
                await self._reply_to(
                    thread_id,
                    f"@{post.author_name or 'there'}: I couldn't use that request — "
                    f"{exc}. Post a location (e.g. \"New York City\"); see the pinned "
                    "how-to for the options.",
                )
                continue
            except Exception:
                log.exception("mission: error parsing post %s", post.post_id)
                continue
            if spec is None:
                continue
            new_id = await self.missions.create_from_board(
                thread_id, post.post_id,
                {
                    "kind": spec.kind,
                    "mission_source": spec.source,
                    "preset_type_id": spec.preset_type_id,
                    "caption": spec.custom.caption if spec.custom else spec.saved_name,
                    "custom_values": _values_json(spec),
                    "saved_name": spec.saved_name,
                    "recurring": spec.recurring,
                    "event_type_id": spec.event_type_id,
                    "event_random": spec.event_random,
                    "area": spec.area,
                    "shape": spec.shape,
                    "call_volume": spec.call_volume,
                    "location_text": spec.location_text,
                },
                requester_name=post.author_name,
                requester_mc_id=post.author_mc_id,
            )
            if new_id is not None:
                created += 1
                await self._reply_to(
                    thread_id,
                    f"@{post.author_name or 'there'}: got it — {spec.describe()} at "
                    f"{spec.location_text}. It'll start at the next free alliance slot.",
                )

        # Advance the cursor only after the whole page is handled; a crash
        # mid-scan re-reads, and the unique board index dedups re-enqueues.
        # On first contact this ALWAYS writes — even "0" for an empty thread —
        # so the baseline can't repeat and swallow the first real request.
        if last_seen is None or max_post > last_seen:
            await self.state.set(self._cursor_key(thread_id), str(max_post))
        if baseline:
            log.info(
                "mission: thread %s baseline set (%d posts, none enqueued)",
                thread_id, len(fresh),
            )
        return created

    async def _reply_to(self, thread_id: int, content: str) -> None:
        """Post a board reply to a specific thread (suppressed in dry-run /
        when replies are disabled)."""
        if not self.cfg.automation.reply_to_board:
            return
        if self.dry_run:
            log.info("mission board DRY-RUN reply to %s:\n%s", thread_id, content)
            return
        try:
            await self.board.post_reply(thread_id, content)
        except MissionChiefError as exc:
            log.warning("mission: board reply to %s failed: %s", thread_id, exc)

    # -- queue + rotation advance ---------------------------------------

    async def _advance(self) -> int:
        """Handle at most ONE start per poll (the free window is alliance-wide).

        Member requests are served first (priority A). Only when no member
        request is claimable does the rotation get to fill the free slot.
        """
        handled = await self._process_queue()
        if handled:
            return handled
        return await self._process_rotation()

    async def _process_queue(self) -> int:
        for mission in await self.missions.claimable():
            if mission["attempts"] >= MAX_ATTEMPTS:
                if await self.missions.claim(mission["id"]):
                    await self.missions.set_status(
                        mission["id"], "failed",
                        f"gave up after {mission['attempts']} failed attempts",
                    )
                    await self._schedule_cleanup(mission["id"])
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
            await self._schedule_cleanup(mission["id"])
            return 1
        return 0

    async def _schedule_cleanup(self, mission_id: int) -> None:
        """When a board request is done with (terminal state), queue its
        original post for the 12h board tidy-up — live mode only."""
        if self.dry_run:
            return
        mission = await self.missions.get(mission_id)
        if mission is None or mission["source"] != "board":
            return
        if mission["status"] not in _TERMINAL_STATUSES:
            return
        thread_id = mission["board_thread_id"]
        post_id = mission["board_post_id"]
        if not thread_id or not post_id:
            return
        await self.deletions.schedule(
            int(thread_id), int(post_id),
            due_at=deletion_due_at(), reason="handled mission request",
        )

    async def _execute(self, mission: aiosqlite.Row, *, announce: bool) -> None:
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
                    return
            try:
                resolved = await self._resolve(mission["location_text"] or "")
            except GeocodeError as exc:
                # Transient (network/rate-limit/5xx) → keep the request and
                # retry; permanent (bad API key, place not found) → fail with
                # the actionable message so it shows up in !fra missions.
                if getattr(exc, "transient", False) and mission["attempts"] < MAX_ATTEMPTS:
                    await self.missions.set_status(
                        mission["id"], "waiting",
                        f"geocoding failed ({exc}); will retry",
                        bump_attempts=True, announce=False,
                    )
                else:
                    await self.missions.set_status(
                        mission["id"], "failed", f"geocoding failed: {exc}",
                    )
                return
            lat, lng, address = resolved.latitude, resolved.longitude, resolved.address or ""
            await self.missions.set_status(
                mission["id"], "processing", "geocoded",
                latitude=lat, longitude=lng, address=address, announce=False,
            )

        # Hold the shared cooldown lock across the whole check-then-start so a
        # concurrent event/rotation start can't consume the same free window.
        async with self._start_lock:
            outcome = await self._perform_start(
                kind=mission["kind"],
                source=mission["mission_source"],
                preset_type_id=mission["preset_type_id"],
                caption=mission["caption"],
                custom_values=_load_values(mission["custom_values"]),
                saved_name=mission["saved_name"],
                latitude=lat, longitude=lng, address=address,
                **_event_args(mission),
            )
        await self._apply_queue_outcome(
            mission, outcome, requester, lat, lng, address, announce=announce
        )

    async def _apply_queue_outcome(
        self, mission: aiosqlite.Row, outcome: StartOutcome, requester: str,
        lat: float, lng: float, address: str, *, announce: bool,
    ) -> None:
        mid = mission["id"]
        if outcome.state == "waiting":
            await self.missions.set_status(
                mid, "waiting", outcome.detail,
                next_attempt_at=outcome.eligible_at, announce=announce,
            )
            if announce:
                await self._notify_board(mission,
                    f"@{requester}: your {mission['kind']} at "
                    f"{address or 'the location'} is queued — next free alliance "
                    f"mission at {outcome.eligible_at} UTC."
                )
            return
        if outcome.state == "form_error":
            await self.missions.set_status(
                mid, "waiting", outcome.detail, bump_attempts=True, announce=False,
            )
            return
        if outcome.state == "http_error":
            await self.missions.set_status(
                mid, "waiting", outcome.detail, bump_attempts=True, announce=False,
            )
            return
        if outcome.state == "refused":
            await self.missions.set_status(mid, "failed", outcome.detail)
            return
        if outcome.state == "not_found":
            await self.missions.set_status(mid, "failed", outcome.detail)
            await self._notify_board(mission,
                f"@{requester}: {outcome.detail}. An admin will handle it."
            )
            return
        if outcome.state == "dry_run":
            await self.missions.set_status(mid, "skipped", outcome.detail)
            await self._notify_board(mission,
                f"@{requester}: {mission['kind']} resolved to "
                f"{address or 'the location'} ({lat:.5f}, {lng:.5f}). "
                f"[dry-run — not started]"
            )
            await self._maybe_promote(mission, lat, lng, address)
            return
        if outcome.state == "unverified":
            await self.missions.set_status(mid, "failed", outcome.detail)
            await self._notify_board(mission,
                f"@{requester}: I submitted the mission but couldn't confirm it "
                "started. An admin will check."
            )
            return
        # started
        await self.missions.set_status(mid, "done", outcome.detail)
        await self._notify_board(mission,
            f"🚨 {mission['kind'].capitalize()} started for {requester} at "
            f"{address or 'the requested location'}!"
        )
        await self._maybe_promote(mission, lat, lng, address)

    async def _maybe_promote(
        self, mission: aiosqlite.Row, lat: float, lng: float, address: str
    ) -> None:
        """A recurring request joins the rotation list once it first starts."""
        if not mission["recurring"] or mission["rotation_id"]:
            return
        rid = await self.rotation.add(
            location_text=mission["location_text"] or address,
            kind=mission["kind"],
            mission_source=mission["mission_source"],
            preset_type_id=mission["preset_type_id"],
            caption=mission["caption"],
            custom_values=mission["custom_values"],
            saved_name=mission["saved_name"],
            event_type_id=mission["event_type_id"],
            event_random=mission["event_random"],
            area=mission["area"],
            shape=mission["shape"],
            call_volume=mission["call_volume"],
            latitude=lat, longitude=lng, address=address,
            created_by=mission["requester_name"] or "member",
        )
        await self.missions.link_rotation(mission["id"], rid)
        log.info("mission %s promoted to rotation entry %s", mission["id"], rid)

    # -- rotation --------------------------------------------------------

    async def _process_rotation(self) -> int:
        entry = await self.rotation.next_entry()
        if entry is None:
            return 0
        lat, lng = entry["latitude"], entry["longitude"]
        address = entry["address"] or ""
        if lat is None or lng is None:
            try:
                resolved = await self._resolve(entry["location_text"] or "")
            except GeocodeError as exc:
                await self.rotation.deactivate_with_note(
                    entry["id"], f"⚠️ geocode failed: {exc}"
                )
                log.warning(
                    "rotation entry %s deactivated — geocode failed: %s",
                    entry["id"], exc,
                )
                return 0
            lat, lng, address = resolved.latitude, resolved.longitude, resolved.address or ""
            await self.rotation.cache_location(entry["id"], lat, lng, address)

        async with self._start_lock:
            outcome = await self._perform_start(
                kind=entry["kind"],
                source=entry["mission_source"],
                preset_type_id=entry["preset_type_id"],
                caption=entry["caption"],
                custom_values=_load_values(entry["custom_values"]),
                saved_name=entry["saved_name"],
                latitude=lat, longitude=lng, address=address,
                **_event_args(entry),
            )

        if outcome.state in ("waiting", "form_error", "http_error"):
            # Free window not available / transient — retry next poll WITHOUT
            # advancing the cycle, so this entry keeps its turn.
            return 0
        if outcome.state == "not_found":
            await self.rotation.deactivate_with_note(entry["id"], outcome.detail)
            return 0
        if outcome.state == "refused":
            await self.rotation.deactivate_with_note(
                entry["id"], "paused — form would spend coins"
            )
            return 0
        # started / dry_run / unverified all consume the entry's turn.
        await self.rotation.mark_started(
            entry["id"], latitude=lat, longitude=lng, address=address
        )
        log.info(
            "rotation entry %s %s at %s (%.5f,%.5f)",
            entry["id"], outcome.state, address or "?", lat, lng,
        )
        return 1

    # -- the shared start engine ----------------------------------------

    async def _perform_start(
        self, *,
        kind: str,
        source: str,
        preset_type_id: int | None,
        caption: str | None,
        custom_values: dict[str, int],
        saved_name: str | None,
        latitude: float,
        longitude: float,
        address: str,
        event_type_id: int | None = None,
        event_random: bool = False,
        area: str = "medium",
        shape: str = "rectangle",
        call_volume: str = "45",
        allow_coins: bool = False,
        dry: bool | None = None,
    ) -> StartOutcome:
        """Load the form, honour the cooldown + free-only guard, then submit
        the right body for the request's kind/source.

        Normally NEVER spends coins. ``allow_coins`` (owner-only paid path)
        lifts the free-only guard and the cooldown wait and sets coins=1.
        ``dry`` overrides the global dry-run for this one call (the paid
        command previews unless the owner confirms), else the global switch
        applies."""
        effective_dry = self.dry_run if dry is None else dry
        new_path = EVENT_KINDS[kind]["new_path"]
        try:
            html = await self.client.fetch_page(f"{new_path}?tlat={latitude}&tlng={longitude}")
        except MissionChiefError as exc:
            return StartOutcome("form_error", f"could not load mission form ({exc}); will retry")
        form = parse_event_form(html)

        # Coins ignore the free cooldown; only the free path must wait.
        if not allow_coins:
            eligible_at = next_free_at(kind, form.last_free_at)
            if eligible_at and eligible_at > utcnow_iso():
                return StartOutcome(
                    "waiting", f"next free mission at {eligible_at}; queued",
                    eligible_at=eligible_at,
                )
        if form.action is None or form.authenticity_token is None:
            return StartOutcome("form_error", "mission form incomplete; will retry")
        # The event form always defaults coins to 1 ("Start Event (20 Coins)"),
        # so is_free_submit(form) can't gauge it — the free weekly event is
        # gated by the cooldown above and we force coins=0 in the payload. Only
        # the large form (coins default 0) is checked by this heuristic.
        if not allow_coins and kind != "event" and not is_free_submit(form):
            return StartOutcome("refused", "refusing to start: form would spend coins")

        # Resolve a "random" event to a concrete standard type now that we
        # have the live form (skips seasonal currency events like Soccer Game).
        chosen_event_id = event_type_id
        if kind == "event" and (event_random or chosen_event_id is None):
            pool = standard_event_ids(html)
            chosen_event_id = pool[self._pick_index(len(pool))] if pool else 0

        # Build the right body.
        try:
            body = self._build_body(
                form, html, kind=kind, source=source, preset_type_id=preset_type_id,
                caption=caption, custom_values=custom_values, saved_name=saved_name,
                latitude=latitude, longitude=longitude, address=address,
                event_type_id=chosen_event_id, area=area, shape=shape,
                call_volume=call_volume,
            )
        except _SavedMissionNotFound as exc:
            return StartOutcome("not_found", str(exc))
        if allow_coins:
            body = _with_coins(body)

        if effective_dry:
            what = self._describe(
                kind, source, preset_type_id, caption, custom_values, saved_name,
                event_type_id=chosen_event_id, area=area, shape=shape,
                call_volume=call_volume, event_random=event_random,
            )
            paid = "(PAID — coins) " if allow_coins else ""
            return StartOutcome(
                "dry_run",
                f"dry-run: would start {paid}{what} at {latitude:.5f},{longitude:.5f}",
            )

        free_before = form.last_free_at
        try:
            status, _, _ = await self.client.post_form(
                form.action, body,
                referer=self.client.url(new_path),
                ajax=True, csrf_token=form.authenticity_token,
                allow_redirects=False,
            )
        except MissionChiefError as exc:
            return StartOutcome("http_error", f"start request failed ({exc}); will retry")

        if status >= 400:
            return StartOutcome(
                "unverified", f"MissionChief rejected the start (HTTP {status})",
                http_status=status,
            )

        # A paid start does NOT consume the free-mission cooldown, so the
        # cooldown-advance check can't confirm it — report success on the
        # accepted POST and leave verification to the owner in-game.
        if allow_coins:
            return StartOutcome(
                "started",
                f"paid {kind} started at {latitude:.5f},{longitude:.5f} "
                f"(coins spent — verify in game)",
            )

        verified = await self._verify_started(new_path, latitude, longitude, free_before)
        if verified is False:
            return StartOutcome(
                "unverified",
                "start submitted but MissionChief shows no new mission — verify manually",
            )
        note = "" if verified else " (could not verify)"
        return StartOutcome(
            "started",
            f"{kind} started at {latitude:.5f},{longitude:.5f}{note}",
            verified=verified,
        )

    async def run_coin_mission(self, spec, *, confirm: bool) -> StartOutcome:
        """Owner-only paid start: geocode the location and start immediately
        using coins (no free-cooldown wait). Previews unless ``confirm`` is
        set. Returns the :class:`StartOutcome` for the caller to render."""
        try:
            resolved = await self._resolve(spec.location_text)
        except GeocodeError as exc:
            return StartOutcome("not_found", f"could not locate '{spec.location_text}': {exc}")
        lat, lng = resolved.latitude, resolved.longitude
        address = resolved.address or spec.location_text
        async with self._start_lock:
            outcome = await self._perform_start(
                kind=spec.kind,
                source=spec.source,
                preset_type_id=spec.preset_type_id,
                caption=spec.custom.caption if spec.custom else spec.saved_name,
                custom_values=spec.custom.values if spec.custom else {},
                saved_name=spec.saved_name,
                latitude=lat, longitude=lng, address=address,
                event_type_id=spec.event_type_id,
                event_random=spec.event_random,
                area=spec.area, shape=spec.shape, call_volume=spec.call_volume,
                allow_coins=True,
                dry=not confirm,  # preview unless the owner confirmed
            )
        outcome.detail = f"{outcome.detail} — {address}"
        return outcome

    @staticmethod
    def _pick_index(n: int) -> int:
        """Random index in [0, n) — isolated so tests can pin it."""
        import random

        return random.randrange(n) if n > 0 else 0

    def _build_body(
        self, form, html, *, kind, source, preset_type_id, caption,
        custom_values, saved_name, latitude, longitude, address,
        event_type_id=None, area="medium", shape="rectangle", call_volume="45",
    ) -> list[tuple[str, str]]:
        if kind == "event":
            tag = ""  # standard events carry no data-event-tag
            return build_alliance_event_payload(
                form, latitude=latitude, longitude=longitude, address=address,
                event_type_id=event_type_id if event_type_id is not None else 0,
                event_tag=tag, area=area, shape=shape, call_volume=call_volume,
            )
        if source == "custom":
            custom = CustomMission(caption=caption or "Custom mission", values=dict(custom_values))
            return build_custom_mission_payload(
                form, custom, latitude=latitude, longitude=longitude, address=address
            )
        if source == "saved":
            saved = find_saved_mission(html, saved_name or "")
            if saved is None:
                raise _SavedMissionNotFound(
                    f"saved mission '{saved_name}' not found in the dropdown"
                )
            return build_custom_mission_payload(
                form, saved.to_custom(), latitude=latitude, longitude=longitude,
                address=address,
            )
        # preset large
        return build_event_payload(
            form, kind="large", latitude=latitude, longitude=longitude,
            address=address, mission_type_id=preset_type_id,
        )

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
    def _describe(
        kind: str, source: str, preset_type_id: int | None,
        caption: str | None, custom_values: dict[str, int], saved_name: str | None,
        *, event_type_id: int | None = None, area: str = "medium",
        shape: str = "rectangle", call_volume: str = "45",
        event_random: bool = False,
    ) -> str:
        if kind == "event":
            name = EVENT_TYPES.get(event_type_id, "event") if event_type_id is not None else "event"
            tag = "random " if event_random else ""
            return f"{tag}alliance event '{name}' ({area}/{shape}/{call_volume}s)"
        if source == "custom":
            cm = CustomMission(caption=caption or "custom", values=dict(custom_values))
            return f"custom '{cm.caption}' ({cm.summary()})"
        if source == "saved":
            return f"saved mission '{saved_name}'"
        if preset_type_id is not None:
            return f"preset {PRESET_TYPE_IDS.get(preset_type_id, preset_type_id)}"
        return "large scale mission"

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
        """Reply on the board — only for board-sourced requests, and on the
        SAME thread the request came from (events board vs mission board).
        Discord requests are notified in Discord by the publisher, so posting
        to the board for them would just be noise."""
        if mission["source"] != "board":
            return
        thread_id = mission["board_thread_id"] or self._auto.thread_id
        await self._reply_to(int(thread_id), content)


class _SavedMissionNotFound(Exception):
    """The named saved mission wasn't present in the form's dropdown."""


def _guide_marker(default_kind: str) -> str:
    """The leading text that identifies our guide post (so it's found and
    edited, never duplicated). Matches the first line of ``_board_guide``."""
    if default_kind == "event":
        return "[FRA] 📋 How to request an ALLIANCE EVENT"
    return "[FRA] 📋 How to request a LARGE SCALE ALLIANCE MISSION"


def _board_guide(default_kind: str, min_rate: float) -> str:
    """The how-to-request post the bot maintains on a request board.

    Structured like the old bot's request guides: a titled guide with a
    maintained-automatically note, a "How to request" bullet list, optional
    settings, and copy-friendly examples on their own lines. Starts with the
    [FRA] marker so it's never re-parsed as a request. This is the STABLE
    text — the caller appends a "last updated" line."""
    if default_kind == "event":
        return "\n".join([
            _guide_marker("event"),
            "[b]Alliance Event Request Guide[/b]",
            "",
            "This post is maintained automatically by the Fire & Rescue "
            "Academy bot.",
            "",
            "[b]How to request[/b]",
            "- Post a location on its own line: a place name or a Google "
            "Maps link.",
            "- That is all you need — you get a random event at Large / "
            "Circle / 30 seconds.",
            "- One event per post. It starts at the next free alliance "
            "event slot, so it can take a while.",
            f"- If your alliance contribution is below {min_rate:g}%, the "
            "request is skipped.",
            "",
            "[b]Optional lines to fine-tune[/b]",
            "- event: Storm (or Civil Unrest, Storm Surge, Fall weather, "
            "Winter weather, Spring weather, Summer weather, Sports Event, "
            "Random)",
            "- area: small / medium / large",
            "- shape: circle / rectangle",
            "- call: 30 / 45 / 60",
            "- schedule: recurring (keeps coming back)",
            "",
            "[b]Examples[/b]",
            "New York City",
            "Amsterdam, Netherlands",
            "https://maps.app.goo.gl/xxxxx",
        ])
    return "\n".join([
        _guide_marker("large"),
        "[b]Large Scale Mission Request Guide[/b]",
        "",
        "This post is maintained automatically by the Fire & Rescue "
        "Academy bot.",
        "",
        "[b]How to request[/b]",
        "- Post a location on its own line: a place name or a Google Maps "
        "link.",
        "- That is all you need for a standard large scale mission.",
        "- One mission per post. It starts at the next free alliance slot, "
        "so it can take a while.",
        f"- If your alliance contribution is below {min_rate:g}%, the "
        "request is skipped.",
        "",
        "[b]Optional lines for your own mission[/b]",
        "- name: My mission name",
        "- custom: need_lf=25 need_elw1=6 water_needed=15000 (your own "
        "required units)",
        "- saved: <name> (start one of the game's Saved Missions by name)",
        "- schedule: recurring (keeps coming back)",
        "",
        "[b]Examples[/b]",
        "New York City",
        "Amsterdam, Netherlands",
        "https://maps.app.goo.gl/xxxxx",
    ])


def _with_coins(body: list[tuple[str, str]], *, count: int = 1) -> list[tuple[str, str]]:
    """Flip a free-mission body into a PAID one: set coins=1 (the game's
    'yes, spend coins' flag, as the start button does) and the mission count.
    Used only by the owner-gated paid path."""
    merged = dict(body)
    merged["mission_position[coins]"] = "1"
    merged["mission_position[amount]"] = str(max(1, count))
    return list(merged.items())


def _event_args(row) -> dict:
    """Pull the alliance-event knobs off a queue/rotation row for
    :meth:`_perform_start`. Missing/NULL columns fall back to the defaults."""
    def _get(key, default=None):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    return {
        "event_type_id": _get("event_type_id"),
        "event_random": bool(_get("event_random", 0)),
        "area": _get("area") or "medium",
        "shape": _get("shape") or "rectangle",
        "call_volume": _get("call_volume") or "45",
    }


def _values_json(spec: MissionSpec) -> str | None:
    if spec.source == "custom" and spec.custom is not None and spec.custom.values:
        return json.dumps(spec.custom.values)
    return None


def _load_values(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in data.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out
