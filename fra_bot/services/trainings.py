"""Training auto-start from board requests (thread 5935).

Flow per new post: match training names → check the requester's
contribution rate against the roster → find an academy of the right
discipline with a free classroom → POST the education form → VERIFY the
class actually opened (the old bot's blind status<400 check produced
false confirmations) → reply on the board.

Board classes are always opened free (cost 0) with a 1-hour alliance
signup window, mirroring the alliance's existing policy.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

from ..config import Config
from ..db.database import Database
from ..db.repos import RemindersRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.academy import (
    AcademyListing,
    find_next_page_path,
    parse_academy_page,
    parse_alliance_buildings_page,
)
from ..mc.buildings_api import parse_api_buildings
from ..mc.parsers.board import BoardPost
from ..mc.trainings_catalog import (
    DISCIPLINES,
    AmbiguousMatch,
    TrainingMatch,
    match_trainings,
)
from .board_requests import BoardRequestService

log = logging.getLogger(__name__)

ALLIANCE_BUILDINGS_PATH = "/verband/gebauede"
API_BUILDINGS_PATH = "/api/buildings"
MAX_ACADEMY_LIST_PAGES = 10
# Academy building type-ids on MissionChief (the icon IS the type). This is
# the reliable way to find an academy — it identifies the discipline AND tells
# academies apart from stations, without depending on a scraped list-page
# "start course" button that can render differently per academy.
ACADEMY_TYPE_IDS = {"fire": 4, "police": 7, "ems": 19, "coastal": 24}
ALLIANCE_SIGNUP_SECONDS = 3600            # normal class: open to the alliance 1h
ALLIANCE_SIGNUP_SECONDS_DEFERRED = 43200  # a class that had to wait: open 12h
BOARD_FEE = 0                   # board classes are free
RETRY_MINUTES = 15              # backoff between busy retries (bounded by MAX_ATTEMPTS)
CLASS_CAPACITY = 10             # people per class (game constant, informational)
MAX_CLASSES_PER_REQUEST = 4     # copies of one course per request/run


def clamp_class_count(value) -> int:
    """A requested copy count, forced into 1..MAX_CLASSES_PER_REQUEST."""
    try:
        return max(1, min(MAX_CLASSES_PER_REQUEST, int(value)))
    except (TypeError, ValueError):
        return 1

GUIDE_MARKER = "[FRA] 📋 How to request a TRAINING"

#: State key holding the last availability walk: {"counts": {...}, "at": epoch}.
#: Written by the hourly guide refresh; read by the Discord training chooser.
AVAILABILITY_STATE_KEY = "training_availability"

#: State key holding the live-harvested course lists per agency:
#: {"courses": {discipline: {name: days}}, "at": epoch}. The academies'
#: education dropdowns ARE the authoritative course list — the built-in
#: catalog is a snapshot that goes stale as the game adds courses.
TRAINING_COURSES_STATE_KEY = "training_courses"

_DAYS_SUFFIX_RE = re.compile(r"\s*\(\s*(\d+)\s*days?\s*\)\s*$", re.IGNORECASE)


def _clean_course_label(label):
    """An education-dropdown label as ``(course name, days-or-None)`` —
    some labels carry a "(N days)" suffix, most are the bare name."""
    match = _DAYS_SUFFIX_RE.search(label or "")
    if match:
        return label[: match.start()].strip(), int(match.group(1))
    return (label or "").strip(), None


def _static_days(discipline, name):
    """Duration from the built-in catalog, tolerant of label variations."""
    from ..mc.trainings_catalog import normalized_equals

    for static_name, days in DISCIPLINES.get(discipline, {}).items():
        if normalized_equals(static_name, name):
            return days
    return 0


async def merged_course_catalog(state):
    """Courses per agency for the Discord chooser: the live-harvested list
    where one exists (it is what the academies actually offer right now),
    the built-in catalog for agencies without a harvest yet."""
    merged = {key: dict(DISCIPLINES.get(key, {})) for key in _AGENCY_ORDER}
    raw = await state.get(TRAINING_COURSES_STATE_KEY)
    if not raw:
        return merged
    try:
        data = json.loads(raw)
    except ValueError:
        return merged
    courses = data.get("courses") if isinstance(data, dict) else None
    if not isinstance(courses, dict):
        return merged
    for key in _AGENCY_ORDER:
        live = courses.get(key)
        if isinstance(live, dict) and live:
            merged[key] = {str(n): int(d or 0) for n, d in live.items()}
    return merged

# Display order + labels for the per-agency guide posts (the reference bot's
# split: one overview post + one post per academy type, so no single post
# grows past what the forum accepts).
_AGENCY_ORDER = ("fire", "police", "ems", "coastal")
_AGENCY_TITLES = {
    "fire": "🚒 Fire Station",
    "police": "🚓 Police",
    "ems": "🚑 EMS / Rescue",
    "coastal": "🌊 Water Rescue",
}
# Prefix used in request text for courses that exist in several academy
# types. These resolve in the request parser (DISCIPLINE_PREFIXES).
_AGENCY_PREFIX = {
    "fire": "Fire Station",
    "police": "Police",
    "ems": "EMS",
    "coastal": "Water Rescue",
}


def _section_marker(key: str) -> str:
    return f"[FRA] 📋 {_AGENCY_PREFIX[key]} training request text"


def _ambiguous_course_names() -> set[str]:
    """Course names that exist in more than one academy type."""
    seen: dict[str, int] = {}
    for courses in DISCIPLINES.values():
        for name in courses:
            seen[name] = seen.get(name, 0) + 1
    return {name for name, count in seen.items() if count > 1}


# The reference bot's "how to join" instructions, appended to every success
# notification (board reply and in-game PM).
COURSE_JOIN_INSTRUCTIONS = (
    "How to add people to the course\n"
    "Browser/Desktop: open the academy link, open the active training "
    "course, then add the required personnel.\n"
    "Phone: open the same academy link in your mobile browser, open the "
    "active course, then add personnel from the course page."
)


def _friendly_failure(discipline: str, reason: str) -> str:
    """The reference bot's member-facing rewrite of internal failure text."""
    lowered = (reason or "").lower()
    if "no free classroom" in lowered or "classrooms busy" in lowered or (
        "no available" in lowered and "academy" in lowered
    ):
        return (
            f"No free {discipline} classrooms are available right now. "
            "The current classes are likely full. Please try again later."
        )
    return reason


def _board_reply(
    requester: str | None,
    opened: list[dict],
    could_not: list[str],
    ambiguous_reasons: list[str],
    *,
    pending: list[str],
) -> str:
    """Assemble the board reply in the reference bot's format."""
    who = requester or "member"
    real = [o for o in opened if not o["dry_run"]]
    dry = [o for o in opened if o["dry_run"]]

    # Nothing recognized / nothing possible: the error format.
    if not opened and not pending and (could_not or ambiguous_reasons):
        reasons = [line[2:] if line.startswith("- ") else line for line in could_not]
        reasons.extend(ambiguous_reasons)
        return (
            f"Training request could not be processed for {who}.\n\n"
            "Reason: " + "\n".join(reasons)
        )

    parts: list[str] = [f"Training request processed for {who}."]
    if real:
        parts.append("")
        parts.append("Opened:")
        for entry in real:
            parts.append(
                f"- {entry['name']}: opened 1 class(es) in academy "
                f"{entry['building_id']}"
            )
        parts.append("")
        parts.append("Where to find and join the class:")
        for entry in real:
            parts.append(
                f"- Academy {entry['building_id']}: "
                f"https://www.missionchief.com/buildings/{entry['building_id']}"
            )
        parts.append(
            "- Browser/Desktop: open the academy link, open the active "
            "training course, then add the required personnel."
        )
        parts.append(
            "- Phone: open the same academy link in your mobile browser, "
            "open the active course, then add personnel from the course page."
        )
    if dry:
        parts.append("")
        parts.append("Would open (dry-run — nothing was started):")
        for entry in dry:
            parts.append(f"- {entry['name']}: academy {entry['building_id']}")
    if pending:
        parts.append("")
        parts.append("Still working on (no free classroom yet, will retry):")
        for name in pending:
            parts.append(f"- {name}")
    if could_not or ambiguous_reasons:
        parts.append("")
        parts.append("Could not open automatically:")
        parts.extend(could_not)
        for reason in ambiguous_reasons:
            parts.append(f"- {reason}")
    if len(parts) == 1:
        return ""
    return "\n".join(parts)


class TrainingsService(BoardRequestService):
    kind = "training"
    guide_marker = GUIDE_MARKER

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        super().__init__(cfg, client, db)
        self._auto = cfg.automation.training
        self.reminders = RemindersRepo(db)

    @property
    def thread_id(self) -> int:
        return self._auto.thread_id

    def guide_body(self) -> str:
        return _overview_guide(self._auto.min_contribution_rate)

    async def _overview_content(self, now_epoch: float, *, quick: bool = False) -> str:
        """Overview post: the stable how-to text + the live academy
        availability + a last-updated line. Expensive (walks the academy
        pages) — only built once the guide throttle decided to write.
        ``quick`` skips the walk (used by the forced `!fra guides` sync so
        the posts land fast); the hourly refresh fills the numbers in."""
        from ..mc.board import guide_updated_line

        counts = None if quick else await self._collect_availability()
        lines = [
            self.guide_body(),
            "",
            "[b]Current academy availability[/b]",
            guide_updated_line(now_epoch),
        ]
        if counts is None:
            lines.append("- being refreshed — the numbers appear here shortly")
        else:
            for key in _AGENCY_ORDER:
                count = counts.get(key, 0)
                unit = "class" if count == 1 else "classes"
                lines.append(f"- {_AGENCY_TITLES[key]}: {count} {unit}")
        return "\n".join(lines)

    # -- multi-post guide (overview + one post per agency) ----------------

    def _section_id_key(self, key: str) -> str:
        return f"board_guide_id:training:{self.thread_id}:{key}"

    def _section_hash_key(self, key: str) -> str:
        return f"board_guide_hash:training:{self.thread_id}:{key}"

    def _section_refreshed_key(self, key: str) -> str:
        return f"board_guide_refreshed:training:{self.thread_id}:{key}"

    def _guide_order_key(self) -> str:
        return f"board_guide_order_checked:training:{self.thread_id}"

    def _guide_markers_in_order(self) -> list[str]:
        """The five guide markers in the order they must appear on the board."""
        return [GUIDE_MARKER] + [_section_marker(key) for key in _AGENCY_ORDER]

    async def _maybe_repair_guide_order(self, now_epoch: float) -> None:
        """Ensure the guide posts sit in the canonical order and none is
        missing; rebuild them if not. A deleted post reappears at the BOTTOM
        (out of order), so both faults are handled the same way: delete every
        surviving guide post and clear its bookkeeping, and the create-or-edit
        pass recreates them all top-to-bottom in order. The board walk this
        needs is throttled to at most hourly, so it never burdens the poll."""
        raw = await self.state.get(self._guide_order_key())
        try:
            last = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if (now_epoch - last) < 3600:
            return

        markers = self._guide_markers_in_order()
        try:
            found = await self.board.find_bot_posts(self.thread_id, markers)
        except MissionChiefError as exc:
            log.warning("training: could not check guide order on %s: %s",
                        self.thread_id, exc)
            return
        await self.state.set(self._guide_order_key(), repr(now_epoch))

        ids = [found.get(marker) for marker in markers]
        present = [pid for pid in ids if pid is not None]
        in_order = None not in ids and all(a < b for a, b in zip(present, present[1:]))
        if in_order:
            return  # all five present and in canonical order — nothing to do

        log.info(
            "training: guide posts out of order or missing on thread %s — "
            "rebuilding in canonical order", self.thread_id,
        )
        for pid in present:
            try:
                await self.board.delete_post(self.thread_id, pid)
            except MissionChiefError as exc:
                log.warning("training: could not delete guide post %s: %s", pid, exc)
        for id_key, hash_key, refreshed_key in (
            (self._guide_id_key(), self._guide_hash_key(), self._guide_refreshed_key()),
            *(
                (self._section_id_key(key), self._section_hash_key(key),
                 self._section_refreshed_key(key))
                for key in _AGENCY_ORDER
            ),
        ):
            await self.state.delete(id_key)
            await self.state.delete(hash_key)
            await self.state.delete(refreshed_key)

    async def _ensure_guide(self, *, quick: bool = False) -> None:
        """Maintain the guide as SEVERAL posts, like the old bot: one
        overview (with live availability, refreshed hourly) plus one post
        per academy type listing the exact request text. Splitting keeps
        every post small enough for the forum. Each post is find-or-edit,
        never duplicated; a failing section doesn't block the others.
        ``quick`` skips the availability walk (forced syncs)."""
        if not self.cfg.automation.reply_to_board:
            return
        import hashlib

        from ..mc.board import ensure_guide_post, guide_now

        now = guide_now()
        # Keep the five posts in the canonical order (overview → Fire → Police
        # → EMS → Coastal) and rebuild them if one was deleted or the order
        # drifted — BEFORE the create-or-edit pass, which then recreates any
        # that were cleared, in order.
        await self._maybe_repair_guide_order(now)
        body = self.guide_body()
        try:
            await ensure_guide_post(
                self.board, self.state, self.thread_id,
                id_key=self._guide_id_key(), hash_key=self._guide_hash_key(),
                refreshed_key=self._guide_refreshed_key(),
                marker=GUIDE_MARKER,
                desired=lambda: self._overview_content(now, quick=quick),
                signature=hashlib.sha1(body.encode("utf-8")).hexdigest()[:12],
                now_epoch=now,
            )
        except MissionChiefError as exc:
            log.warning("training: could not maintain guide overview: %s", exc)
        for key in _AGENCY_ORDER:
            text = _discipline_guide(key)
            try:
                await ensure_guide_post(
                    self.board, self.state, self.thread_id,
                    id_key=self._section_id_key(key),
                    hash_key=self._section_hash_key(key),
                    refreshed_key=self._section_refreshed_key(key),
                    marker=_section_marker(key),
                    desired=text,
                    signature=hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
                    now_epoch=now,
                    # Static content: only rewrite when the catalog changes.
                    min_refresh_seconds=30 * 24 * 3600,
                )
            except MissionChiefError as exc:
                log.warning("training: could not maintain %s guide: %s", key, exc)

    async def force_guide(self, *, repost: bool = False) -> str:
        """Section-aware force sync for ``!fra guides`` — reports each of
        the five posts (overview + four agencies)."""
        label = f"training (thread {self.thread_id})"
        if not self.cfg.automation.reply_to_board:
            return f"➖ {label}: reply_to_board is off"
        sections: list[tuple[str, str, str, str, str]] = [
            ("overview", GUIDE_MARKER, self._guide_id_key(),
             self._guide_hash_key(), self._guide_refreshed_key()),
        ]
        for key in _AGENCY_ORDER:
            sections.append((
                key, _section_marker(key), self._section_id_key(key),
                self._section_hash_key(key), self._section_refreshed_key(key),
            ))
        try:
            for _, marker, id_key, hash_key, refreshed_key in sections:
                if repost:
                    stored = await self.state.get(id_key)
                    target = (
                        int(stored) if stored
                        else await self.board.find_bot_post(self.thread_id, marker)
                    )
                    if target:
                        await self.board.delete_post(self.thread_id, int(target))
                    await self.state.delete(id_key)
                await self.state.delete(hash_key)
                await self.state.delete(refreshed_key)
            # Quick sync: land the five posts fast, without the availability
            # walk. The overview's refresh marker is cleared afterwards so
            # the next poll fills the numbers in within minutes.
            await self._ensure_guide(quick=True)
        except MissionChiefError as exc:
            return f"❌ {label}: {exc}"
        parts = []
        for name, _, id_key, _, _ in sections:
            post_id = await self.state.get(id_key)
            parts.append(f"{name} #{post_id}" if post_id else f"{name} ❌")
        if await self.state.get(self._guide_id_key()):
            await self.state.delete(self._guide_refreshed_key())
        icon = "✅" if all("#" in part for part in parts) else "⚠️"
        url = self.client.url(f"/alliance_threads/{self.thread_id}")
        line = f"{icon} {label}: " + " · ".join(parts) + f" — {url}"
        if icon == "⚠️":
            reason = getattr(self.board, "last_error", None)
            if reason:
                line += f"\n   ↳ last error: {reason}"
        return line

    async def _collect_availability(self) -> dict[str, int] | None:
        """Free classrooms per discipline across all alliance academies.

        Returns ``None`` if the building list can't be read (shown as
        "temporarily unavailable"); a per-academy page failure just omits that
        academy's rooms. The walk is many paced page fetches, so it runs as
        BULK traffic — it yields to member requests (opening a training has
        first priority, guide upkeep does not). The result is cached in
        state so the Discord training chooser can show the numbers instantly
        without walking the academies per click."""
        from ..core.pacing import bulk_traffic

        try:
            with bulk_traffic():
                # Find academies by building TYPE-ID (the icon = the type), the
                # same reliable signal _find_academies uses; scrape fallback.
                academies = await self._academy_ids_by_discipline()
                if not any(academies.values()):
                    log.info("training availability: no academies detected — skipping")
                    return None

                counts = {key: 0 for key in _AGENCY_ORDER}
                # Per-discipline completeness: a discipline is only trustworthy
                # if EVERY one of its academies' detail pages was read. A
                # partial outage (some detail pages 429/parse-fail) would
                # otherwise report a fresh "0" that means "unknown", not "no
                # free classrooms" — which auto-scale must never build on.
                complete = {key: True for key in _AGENCY_ORDER}
                harvest: dict[str, dict[str, int]] = {
                    key: {} for key in _AGENCY_ORDER
                }
                for discipline, ids in academies.items():
                    for building_id in ids:
                        try:
                            page = parse_academy_page(
                                await self.client.fetch_page(f"/buildings/{building_id}")
                            )
                        except MissionChiefError as exc:
                            complete[discipline] = False
                            log.debug("training availability: academy %s failed (%s)",
                                      building_id, exc)
                            continue
                        counts[discipline] += max(0, page.available_rooms)
                        # Free ride: the education dropdown on this page IS the
                        # authoritative course list — harvest it so the Discord
                        # chooser never misses a newly added course.
                        for label in page.courses:
                            name, days = _clean_course_label(label)
                            if not name:
                                continue
                            if days is None:
                                days = _static_days(discipline, name)
                            harvest[discipline].setdefault(name, days)
        except MissionChiefError as exc:
            log.warning("training availability: could not read academy list: %s", exc)
            return None

        await self.state.set(AVAILABILITY_STATE_KEY, json.dumps({
            "counts": counts,
            "complete": complete,
            "at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
        }))
        await self._store_course_harvest(harvest)
        return counts

    async def _store_course_harvest(
        self, harvest: dict[str, dict[str, int]]
    ) -> None:
        """Merge the walked course lists into state, per discipline — an
        agency whose academies could not be read keeps its previous list
        (an empty harvest is indistinguishable from a read failure)."""
        raw = await self.state.get(TRAINING_COURSES_STATE_KEY)
        stored: dict = {}
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and isinstance(data.get("courses"), dict):
                    stored = data["courses"]
            except ValueError:
                stored = {}
        changed = False
        for key, courses in harvest.items():
            if courses:
                stored[key] = courses
                changed = True
        if not changed:
            return
        await self.state.set(TRAINING_COURSES_STATE_KEY, json.dumps({
            "courses": stored,
            "at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
        }))

    async def cached_availability(self) -> dict | None:
        """The last availability walk as ``{"counts": {...}, "at": epoch}``,
        or None when no walk has completed yet."""
        raw = await self.state.get(AVAILABILITY_STATE_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("counts"), dict):
            return None
        return data

    async def refresh_availability(
        self, *, max_age_seconds: int = 50 * 60
    ) -> dict | None:
        """The hourly refresh behind the class-availability panel.

        Reuses the cache when a recent walk (the guide refresh, or a prior
        pass of this job) already collected the numbers — the two hourly
        consumers must not double the game traffic — and walks the
        academies otherwise. Returns the fresh cache entry, or None when
        the walk failed and nothing usable is cached."""
        cached = await self.cached_availability()
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        if cached and now - int(cached.get("at") or 0) < max_age_seconds:
            return cached
        if await self._collect_availability() is None:
            return cached
        return await self.cached_availability()

    async def parse_request(self, post: BoardPost) -> dict | None:
        # Match against the LIVE course catalog (harvested from the academy
        # pages) — the built-in list goes stale as the game adds courses,
        # and a missing name used to fuzz onto the nearest wrong one.
        catalog = await merged_course_catalog(self.state)
        matches, ambiguous = match_trainings(post.content, catalog)
        if not matches and not ambiguous:
            return None  # chatter, not a training request
        return self.request_data(
            post,
            {
                "trainings": [
                    {
                        "discipline": m.discipline, "name": m.name,
                        "duration": m.duration_days, "count": m.count,
                    }
                    for m in matches
                ],
                "ambiguous": [
                    {"name": a.name, "disciplines": list(a.disciplines)} for a in ambiguous
                ],
            },
        )

    async def execute_request(self, request, *, announce: bool) -> None:
        payload = json.loads(request["payload"] or "{}")

        if "pending_trainings" in payload:
            # Retry: only the trainings (and copy counts) still marked busy.
            match_counts = [
                (
                    TrainingMatch(discipline=t["discipline"], name=t["name"],
                                  duration_days=0),
                    clamp_class_count(t.get("count", 1)),
                )
                for t in payload["pending_trainings"]
            ]
            ambiguous: list[AmbiguousMatch] = []
        else:
            match_counts = [
                (
                    TrainingMatch(
                        discipline=t["discipline"], name=t["name"],
                        duration_days=t.get("duration", 0),
                    ),
                    clamp_class_count(t.get("count", 1)),
                )
                for t in payload.get("trainings", [])
            ]
            ambiguous = [
                AmbiguousMatch(name=a["name"], disciplines=tuple(a["disciplines"]))
                for a in payload.get("ambiguous", [])
            ]
            # Gate on contribution rate on the first attempt only.
            rate = await self.contribution_rate(request["requester_mc_id"])
            if rate is not None and rate < self._auto.min_contribution_rate:
                await self.requests.set_status(
                    request["id"], "skipped",
                    f"contribution rate {rate:g}% is below the required "
                    f"{self._auto.min_contribution_rate:g}%",
                )
                await self.reply_for(
                    request,
                    f"@{request['requester_name']}: your training request was not "
                    f"processed — your alliance contribution is {rate:g}%, the "
                    f"minimum is {self._auto.min_contribution_rate:g}%."
                )
                return

        await self._process(request, payload, match_counts, ambiguous, announce=announce)

    async def _process(
        self,
        request,
        payload: dict,
        match_counts: list[tuple[TrainingMatch, int]],
        ambiguous: list[AmbiguousMatch],
        *,
        announce: bool,
    ) -> None:
        request_id = request["id"]
        requester = request["requester_name"]
        results: list[dict] = []
        opened: list[dict] = []       # {name, building_id, dry_run}
        could_not: list[str] = []     # "- {name}: {friendly reason}" lines
        pending: list[dict] = []      # transient failures worth retrying
        opened_any = False

        ambiguous_reasons = [self._ambiguity_help(a) for a in ambiguous]

        # A class that had to WAIT for a free classroom (this request was
        # already retried at least once) is opened for 12h so the members who
        # waited still have time to join; a first-try class opens for the
        # normal 1h.
        deferred = (request["attempts"] or 0) > 0
        signup = (
            ALLIANCE_SIGNUP_SECONDS_DEFERRED if deferred else ALLIANCE_SIGNUP_SECONDS
        )

        for match, want in match_counts:
            # Discord requests may ask for several copies of the same class
            # (capped at MAX_CLASSES_PER_REQUEST); board requests are 1.
            remaining = want
            reminder_scheduled = False
            while remaining > 0:
                outcome = await self._open_training(match, duration=signup)
                results.append({
                    "training": match.name,
                    "outcome": outcome["status"],
                    "building_id": outcome.get("building_id"),
                })
                if outcome["status"] == "opened":
                    remaining -= 1
                    opened_any = True
                    opened.append({
                        "name": match.name,
                        "building_id": outcome["building_id"],
                        "dry_run": bool(outcome.get("dry_run")),
                    })
                    if not reminder_scheduled:
                        # The copies share start + duration: one ping covers all.
                        await self._maybe_schedule_reminder(request_id, payload, match)
                        reminder_scheduled = True
                    continue
                if outcome["status"] == "uncertain":
                    # Submitted but unproven — NEVER re-post this copy (a
                    # false negative would double-open it).
                    remaining -= 1
                    opened_any = True
                    could_not.append(
                        f"- {match.name}: submitted to academy "
                        f"{outcome['building_id']} but the opening could not be "
                        "confirmed — please double-check."
                    )
                    continue
                if outcome["status"] == "busy":
                    # Classrooms exhausted mid-way: park the copies still
                    # wanted and retry them at the next pass.
                    pending.append({
                        "discipline": match.discipline, "name": match.name,
                        "count": remaining,
                    })
                else:  # failed — permanent for this course, drop every copy
                    could_not.append(
                        f"- {match.name}: "
                        f"{_friendly_failure(match.discipline, outcome['reason'])}"
                    )
                break

        next_attempt_at: str | None = None
        bump = False
        if pending:
            status = "waiting"
            detail = "retrying: " + ", ".join(
                f"{p['name']} ×{p['count']}" if p["count"] > 1 else p["name"]
                for p in pending
            )
            # Busy retries must be BOUNDED: bump attempts so MAX_ATTEMPTS can
            # end a hopeless request (e.g. no academy of that discipline
            # exists), and back off so each retry doesn't re-walk the whole
            # academy list every poll.
            bump = True
            next_attempt_at = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=RETRY_MINUTES)
            ).isoformat()
        elif opened_any:
            status = "done"
            detail = "; ".join(f"{r['training']}: {r['outcome']}" for r in results)
        elif match_counts:
            status = "failed"
            detail = "; ".join(f"{r['training']}: {r['outcome']}" for r in results)
        else:
            status = "skipped"
            detail = "only ambiguous names found"

        # Preserve Discord flags (discord_user_id/remind/channel_id) across
        # retries — the payload is rewritten every attempt.
        merged = dict(payload)
        merged.update({"results": results, "pending_trainings": pending})
        await self.requests.set_status(
            request_id,
            status,
            detail,
            payload=json.dumps(merged),
            next_attempt_at=next_attempt_at,
            bump_attempts=bump,
            announce=announce or status in ("done", "failed"),
        )

        # Board feedback in the reference bot's format. Interim (busy)
        # states stay quiet after the first notice; terminal states and
        # successes always report.
        if announce or opened_any or could_not:
            reply = _board_reply(
                requester, opened, could_not, ambiguous_reasons,
                pending=[
                    f"{p['name']} ×{p['count']}" if p["count"] > 1 else p["name"]
                    for p in pending
                ] if pending and announce else [],
            )
            if reply:
                await self.reply_for(request, reply)

        # A board requester gets the reference bot's success notification as
        # an in-game PM (board posts carry no Discord identity). Only real
        # opens — never dry-run — and only for board-sourced requests.
        real_opened = [o for o in opened if not o["dry_run"]]
        if real_opened and not self.is_discord_request(request) and not self.dry_run:
            await self._notify_ingame(requester, real_opened)

    async def _on_give_up(self, request) -> None:
        """Tell the member when a training request is abandoned after repeated
        'no free classroom' — the classes stayed full for too long."""
        try:
            payload = json.loads(request["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        pending = payload.get("pending_trainings") or []
        names = ", ".join(
            p.get("name", "?") for p in pending if isinstance(p, dict)
        ) or "the requested class(es)"
        who = request["requester_name"] or "member"
        await self.reply_for(request, (
            f"Training request could not be completed for {who}.\n\n"
            f"No free classroom became available for: {names}. "
            "The current classes are likely full — please try again later."
        ))

    async def _maybe_schedule_reminder(
        self, request_id: int, payload: dict, match: TrainingMatch
    ) -> None:
        """Discord requesters can opt into a ping once the course should be
        finished (start + the catalog duration in days)."""
        user_id = payload.get("discord_user_id")
        if not payload.get("remind") or not user_id:
            return
        days = match.duration_days or DISCIPLINES.get(match.discipline, {}).get(match.name, 0)
        if not days:
            return  # unknown duration: can't estimate an end time
        due = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
        ).isoformat()
        await self.reminders.add(
            discord_user_id=int(user_id),
            channel_id=payload.get("channel_id"),
            training=match.name,
            due_at=due,
            request_id=request_id,
        )

    # ------------------------------------------------------------------

    def _ambiguity_help(self, ambiguity: AmbiguousMatch) -> str:
        # The reference bot's ambiguity description, verbatim structure.
        options = ", ".join(
            f"{_AGENCY_PREFIX.get(d, d)} - {ambiguity.name}"
            for d in sorted(ambiguity.disciplines)
        )
        return (
            f"{ambiguity.name} exists in multiple academy types. "
            f"Use one of: {options}."
        )

    async def _notify_ingame(self, requester: str | None, opened: list[dict]) -> None:
        """The reference bot's success notification for board requesters:
        an in-game MissionChief PM (board posts carry no Discord identity)."""
        from ..mc.messages import send_ingame_message

        if not requester:
            return
        lines: list[str] = []
        for entry in opened:
            lines.append(
                "Your training has been started automatically: "
                f"{entry['name']} (1 class, free)."
            )
            if entry.get("building_id"):
                lines.append(
                    "Academy: https://www.missionchief.com/buildings/"
                    f"{entry['building_id']}"
                )
        lines.append("")
        lines.append(COURSE_JOIN_INSTRUCTIONS)
        try:
            await send_ingame_message(
                self.client, requester, "Training request", "\n".join(lines)
            )
        except Exception:  # noqa: BLE001 - a PM must never fail the request
            log.exception("training: in-game PM to %s failed", requester)

    async def _open_training(
        self, match: TrainingMatch, *, duration: int = ALLIANCE_SIGNUP_SECONDS
    ) -> dict:
        """Try to open one class.

        Returns a dict with ``status`` one of:
          * ``opened``    — class confirmed open (building_id set)
          * ``uncertain`` — POST accepted but couldn't verify (building_id set)
          * ``busy``      — transient (no free classroom / list failed); retry
          * ``failed``    — permanent (course not offered here)
        """
        try:
            academies = await self._find_academies(match.discipline)
        except MissionChiefError as exc:
            return {"status": "busy", "reason": f"could not list academies ({exc})"}
        if not academies:
            return {
                "status": "busy",
                "reason": f"no available {match.discipline} academy (classrooms busy?)",
            }

        last_reason = "no suitable academy"
        busy = False
        for academy in academies:
            path = f"/buildings/{academy.building_id}"
            try:
                page = parse_academy_page(await self.client.fetch_page(path))
            except MissionChiefError as exc:
                last_reason = f"could not load academy {academy.building_id} ({exc})"
                busy = True
                continue

            if page.action is None or page.authenticity_token is None:
                last_reason = f"academy {academy.building_id} has no education form"
                continue
            course_value = page.find_course_value(match.name)
            if course_value is None:
                last_reason = (
                    f"course '{match.name}' not offered by academy {academy.building_id}"
                )
                continue
            if page.costs and BOARD_FEE not in page.costs:
                last_reason = f"academy {academy.building_id} does not allow a free class"
                continue
            if page.available_rooms < 1:
                last_reason = f"academy {academy.building_id} has no free classroom"
                busy = True
                continue

            if self.dry_run:
                log.info(
                    "DRY-RUN: would open '%s' in academy %s (form %s)",
                    match.name, academy.building_id, page.action,
                )
                return {
                    "status": "opened",
                    "building_id": academy.building_id,
                    "dry_run": True,
                }

            rooms_before = page.available_rooms
            try:
                status, _, _ = await self.client.post_form(
                    page.action,
                    {
                        "utf8": "✓",
                        "authenticity_token": page.authenticity_token,
                        "building_rooms_use": "1",
                        "education_select": course_value,
                        "alliance[duration]": str(duration),
                        "alliance[cost]": str(BOARD_FEE),
                        "commit": "Educate",
                    },
                    referer=self.client.url(path),
                )
            except MissionChiefError as exc:
                # A submit error may or may not have landed — do NOT try
                # another academy (that risks a double open). Report
                # uncertain so an admin verifies.
                log.warning("Training POST to %s errored: %s", academy.building_id, exc)
                return {
                    "status": "uncertain",
                    "building_id": academy.building_id,
                    "reason": str(exc),
                }
            if status >= 400:
                last_reason = f"MissionChief rejected the request (HTTP {status})"
                continue

            # The POST returned <400, so it likely landed. Verify by
            # re-reading the classroom count; but never POST elsewhere
            # after this point — that would double-open on a false
            # negative. Verification only downgrades to 'uncertain'.
            try:
                after = parse_academy_page(await self.client.fetch_page(path))
                if after.available_rooms >= rooms_before and after.action is not None:
                    return {
                        "status": "uncertain",
                        "building_id": academy.building_id,
                        "reason": "classroom count did not drop",
                    }
            except MissionChiefError:
                return {
                    "status": "uncertain",
                    "building_id": academy.building_id,
                    "reason": "could not verify",
                }
            return {"status": "opened", "building_id": academy.building_id}

        return {"status": "busy" if busy else "failed", "reason": last_reason}

    async def _find_academies(self, discipline: str) -> list[AcademyListing]:
        """Alliance academies for a discipline, preferred building first.

        Identified by building type-id from ``/api/buildings`` (the icon = the
        type) — the reliable signal that tells an academy from a station and
        never depends on a scraped list-page button. Falls back to the old
        alliance-buildings scrape if the API is unavailable."""
        candidates = await self._academies_from_api(discipline)
        if not candidates:
            candidates = await self._academies_from_list(discipline)
        preferred_id = self._auto.preferred_academies.get(discipline)
        candidates.sort(key=lambda a: 0 if a.building_id == preferred_id else 1)
        if not candidates and preferred_id:
            # Everything failed us; still try the known building.
            candidates = [
                AcademyListing(
                    building_id=preferred_id,
                    name=f"Preferred {discipline} academy",
                    discipline=discipline,
                    has_start_button=True,
                )
            ]
        return candidates

    async def _academies_from_api(self, discipline: str) -> list[AcademyListing]:
        type_id = ACADEMY_TYPE_IDS.get(discipline)
        if type_id is None:
            return []
        try:
            raw = await self.client.fetch_page(API_BUILDINGS_PATH)
            buildings = parse_api_buildings(raw)
        except MissionChiefError as exc:
            log.info("training: /api/buildings unavailable (%s); using list scrape", exc)
            return []
        except Exception as exc:  # noqa: BLE001 — bad/non-JSON body → fall back
            log.info("training: could not parse /api/buildings (%s); using list scrape", exc)
            return []
        return [
            AcademyListing(
                building_id=b.building_id,
                name=f"{discipline} academy #{b.building_id}",
                discipline=discipline,
                has_start_button=True,
            )
            for b in buildings
            if b.building_type_id == type_id and b.building_id is not None
        ]

    async def _academies_from_list(self, discipline: str) -> list[AcademyListing]:
        listings: list[AcademyListing] = []
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_ACADEMY_LIST_PAGES):
            html = await self.client.fetch_page(path)
            listings.extend(parse_alliance_buildings_page(html))
            next_path = find_next_page_path(html)
            if not next_path:
                break
            path = next_path
        return [
            listing
            for listing in listings
            if listing.discipline == discipline and listing.has_start_button
        ]

    async def _academy_ids_by_discipline(self) -> dict[str, list[int]]:
        """``{discipline: [academy building ids]}`` for the availability walk.

        Primary source is ``/api/buildings`` by TYPE-ID (icon = type); if that
        is unavailable it falls back to the alliance-buildings scrape (which may
        raise, letting the caller treat it as "couldn't read")."""
        out: dict[str, list[int]] = {key: [] for key in _AGENCY_ORDER}
        try:
            buildings = parse_api_buildings(
                await self.client.fetch_page(API_BUILDINGS_PATH)
            )
        except MissionChiefError:
            buildings = None
        except Exception:  # noqa: BLE001 — bad/non-JSON body → scrape fallback
            buildings = None
        if buildings:
            id_to_disc = {tid: disc for disc, tid in ACADEMY_TYPE_IDS.items()}
            seen: set[int] = set()
            for b in buildings:
                disc = id_to_disc.get(b.building_type_id)
                if disc and b.building_id is not None and b.building_id not in seen:
                    seen.add(b.building_id)
                    out[disc].append(b.building_id)
            if any(out.values()):
                return out
        # Fallback: the alliance-buildings scrape (keyword discipline).
        seen = set()
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_ACADEMY_LIST_PAGES):
            html = await self.client.fetch_page(path)
            for listing in parse_alliance_buildings_page(html):
                if listing.discipline in out and listing.building_id not in seen:
                    seen.add(listing.building_id)
                    out[listing.discipline].append(listing.building_id)
            nxt = find_next_page_path(html)
            if not nxt:
                break
            path = nxt
        return out


def _overview_guide(min_rate: float) -> str:
    """The STABLE overview post, worded like the old bot's Training Request
    Guide. Starts with :data:`GUIDE_MARKER` so it's never re-parsed as a
    request; the course lists live in the per-agency posts below it."""
    return "\n".join([
        GUIDE_MARKER,
        "[b]Training Request Guide[/b]",
        "",
        "This post is maintained automatically by the Fire & Rescue Academy bot.",
        "",
        "[b]How to request[/b]",
        "- Type one or more training names from the agency posts below.",
        "- You can request multiple classes in one post, one per line or "
        "separated by commas.",
        "- Want several copies of one course? Prefix a count: 3x HazMat "
        "(maximum 4 per request).",
        "- Small typos are supported, but the exact names below work best.",
        "- Some trainings exist in more than one academy type. For those, use "
        "the prefixed text shown in the agency posts.",
        "- Example: use Fire Station - Lifeguard Training or Water Rescue - "
        "Lifeguard Training, not only Lifeguard Training.",
        "- Requests are opened as free alliance classes, 1 class per "
        "recognized training.",
        "- A class stays open to the whole alliance for 1 hour to join.",
        f"- If your alliance contribution is below {min_rate:g}%, the class "
        "will not be opened automatically.",
        "",
        "[b]Discord requests[/b]",
        "You can also request trainings through the Discord request panel "
        f"or /training: up to {MAX_CLASSES_PER_REQUEST} classes of the same "
        f"course at once (each class holds {CLASS_CAPACITY} people), with an "
        "optional reminder when the course should be finished.",
        "",
        "[b]Guide posts[/b]",
        "The bot keeps one post per agency below with the exact names to use.",
        "",
        "[b]Examples[/b]",
        "HazMat",
        "SWAT, K-9",
        "Fire Station - Lifeguard Training",
        "Water Rescue - Lifeguard Training",
    ])


def _discipline_guide(key: str) -> str:
    """One agency's request-text post (the old bot's per-agency guide):
    every course with its duration, with the disambiguating prefix spelled
    out for courses that exist in several academy types."""
    ambiguous = _ambiguous_course_names()
    prefix = _AGENCY_PREFIX[key]
    lines = [
        _section_marker(key),
        f"[b]{_AGENCY_TITLES[key]} trainings[/b]",
        "",
        "Use one of these names in this topic to request a class:",
    ]
    for name, days in sorted(DISCIPLINES.get(key, {}).items()):
        unit = "day" if days == 1 else "days"
        if name in ambiguous:
            lines.append(f"- {prefix} - {name} ({days} {unit}) - opens {name}")
        else:
            lines.append(f"- {name} ({days} {unit})")
    return "\n".join(lines)
