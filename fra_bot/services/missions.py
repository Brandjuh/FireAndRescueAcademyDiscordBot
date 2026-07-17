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
import datetime
import hashlib
import json
import logging
import random
from dataclasses import dataclass

import aiosqlite

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import (
    BoardDeletionRepo,
    EventPingsRepo,
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
from ..mc.parsers.logs import parse_logs_page
from ..mc.parsers.events import (
    EVENT_KINDS,
    EVENT_TYPES,
    parse_large_mission_types,
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
    parse_saved_missions,
)
from ..mc.parsers.mission_spec import (
    PRESET_TYPE_IDS,
    MissionSpec,
    MissionSpecError,
    parse_board_request,
)
from .board_cleanup import deletion_due_at, schedule_reply_cleanup

log = logging.getLogger(__name__)

# A board request is done with the board once it reaches one of these.
_TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})

#: State key caching the game's Saved Missions dropdown for the Discord
#: chooser: {"names": [...], "at": epoch}. Filled opportunistically on
#: every large-mission form fetch and by the periodic refresh job.
SAVED_MISSIONS_STATE_KEY = "saved_missions_list"

# Give up on a mission after this many transient-error retries (condition
# waits for the cooldown don't count).
MAX_ATTEMPTS = 12

# Cooldown waits are re-verified against the live form at least this often:
# a computed eligible_at can be skewed (timezone fallback, a stale last-free
# line), and trusting it blindly leaves a free slot unused for hours.
MAX_WAIT_MINUTES = 30
# Waiting rechecks that start nothing don't consume the poll's one start,
# but one poll must not walk every queued form either.
_MAX_RECHECKS_PER_POLL = 5

# Member-facing names for the two request boards.
_KIND_LABELS = {
    "large": "Large Scale Alliance Mission",
    "event": "Alliance Event",
}

# The maintained "what is on the schedule" post (the reference bot kept an
# equivalent [EM-GUIDE:locations] post on its events board).
SCHEDULE_MARKER = "[FRA] 📅 Scheduled locations"


def _window_ladder(attempts: int) -> str:
    """Recheck time when the timestamps said the free window should be
    open but the FREE start button is not on the form yet (the game lags,
    or the previous mission is still winding down). The button is the
    reference of truth, so recheck on a short ladder — 5 minutes first,
    then 30 — and start the PLANNED item the moment it appears. Never
    fail it, never let something else jump the list."""
    minutes = 5 if attempts <= 0 else 30
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


def _transient_backoff(attempts: int) -> str:
    """Retry time for transient failures (unreadable form, HTTP error,
    geocode hiccup): grows with the attempt count, capped at the re-verify
    horizon. Without this the row is due again the very next poll — an
    hour of game maintenance would burn through MAX_ATTEMPTS and retire a
    request that was merely waiting out its cooldown."""
    minutes = min(5 * (attempts + 1), MAX_WAIT_MINUTES)
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


# MissionChief silently refuses a start while another alliance mission/event
# is still running (the POST is accepted but no mission appears). The bot
# knows what it started, so after any confirmed start — or any such refusal —
# all free starts back off this long instead of hammering doomed attempts.
START_BACKOFF_MINUTES = 60
# A queued request that keeps being refused eventually needs a human.
REFUSED_GIVE_UP_HOURS = 48
# State key for the alliance-busy backoff window.
STATE_START_BACKOFF = "alliance_start_backoff_until"

# Verifying a free start: MissionChief can reflect the advanced free-mission
# cooldown a MOMENT after the start, so a first "unchanged" reading may be a
# false negative — which would make the rotation re-fire the same mission the
# next window. Re-check a few times with a short pause before concluding, and
# if the cooldown stays unreadable fall back to the alliance log's "mission
# started" line (an independent record). A refused start arms a 60-min backoff,
# so this extra work happens at most about once an hour.
VERIFY_RETRY_ATTEMPTS = 3
VERIFY_RETRY_DELAY_SECONDS = 3.0
# A "mission started" log line this fresh confirms our just-submitted start.
VERIFY_LOG_WINDOW_MINUTES = 15
ALLIANCE_LOG_PATH = "/alliance_logfiles"


def start_backoff_iso(minutes: int = START_BACKOFF_MINUTES) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


def _iso_minutes_ago(minutes: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")


def older_than_hours(created_iso: str | None, hours: int) -> bool:
    if not created_iso:
        return False
    try:
        created = datetime.datetime.fromisoformat(created_iso)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - created > datetime.timedelta(hours=hours)


def _capped_wait(eligible_at: str | None) -> str:
    """Never park a cooldown wait further out than MAX_WAIT_MINUTES: the
    availability is re-verified against the live form on the next due poll,
    so a skewed eligible_at can cost at most half an hour.

    Always returns UTC — claimable() compares next_attempt_at to
    utcnow_iso() as strings, so a non-UTC offset (the form's timezone)
    would corrupt both the cap comparison and the due check."""
    cap = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=MAX_WAIT_MINUTES
    )
    when = cap
    if eligible_at:
        try:
            parsed = datetime.datetime.fromisoformat(eligible_at)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            when = min(parsed, cap)
    return when.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def _event_error_reply(requester: str | None, reason: str) -> str:
    """The reference bot's 'could not be processed' event reply, verbatim
    structure."""
    return (
        f"Event request could not be processed for {requester or 'member'}.\n\n"
        f"[b]Reason[/b]: {reason}\n\n"
        "[b]Post one clear location, for example[/b]\n"
        "Kansas City, Kansas\n"
        "Amsterdam, Netherlands"
    )


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
        self.event_pings = EventPingsRepo(db)
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
        status: str = "pending",
        status_detail: str | None = None,
    ) -> int:
        """Create a Discord-sourced request. Returns the queue id.

        Recurring requests are queued too (recurring=1); the scheduler
        promotes them to the rotation list once they first start. A
        non-default ``status`` records a request that was refused at intake
        (the log entry) — written with the insert itself, so the scheduler
        can never claim it in between.
        """
        extra: dict = {}
        if status != "pending":
            extra = {"status": status, "status_detail": status_detail}
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
            **extra,
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

    # -- the game's Saved Missions list (for the Discord chooser) --------

    async def _cache_saved_missions(self, html: str) -> None:
        """Cache the Saved Missions dropdown captions from a large-mission
        form page. Best effort; an empty parse never clobbers a known list
        (a glitchy page is indistinguishable from a truly empty dropdown)."""
        try:
            names = [s.caption for s in parse_saved_missions(html)]
        except Exception:  # noqa: BLE001 — the cache must never break a start
            return
        if not names:
            return
        await self.state.set(SAVED_MISSIONS_STATE_KEY, json.dumps({
            "names": names[:50],
            "at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }))

    async def saved_mission_names(self) -> list[str]:
        """The cached Saved Missions captions (may lag the game a little)."""
        raw = await self.state.get(SAVED_MISSIONS_STATE_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        names = data.get("names") if isinstance(data, dict) else None
        return [str(n) for n in names] if isinstance(names, list) else []

    async def refresh_saved_missions(self) -> list[str]:
        """Fetch the large-mission form and refresh the Saved Missions
        cache — the periodic job behind the Discord chooser's list. Runs as
        bulk traffic (pure maintenance); on failure the old cache stands."""
        from ..core.pacing import bulk_traffic

        try:
            with bulk_traffic():
                html = await self.client.fetch_page(
                    EVENT_KINDS["large"]["new_path"]
                )
        except MissionChiefError as exc:
            log.warning("saved-missions refresh failed: %s", exc)
            return await self.saved_mission_names()
        html = await self._saved_missions_html(html)
        await self._cache_saved_missions(html)
        return await self.saved_mission_names()

    def _playwright_cookies(self) -> list[dict]:
        return [
            {
                "name": cookie.key,
                "value": cookie.value,
                "url": self.cfg.missionchief.base_url,
            }
            for cookie in self.client.session.cookie_jar
        ]

    async def _saved_missions_html(self, plain_html: str) -> str:
        """HTML that actually carries the Saved Missions anchors. When the
        plain form has none, the block may be drawn by JavaScript — retry
        once with a browser-rendered fetch (only if Playwright is there).
        Falls back to the plain HTML on any failure."""
        if parse_saved_missions(plain_html):
            return plain_html
        from ..mc.browser_builder import BrowserBuilder, render_page

        if not BrowserBuilder.available():
            return plain_html
        try:
            rendered = await render_page(
                self.cfg.missionchief.base_url,
                self._playwright_cookies(),
                EVENT_KINDS["large"]["new_path"],
            )
        except Exception as exc:  # noqa: BLE001 — a fallback must not raise
            log.warning("rendered saved-missions fetch failed: %s", exc)
            return plain_html
        if parse_saved_missions(rendered):
            log.info("saved missions found via the rendered form (JS-drawn)")
            return rendered
        return plain_html

    async def poll(self) -> None:
        """Scan the request board(s), then advance the queue/rotation.

        A broken board must never starve the queue: each board scan is
        isolated, and the queue/rotation advance ALWAYS runs — otherwise a
        single unreachable thread would leave Discord-sourced requests
        pending forever.

        The switches are read LIVE each pass (the job is always registered,
        see bot.py) so `!fra set` enables the pipeline without a restart."""
        auto = self.cfg.automation
        if not (
            auto.mission.enabled
            or auto.mission.board_enabled
            or auto.events.enabled
        ):
            return
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
        await self._ensure_schedule_post(thread_id, default_kind, now)

    # -- the maintained schedule post (reference bot's locations post) ------

    def _sched_id_key(self, thread_id: int) -> str:
        return f"mission_board_sched_id:{thread_id}"

    def _sched_hash_key(self, thread_id: int) -> str:
        return f"mission_board_sched_hash:{thread_id}"

    def _sched_refreshed_key(self, thread_id: int) -> str:
        return f"mission_board_sched_refreshed:{thread_id}"

    async def _schedule_body(self, default_kind: str) -> str:
        """The 'what is on the schedule' body: the recurring rotation of
        this board's kind plus the queued member requests, oldest first."""
        label = _KIND_LABELS.get(default_kind, default_kind)
        lines = [
            SCHEDULE_MARKER,
            f"[b]Current {label} schedule[/b]",
            "",
            "[b]In rotation (recurring)[/b]",
        ]
        rotation = [
            entry for entry in await self.rotation.list_all()
            if (entry["kind"] or "large") == default_kind
        ]
        if rotation:
            for entry in rotation:
                where = entry["address"] or entry["location_text"]
                last = (entry["last_started_at"] or "")[:10] or "never"
                state = "" if entry["active"] else " — paused"
                lines.append(f"- {where} (last started: {last}){state}")
        else:
            lines.append("- none yet")
        lines.append("")
        lines.append("[b]Waiting in the queue[/b]")
        queued = [
        row for row in await self.missions.open_for_kind(default_kind)
            if not row["rotation_id"]      # recurring ones show under rotation
        ]
        if queued:
            for row in queued:
                lines.append(f"- {row['address'] or row['location_text'] or '?'}")
        else:
            lines.append("- empty — post a location to add one")
        lines.append("")
        lines.append("Locations start at the next free alliance mission slot; "
                     "see the how-to post above to add one.")
        return "\n".join(lines)

    async def _ensure_schedule_post(
        self, thread_id: int, default_kind: str, now: float
    ) -> None:
        try:
            body = await self._schedule_body(default_kind)
        except Exception:  # noqa: BLE001 - schedule post must not break the scan
            log.exception("mission: could not build schedule body for %s", thread_id)
            return
        signature = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        desired = f"{body}\n\n{guide_updated_line(now)}"
        try:
            await ensure_guide_post(
                self.board, self.state, thread_id,
                id_key=self._sched_id_key(thread_id),
                hash_key=self._sched_hash_key(thread_id),
                refreshed_key=self._sched_refreshed_key(thread_id),
                marker=SCHEDULE_MARKER,
                desired=desired, signature=signature, now_epoch=now,
            )
        except MissionChiefError as exc:
            log.warning(
                "mission: could not maintain schedule post on %s: %s",
                thread_id, exc,
            )

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
                for id_key, marker in (
                    (self._guide_id_key(thread_id), _guide_marker(default_kind)),
                    (self._sched_id_key(thread_id), SCHEDULE_MARKER),
                ):
                    stored = await self.state.get(id_key)
                    target = int(stored) if stored else await self.board.find_bot_post(
                        thread_id, marker
                    )
                    if target:
                        await self.board.delete_post(thread_id, int(target))
                    await self.state.delete(id_key)
            for key in (
                self._guide_hash_key(thread_id),
                self._guide_refreshed_key(thread_id),
                self._sched_hash_key(thread_id),
                self._sched_refreshed_key(thread_id),
            ):
                await self.state.delete(key)
            await self._ensure_guide(thread_id, default_kind)
        except MissionChiefError as exc:
            return f"❌ {label}: {exc}"
        post_id = await self.state.get(self._guide_id_key(thread_id))
        sched_id = await self.state.get(self._sched_id_key(thread_id))
        if post_id:
            url = self.client.url(f"/alliance_threads/{thread_id}")
            sched_note = (
                f", schedule #{sched_id}" if sched_id
                else ", schedule ❌ (see the log)"
            )
            return f"✅ {label}: guide is post #{post_id}{sched_note} — {url}"
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
                    _event_error_reply(post.author_name, str(exc)),
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
                    f"Event request received for {post.author_name or 'member'}.\n\n"
                    f"[b]Location[/b]: {spec.location_text}\n"
                    f"[b]Type[/b]: {spec.describe()}\n\n"
                    "It will start at the next free alliance mission slot.",
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
        """Post a board reply to a specific thread (when ``reply_to_board``
        is on).

        Replies post in dry-run too: they're informational posts on OUR
        request topics, not game actions — members need the feedback
        regardless of whether real starts are enabled. Action messages
        carry their own [dry-run] markers."""
        if not self.cfg.automation.reply_to_board:
            return
        try:
            posted = await self.board.post_reply(thread_id, content)
        except MissionChiefError as exc:
            log.warning("mission: board reply to %s failed: %s", thread_id, exc)
            return
        if posted:
            # Our own notice gets the same 12h tidy-up as the request post.
            await schedule_reply_cleanup(
                self.board, self.deletions, thread_id, content,
                kind="mission", dry_run=self.dry_run,
            )

    # -- queue + rotation advance ---------------------------------------

    async def _advance(self) -> int:
        """Handle at most ONE start per poll (the free window is alliance-wide).

        Member requests are served first (priority A). Only when no member
        request is claimable does the rotation get to fill the free slot.
        """
        await self._promote_pending_recurring()
        # Self-healing: rows parked beyond the re-verify horizon (older code
        # trusted the computed eligible_at outright) are pulled back in.
        reverified = await self.missions.reverify_waiting(_capped_wait(None))
        if reverified:
            log.info("missions: %d parked wait(s) pulled within %d min for "
                     "re-verification", reverified, MAX_WAIT_MINUTES)
        handled = await self._process_queue()
        if handled > 0:
            return handled
        if handled < 0:
            # Recheck budget spent with member requests still unexamined —
            # the rotation must not jump the queue for the free window.
            return 0
        return await self._process_rotation()

    async def _promote_pending_recurring(self) -> None:
        """Recurring requests join the rotation AT INTAKE (not only after
        their first start): with a busy queue they would otherwise sit
        invisible for hours while the member asked for a recurring spot.
        The queue item stays for the prompt first start; rotation_id links
        the two so nothing promotes twice."""
        for mission in await self.missions.open_recurring_unpromoted():
            try:
                await self._maybe_promote(
                    mission, mission["latitude"], mission["longitude"],
                    mission["address"] or "",
                )
            except Exception:  # noqa: BLE001 - never block the queue on this
                log.exception("mission %s: rotation promotion failed", mission["id"])

    async def _process_queue(self) -> int:
        rechecks = 0
        blocked_kinds: set[str] = set()
        # Strict list order per kind: only the OLDEST open request of a
        # kind may take that kind's window. A younger sibling used to be
        # able to start while the head sat parked waiting out its recheck
        # — jumping the list and scrambling the schedule.
        heads: dict[str, int] = {}
        for kind in _KIND_LABELS:
            head = await self.missions.open_for_kind(kind, limit=1)
            if head:
                heads[kind] = head[0]["id"]
        for mission in await self.missions.claimable():
            if mission["attempts"] >= MAX_ATTEMPTS:
                if await self.missions.claim(mission["id"]):
                    await self.missions.set_status(
                        mission["id"], "failed",
                        f"gave up after {mission['attempts']} failed attempts",
                    )
                    await self._schedule_cleanup(mission["id"])
                continue
            if heads.get(mission["kind"]) != mission["id"]:
                continue  # not this kind's head: keep the list order
            # The cooldown is per KIND and alliance-wide: once one event
            # answered "window closed", checking the other queued events is
            # pointless this poll — skip straight to the large items (and
            # vice versa), so neither kind can starve the other.
            if mission["kind"] in blocked_kinds:
                continue
            if not await self.missions.claim(mission["id"]):
                continue  # another poll won the claim
            first_attempt = mission["status"] == "pending"
            state: str | None = None
            try:
                state = await self._execute(mission, announce=first_attempt)
            except MissionChiefError as exc:
                await self.missions.set_status(
                    mission["id"], "waiting",
                    f"MissionChief error ({exc}); will retry",
                    next_attempt_at=_transient_backoff(mission["attempts"]),
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
            # A recheck that ended in 'waiting' started NOTHING — it must
            # not consume this poll's one start: a queue full of waiting
            # events would otherwise starve a large mission whose separate
            # cooldown is free right now. Cooldown answers block the whole
            # kind (shared window); transient retries (geocode/HTTP/form)
            # are bounded so one poll can't walk the whole queue.
            current = await self.missions.get(mission["id"])
            if current is not None and current["status"] == "waiting":
                if state == "waiting":  # the kind's shared cooldown is closed
                    blocked_kinds.add(mission["kind"])
                    continue
                rechecks += 1
                if rechecks >= _MAX_RECHECKS_PER_POLL:
                    # Budget spent with items possibly unexamined: -1 tells
                    # _advance the rotation may NOT take the free window —
                    # member requests keep priority.
                    return -1
                continue
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

    async def _execute(self, mission: aiosqlite.Row, *, announce: bool) -> str | None:
        """Run one queued mission attempt. Returns the start engine's
        outcome state ('waiting' = the kind's shared cooldown is closed),
        or None when the attempt never reached the mission form."""
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
                    await self._notify_board(mission, _event_error_reply(
                        requester,
                        f"Latest alliance donation is {rate:.1f}%, below the "
                        f"required {self._auto.min_contribution_rate:.1f}%.",
                    ))
                    return None
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
                        next_attempt_at=_transient_backoff(mission["attempts"]),
                        bump_attempts=True, announce=False,
                    )
                else:
                    await self.missions.set_status(
                        mission["id"], "failed", f"geocoding failed: {exc}",
                    )
                    await self._notify_board(mission, _event_error_reply(
                        requester,
                        "Location could not be resolved to GPS coordinates. "
                        f"({exc})",
                    ))
                return None
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
        return outcome.state

    async def _apply_queue_outcome(
        self, mission: aiosqlite.Row, outcome: StartOutcome, requester: str,
        lat: float, lng: float, address: str, *, announce: bool,
    ) -> None:
        mid = mission["id"]
        kind_label = _KIND_LABELS.get(mission["kind"], mission["kind"])
        if outcome.state == "waiting":
            # A healthy cooldown answer also clears the attempt counter:
            # attempts measure consecutive FAILURES, and this recheck just
            # succeeded — the request is merely waiting out the window.
            await self.missions.set_status(
                mid, "waiting", outcome.detail,
                next_attempt_at=_capped_wait(outcome.eligible_at),
                reset_attempts=True,
                announce=announce,
            )
            if announce:
                await self._notify_board(mission,
                    f"Event request received for {requester}.\n\n"
                    f"[b]Location[/b]: {address or 'the location'}\n"
                    f"[b]Type[/b]: {kind_label}\n\n"
                    "Queued — the next free alliance mission slot opens at "
                    f"{outcome.eligible_at} UTC."
                )
            return
        if outcome.state in ("form_error", "http_error"):
            await self.missions.set_status(
                mid, "waiting", outcome.detail,
                next_attempt_at=_transient_backoff(mission["attempts"]),
                bump_attempts=True, announce=False,
            )
            return
        if outcome.state == "refused":
            # The free button is not available even though the timestamps
            # said the window should be open. That is a WAIT on the window
            # ladder (5 min, then 30), not a failure — permanently failing
            # here retired scheduled requests over a lagging game page.
            # MAX_ATTEMPTS still bounds a request that never becomes free.
            await self.missions.set_status(
                mid, "waiting",
                f"{outcome.detail}; free start not available yet — "
                "rechecking on the window ladder",
                next_attempt_at=_window_ladder(mission["attempts"]),
                bump_attempts=True, announce=False,
            )
            return
        if outcome.state == "not_found":
            await self.missions.set_status(mid, "failed", outcome.detail)
            await self._notify_board(mission, _event_error_reply(
                requester, f"{outcome.detail}. Admins have been notified."
            ))
            return
        if outcome.state == "dry_run":
            await self.missions.set_status(mid, "skipped", outcome.detail)
            await self._notify_board(mission,
                f"Event request processed for {requester}.\n\n"
                f"[b]Resolved[/b]: {address or 'the location'} "
                f"({lat:.5f}, {lng:.5f})\n"
                f"[b]Type[/b]: {kind_label}\n\n"
                "[dry-run — not started]"
            )
            await self._maybe_promote(mission, lat, lng, address)
            return
        if outcome.state == "unverified":
            # MissionChief took the POST but no mission appeared — almost
            # always because another alliance mission/event is still running.
            # That's a wait, not a failure; only a request stuck this way
            # for days gets surfaced to a human.
            if older_than_hours(mission["created_at"], REFUSED_GIVE_UP_HOURS):
                await self.missions.set_status(
                    mid, "failed",
                    f"{outcome.detail}; kept being refused for over "
                    f"{REFUSED_GIVE_UP_HOURS}h — giving up",
                )
                await self._notify_board(mission, _event_error_reply(
                    requester,
                    "MissionChief kept refusing the start. "
                    "Admins have been notified.",
                ))
                return
            retry_at = await self.start_backoff_until() or start_backoff_iso()
            await self.missions.set_status(
                mid, "waiting", f"{outcome.detail}; retrying after {retry_at}",
                next_attempt_at=_capped_wait(retry_at),
                reset_attempts=True, announce=announce,
            )
            if announce:
                await self._notify_board(mission,
                    f"Event request received for {requester}.\n\n"
                    f"[b]Location[/b]: {address or 'the location'}\n"
                    f"[b]Type[/b]: {kind_label}\n\n"
                    "Queued — the alliance is busy with another mission/event; "
                    "it will start as soon as a slot is free."
                )
            return
        # started
        await self.missions.set_status(mid, "done", outcome.detail)
        await self._queue_event_ping(mission, lat, lng, address)
        if mission["rotation_id"]:
            await self.rotation.mark_started(
                mission["rotation_id"], latitude=lat, longitude=lng,
                address=address or None,
            )
        await self._notify_board(mission,
            f"Event request processed for {requester}.\n\n"
            f"[b]Started[/b]: {kind_label} at "
            f"{address or 'the requested location'}"
        )
        if mission["source"] == "board" and not self.dry_run:
            await self._notify_ingame_started(
                requester, kind_label, address or "the requested location"
            )
        await self._maybe_promote(mission, lat, lng, address)

    async def _notify_ingame_started(
        self, requester: str | None, kind_label: str, address: str
    ) -> None:
        """The personal success notification for board requesters — an
        in-game MissionChief PM, like the reference bot sends."""
        from ..mc.messages import send_ingame_message

        if not requester or requester == "member":
            return
        try:
            await send_ingame_message(
                self.client, requester, "Event request",
                f"Your event request has been started: {kind_label} at "
                f"{address}. Have fun!",
            )
        except Exception:  # noqa: BLE001 - a PM must never fail the start
            log.exception("mission: in-game PM to %s failed", requester)

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
        """Try the rotation heads per KIND, least-recently started first.

        The large and event cooldowns are separate: an event entry at the
        head of the cycle whose 7-day window is closed must not hide a
        large entry whose 24h window is free (the same cross-kind
        starvation the queue guards against with blocked_kinds). One poll
        still starts at most one entry.

        A kind with OPEN member requests is skipped entirely: those own
        the kind's next free window. A parked request (waiting out a
        computed eligible_at that may overshoot the real window by a bit)
        must find the window still FREE at its recheck — the rotation
        grabbing it would leapfrog the member's request with some other
        event and scramble the schedule."""
        heads = []
        for kind in _KIND_LABELS:
            if await self.missions.open_for_kind(kind, limit=1):
                continue  # member requests own this kind's window
            entry = await self.rotation.next_entry(kind=kind)
            if entry is not None:
                heads.append(entry)
        # Preserve the global least-recently-started fairness across kinds.
        heads.sort(key=lambda e: (e["last_started_at"] is not None,
                                  e["last_started_at"] or "", e["id"]))
        for entry in heads:
            handled = await self._run_rotation_entry(entry)
            if handled:
                return 1
        return 0

    async def _run_rotation_entry(self, entry: aiosqlite.Row) -> int:
        lat, lng = entry["latitude"], entry["longitude"]
        address = entry["address"] or ""
        if lat is None or lng is None:
            try:
                resolved = await self._resolve(entry["location_text"] or "")
            except GeocodeError as exc:
                # A transient geocode failure (network, Nominatim 429/5xx) must
                # NOT permanently pause a good entry — the queue path already
                # keeps such requests waiting. Only a permanent failure (bad
                # place, dead API key) deactivates; otherwise keep the turn and
                # retry next poll.
                if getattr(exc, "transient", False):
                    log.info(
                        "rotation entry %s kept active — transient geocode "
                        "error (%s); will retry", entry["id"], exc,
                    )
                    return 0
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

        if outcome.state in ("waiting", "form_error", "http_error", "unverified"):
            # Free window not available / transient / refused by the game
            # (another alliance mission still running — the start backoff is
            # armed) — retry later WITHOUT advancing the cycle, so this entry
            # keeps its turn.
            return 0
        if outcome.state == "not_found":
            await self.rotation.deactivate_with_note(entry["id"], outcome.detail)
            return 0
        if outcome.state == "refused":
            # The free button is not there yet (game lagging, previous
            # mission winding down). Deactivating here used to DROP the
            # entry from the schedule and let the next one jump the list.
            # The entry keeps its turn; the next polls re-check until the
            # button appears, then the PLANNED entry starts.
            log.info(
                "rotation entry %s: free start not available yet — "
                "keeping its turn", entry["id"],
            )
            return 0
        # started / dry_run both consume the entry's turn.
        if outcome.state == "started":
            await self._queue_event_ping(entry, lat, lng, address)
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

        # The bot knows what it started: while a mission/event it recently
        # started (or a refused attempt) says the alliance slot is busy,
        # don't even submit — MissionChief would refuse the start and that
        # used to surface as a spurious "Mission failed". Coins bypass this.
        if not allow_coins:
            backoff = await self.start_backoff_until()
            if backoff:
                return StartOutcome(
                    "waiting",
                    "the alliance is busy with a mission/event the bot already "
                    f"started; next attempt after {backoff}",
                    eligible_at=backoff,
                )

        new_path = EVENT_KINDS[kind]["new_path"]
        try:
            html = await self.client.fetch_page(f"{new_path}?tlat={latitude}&tlng={longitude}")
        except MissionChiefError as exc:
            return StartOutcome("form_error", f"could not load mission form ({exc}); will retry")
        form = parse_event_form(html)
        saved_html = html
        if kind != "event":
            if source == "saved":
                # The lookup must see the anchors even when the block is
                # JS-drawn; the rendered fallback only triggers when the
                # plain HTML has none.
                saved_html = await self._saved_missions_html(html)
            # Free ride: the large form carries the Saved Missions dropdown —
            # keep the Discord chooser's list current on every start attempt.
            await self._cache_saved_missions(saved_html)

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
                form, saved_html, kind=kind, source=source,
                preset_type_id=preset_type_id,
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
            # The POST may have reached MissionChief and created the mission
            # before the response was lost. Confirm via the free-mission
            # cooldown before deciding: a start we can PROVE happened must be
            # reported as started so the caller advances the rotation cycle /
            # marks the request done — otherwise the same free large mission
            # is re-fired on the next window (the "same mission two days in a
            # row" a lost response would otherwise cause). If we cannot prove
            # it started, retry as before.
            verified = await self._verify_started(new_path, latitude, longitude, free_before, kind=kind)
            if verified:
                await self.set_start_backoff()
                return StartOutcome(
                    "started",
                    f"{kind} started at {latitude:.5f},{longitude:.5f} "
                    "(response lost — confirmed via cooldown)",
                    verified=True,
                )
            return StartOutcome("http_error", f"start request failed ({exc}); will retry")

        if status >= 400:
            await self.set_start_backoff()
            return StartOutcome(
                "unverified", f"MissionChief rejected the start (HTTP {status})",
                http_status=status,
            )

        # A paid start does NOT consume the free-mission cooldown, so the
        # cooldown-advance check can't confirm it — report success on the
        # accepted POST and leave verification to the owner in-game.
        if allow_coins:
            await self.set_start_backoff()
            return StartOutcome(
                "started",
                f"paid {kind} started at {latitude:.5f},{longitude:.5f} "
                f"(coins spent — verify in game)",
            )

        verified = await self._verify_started(new_path, latitude, longitude, free_before, kind=kind)
        await self.set_start_backoff()
        if verified is False:
            return StartOutcome(
                "unverified",
                "MissionChief accepted the request but no new mission appeared "
                "— another alliance mission/event is likely still running",
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
        if outcome.state == "started":
            # A paid start is a real mission too — members get the same ping.
            await self._queue_event_ping(
                {
                    "kind": spec.kind,
                    "event_random": spec.event_random,
                    "event_type_id": spec.event_type_id,
                    "caption": spec.custom.caption if spec.custom else None,
                    "saved_name": spec.saved_name,
                    "preset_type_id": spec.preset_type_id,
                    "location_text": spec.location_text,
                },
                lat, lng, address,
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
                captions = [s.caption for s in parse_saved_missions(html)]
                visible = (
                    "visible: " + ", ".join(captions[:10])
                    + (f" (+{len(captions) - 10} more)" if len(captions) > 10 else "")
                    if captions else
                    "the form shows NO saved missions — run `!fra savedmissions` "
                    "to diagnose"
                )
                raise _SavedMissionNotFound(
                    f"saved mission '{saved_name}' not found in the dropdown — "
                    + visible
                )
            return build_custom_mission_payload(
                form, saved.to_custom(), latitude=latitude, longitude=longitude,
                address=address,
            )
        # Preset large. Without an explicit preset the form's default radio
        # never changes, so keeping it would start the exact same game
        # mission every window — pick a random selectable type per start
        # instead, so the daily rotation has real variety.
        type_id = preset_type_id
        if type_id is None:
            choices = parse_large_mission_types(html) or list(PRESET_TYPE_IDS)
            type_id = random.choice(choices)
            log.info("mission: no preset chosen — random large type %s", type_id)
        return build_event_payload(
            form, kind="large", latitude=latitude, longitude=longitude,
            address=address, mission_type_id=type_id,
        )

    async def _verify_started(
        self, new_path: str, lat: float, lng: float, free_before: str | None,
        *, kind: str = "large",
    ) -> bool | None:
        """Confirm a start via an advanced free-mission cooldown. True =
        confirmed, False = cooldown unchanged (not started), None = unknown.

        MissionChief sometimes reflects the advanced cooldown a moment AFTER
        the start, so a first "unchanged" reading can be a false negative that
        would make the rotation re-fire the same mission next window. Re-check
        a few times with a short pause before concluding; if the cooldown
        stays unreadable, ask the alliance log whether a mission just started
        (an independent "…started" line)."""
        result: bool | None = None
        for attempt in range(VERIFY_RETRY_ATTEMPTS):
            try:
                check = parse_event_form(
                    await self.client.fetch_page(f"{new_path}?tlat={lat}&tlng={lng}")
                )
            except MissionChiefError:
                result = None
            else:
                free_after = check.last_free_at
                if free_after is None:
                    result = None
                elif free_before is None:
                    result = True
                else:
                    result = free_after > free_before
            if result:                       # confirmed — no need to wait more
                return True
            if attempt < VERIFY_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(VERIFY_RETRY_DELAY_SECONDS)
        # Cooldown never readable → last-resort independent check: did a
        # matching "…started" line just land in the alliance log?
        if result is None and await self._started_in_log(kind):
            return True
        return result

    async def _started_in_log(self, kind: str) -> bool:
        """Independent confirmation that a start landed: a fresh
        ``large scale mission started`` / ``alliance event started`` line at
        the top of the alliance log. Used only when the cooldown signal is
        unreadable, so it never overrides a definitive 'not started'."""
        want = "alliance_event_started" if kind == "event" else "large_mission_started"
        cutoff = _iso_minutes_ago(VERIFY_LOG_WINDOW_MINUTES)
        try:
            page = parse_logs_page(
                await self.client.fetch_page(f"{ALLIANCE_LOG_PATH}?page=1")
            )
        except MissionChiefError:
            return False
        for row in page.rows:                # newest first
            if row.get("action_key") != want:
                continue
            event_at = row.get("event_at")
            return event_at is None or event_at >= cutoff  # freshest match
        return False

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

    @staticmethod
    def _ping_name(row: aiosqlite.Row) -> str:
        """Mission/event type name shown in the role-ping embed."""
        if row["kind"] == "event":
            if row["event_random"] or row["event_type_id"] is None:
                return "Surprise event"
            return EVENT_TYPES.get(row["event_type_id"], "Alliance event")
        if row["caption"]:
            return str(row["caption"])
        if row["saved_name"]:
            return str(row["saved_name"])
        if row["preset_type_id"] is not None:
            return str(PRESET_TYPE_IDS.get(row["preset_type_id"], "Large scale mission"))
        return "Large scale mission"

    async def start_backoff_until(self) -> str | None:
        """The active alliance-busy window (ISO), or None once it expired."""
        raw = await self.state.get(STATE_START_BACKOFF)
        if raw and raw > utcnow_iso():
            return raw
        return None

    async def set_start_backoff(self, minutes: int = START_BACKOFF_MINUTES) -> None:
        await self.state.set(STATE_START_BACKOFF, start_backoff_iso(minutes))

    async def _queue_event_ping(
        self, row: aiosqlite.Row, lat: float | None, lng: float | None,
        address: str | None,
    ) -> None:
        """Record a REAL start in the ping outbox (the EventPinger cog
        delivers it). Never blocks or fails the start path."""
        try:
            await self.event_pings.add(
                kind=row["kind"],
                name=self._ping_name(row),
                address=address or row["location_text"] or None,
                latitude=lat,
                longitude=lng,
            )
        except Exception:  # noqa: BLE001 — the start itself succeeded
            log.exception("could not enqueue event ping")

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
    from ..mc.parsers.mission_template import HOSPITAL_DEPARTMENTS, render_template

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
        "[b]Own mission — copy the template below[/b]",
        "- Copy the whole list, fill in your location, a name and your "
        "numbers, and post it.",
        "- Lines you delete (or leave at 0) count as 0 — only fill in what "
        "you need.",
        "- Patient transport probability is a percentage (the template "
        "default is 50).",
        "- Hospital department: pick one by name — "
        + ", ".join(HOSPITAL_DEPARTMENTS.values()) + ".",
        "- Add a line 'schedule: recurring' to keep the mission coming back.",
        "",
        render_template(),
        "",
        "[b]Other options[/b]",
        "- name: My mission name",
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
