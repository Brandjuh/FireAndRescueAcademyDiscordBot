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
import json
import logging
from dataclasses import dataclass

import aiosqlite

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import MembersRepo, MissionsRepo, RotationRepo, RunsRepo, StateRepo
from ..geo.geocoder import GeocodeError, Geocoder
from ..geo.maps_links import find_maps_links
from ..mc.board import REPLY_MARKER, BoardClient
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.events import (
    EVENT_KINDS,
    build_event_payload,
    is_free_submit,
    next_free_at,
    parse_event_form,
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
    parse_mission_spec,
)

log = logging.getLogger(__name__)

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
        """Scan the board (if enabled), then advance the queue/rotation."""
        run_id = await self.runs.start("missions")
        try:
            scanned = 0
            if self._auto.board_enabled:
                scanned = await self._scan_board()
            executed = await self._advance()
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
        # can share the thread without stepping on each other's dedup.
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
                    "kind": spec.kind,
                    "mission_source": spec.source,
                    "preset_type_id": spec.preset_type_id,
                    "caption": spec.custom.caption if spec.custom else spec.saved_name,
                    "custom_values": _values_json(spec),
                    "saved_name": spec.saved_name,
                    "recurring": spec.recurring,
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
        if not allow_coins and not is_free_submit(form):
            return StartOutcome("refused", "refusing to start: form would spend coins")

        # Build the right body.
        try:
            body = self._build_body(
                form, html, kind=kind, source=source, preset_type_id=preset_type_id,
                caption=caption, custom_values=custom_values, saved_name=saved_name,
                latitude=latitude, longitude=longitude, address=address,
            )
        except _SavedMissionNotFound as exc:
            return StartOutcome("not_found", str(exc))
        if allow_coins:
            body = _with_coins(body)

        if effective_dry:
            what = self._describe(kind, source, preset_type_id, caption, custom_values, saved_name)
            paid = "(PAID — ~10 coins) " if allow_coins else ""
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
                allow_coins=True,
                dry=not confirm,  # preview unless the owner confirmed
            )
        outcome.detail = f"{outcome.detail} — {address}"
        return outcome

    def _build_body(
        self, form, html, *, kind, source, preset_type_id, caption,
        custom_values, saved_name, latitude, longitude, address,
    ) -> list[tuple[str, str]]:
        if kind == "event":
            return build_event_payload(
                form, kind="event", latitude=latitude, longitude=longitude,
                address=address,
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
    ) -> str:
        if kind == "event":
            return "alliance event"
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
        """Reply on the board — only for board-sourced requests. Discord
        requests are notified in Discord by the publisher, so posting to the
        board for them would just be noise. Skipped in dry-run / when replies
        are disabled."""
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


class _SavedMissionNotFound(Exception):
    """The named saved mission wasn't present in the form's dropdown."""


def _with_coins(body: list[tuple[str, str]], *, count: int = 1) -> list[tuple[str, str]]:
    """Flip a free-mission body into a PAID one: set coins=1 (the game's
    'yes, spend coins' flag, as the start button does) and the mission count.
    Used only by the owner-gated paid path."""
    merged = dict(body)
    merged["mission_position[coins]"] = "1"
    merged["mission_position[amount]"] = str(max(1, count))
    return list(merged.items())


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
