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

from ..config import Config
from ..db.database import Database
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.academy import (
    AcademyListing,
    find_next_page_path,
    parse_academy_page,
    parse_alliance_buildings_page,
)
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
MAX_ACADEMY_LIST_PAGES = 10
ALLIANCE_SIGNUP_SECONDS = 3600  # class stays open to the alliance for 1h
BOARD_FEE = 0                   # board classes are free
RETRY_MINUTES = 15              # backoff between busy retries (bounded by MAX_ATTEMPTS)

GUIDE_MARKER = "[FRA] 📋 How to request a TRAINING"

# Friendly discipline headings for the guide, in display order.
_DISCIPLINE_TITLES = {
    "fire": "🚒 Fire",
    "police": "🚓 Police",
    "ems": "🚑 EMS",
    "coastal": "🌊 Water Rescue",
}


class TrainingsService(BoardRequestService):
    kind = "training"
    guide_marker = GUIDE_MARKER

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        super().__init__(cfg, client, db)
        self._auto = cfg.automation.training

    @property
    def thread_id(self) -> int:
        return self._auto.thread_id

    def guide_body(self) -> str:
        return _training_guide(self._auto.min_contribution_rate)

    async def guide_content(self, now_epoch: float) -> str:
        """Static guide + a live "free classrooms per academy type" block +
        the last-updated line. Expensive (walks the academy pages) — the base
        only invokes this once the guide throttle has decided to write, never
        on the quiet polls in between."""
        from ..mc.board import guide_updated_line

        availability = await self._collect_availability()
        return "\n".join([
            self.guide_body(), "",
            _availability_block(availability), "",
            guide_updated_line(now_epoch),
        ])

    async def _collect_availability(self) -> dict[str, int] | None:
        """Free classrooms per discipline across all alliance academies.

        Returns ``None`` if the building list can't be read (shown as
        "temporarily unavailable"); a per-academy page failure just omits that
        academy's rooms."""
        try:
            listings: list[AcademyListing] = []
            path = ALLIANCE_BUILDINGS_PATH
            for _ in range(MAX_ACADEMY_LIST_PAGES):
                html = await self.client.fetch_page(path)
                listings.extend(parse_alliance_buildings_page(html))
                nxt = find_next_page_path(html)
                if not nxt:
                    break
                path = nxt
        except MissionChiefError as exc:
            log.warning("training availability: could not read academy list: %s", exc)
            return None

        counts = {key: 0 for key in _DISCIPLINE_TITLES}
        seen: set[int] = set()
        for listing in listings:
            if listing.discipline not in counts or listing.building_id in seen:
                continue
            seen.add(listing.building_id)
            try:
                page = parse_academy_page(
                    await self.client.fetch_page(f"/buildings/{listing.building_id}")
                )
            except MissionChiefError as exc:
                log.debug("training availability: academy %s failed (%s)",
                          listing.building_id, exc)
                continue
            counts[listing.discipline] += max(0, page.available_rooms)
        return counts

    async def parse_request(self, post: BoardPost) -> dict | None:
        matches, ambiguous = match_trainings(post.content)
        if not matches and not ambiguous:
            return None  # chatter, not a training request
        return self.request_data(
            post,
            {
                "trainings": [
                    {"discipline": m.discipline, "name": m.name, "duration": m.duration_days}
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
            # Retry: only the trainings still marked busy.
            matches = [
                TrainingMatch(discipline=t["discipline"], name=t["name"], duration_days=0)
                for t in payload["pending_trainings"]
            ]
            ambiguous: list[AmbiguousMatch] = []
        else:
            matches = [
                TrainingMatch(
                    discipline=t["discipline"], name=t["name"],
                    duration_days=t.get("duration", 0),
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
                await self.reply(
                    f"@{request['requester_name']}: your training request was not "
                    f"processed — your alliance contribution is {rate:g}%, the "
                    f"minimum is {self._auto.min_contribution_rate:g}%."
                )
                return

        await self._process(
            request["id"], request["requester_name"], matches, ambiguous, announce=announce
        )

    async def _process(
        self,
        request_id: int,
        requester: str | None,
        matches: list[TrainingMatch],
        ambiguous: list[AmbiguousMatch],
        *,
        announce: bool,
    ) -> None:
        lines: list[str] = []
        results: list[dict] = []
        opened_any = False
        pending: list[dict] = []  # transient failures worth retrying

        for ambiguity in ambiguous:
            lines.append(self._ambiguity_help(ambiguity))

        for match in matches:
            outcome = await self._open_training(match)
            results.append({"training": match.name, "outcome": outcome["status"]})
            if outcome["status"] == "opened":
                opened_any = True
                lines.append(
                    f"✅ {match.name}: class opened in academy "
                    f"{outcome['building_id']} — "
                    f"https://www.missionchief.com/buildings/{outcome['building_id']} "
                    f"(free, join within 1 hour)"
                )
            elif outcome["status"] == "uncertain":
                opened_any = True
                lines.append(
                    f"⚠️ {match.name}: submitted to academy "
                    f"{outcome['building_id']} but I couldn't confirm it opened — "
                    "please double-check."
                )
            elif outcome["status"] == "busy":
                pending.append(
                    {"discipline": match.discipline, "name": match.name}
                )
                lines.append(f"⏳ {match.name}: {outcome['reason']} (will retry)")
            else:  # failed
                lines.append(f"❌ {match.name}: {outcome['reason']}")

        next_attempt_at: str | None = None
        bump = False
        if pending:
            status = "waiting"
            detail = "retrying: " + ", ".join(p["name"] for p in pending)
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
        elif matches:
            status = "failed"
            detail = "; ".join(f"{r['training']}: {r['outcome']}" for r in results)
        else:
            status = "skipped"
            detail = "only ambiguous names found"

        await self.requests.set_status(
            request_id,
            status,
            detail,
            payload=json.dumps(
                {"results": results, "pending_trainings": pending}
            ),
            next_attempt_at=next_attempt_at,
            bump_attempts=bump,
            announce=announce or status in ("done", "failed"),
        )

        if lines and (announce or opened_any):
            await self.reply(
                f"Training request from {requester}:\n" + "\n".join(lines)
            )

    # ------------------------------------------------------------------

    def _ambiguity_help(self, ambiguity: AmbiguousMatch) -> str:
        options = ", ".join(sorted(ambiguity.disciplines))
        return (
            f"⚠️ \"{ambiguity.name}\" exists in multiple academy types "
            f"({options}). Please repost with a prefix, e.g. "
            f"\"Fire - {ambiguity.name}\" or \"Water Rescue - {ambiguity.name}\"."
        )

    async def _open_training(self, match: TrainingMatch) -> dict:
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
                        "alliance[duration]": str(ALLIANCE_SIGNUP_SECONDS),
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
        """Alliance academies for a discipline, preferred building first."""
        listings: list[AcademyListing] = []
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_ACADEMY_LIST_PAGES):
            html = await self.client.fetch_page(path)
            listings.extend(parse_alliance_buildings_page(html))
            next_path = find_next_page_path(html)
            if not next_path:
                break
            path = next_path

        candidates = [
            listing
            for listing in listings
            if listing.discipline == discipline and listing.has_start_button
        ]
        preferred_id = self._auto.preferred_academies.get(discipline)
        candidates.sort(key=lambda a: 0 if a.building_id == preferred_id else 1)
        if not candidates and preferred_id:
            # List scrape failed us; still try the known building.
            candidates = [
                AcademyListing(
                    building_id=preferred_id,
                    name=f"Preferred {discipline} academy",
                    discipline=discipline,
                    has_start_button=True,
                )
            ]
        return candidates


def _training_guide(min_rate: float) -> str:
    """The STABLE how-to-request post for the training board. Starts with
    :data:`GUIDE_MARKER` (so it's never re-parsed as a request) and uses forum
    BBCode headers. The caller appends live availability + a last-updated line."""
    lines = [
        GUIDE_MARKER,
        "",
        "[b]How to request[/b]",
        "Post the training name on its own line. Want several? Put one per line.",
        "",
        "[b]Copy an example[/b]",
        "HazMat",
        "SWAT",
        "Lifeguard Training",
        "",
        "[b]Same course in two academies?[/b]",
        "Put the academy in front so it opens in the right one:",
        "Fire - Lifeguard Training",
        "Water Rescue - Lifeguard Training",
        "",
        "[b]Good to know[/b]",
        "- Classes open FREE, one class per recognised course.",
        "- A class stays open to the whole alliance for 1 hour to sign up.",
        f"- Members below {min_rate:g}% alliance contribution are skipped.",
        "",
        "[b]Courses you can request[/b]",
    ]
    for key, title in _DISCIPLINE_TITLES.items():
        courses = DISCIPLINES.get(key) or {}
        if courses:
            lines.append(f"{title}: {', '.join(sorted(courses))}")
    return "\n".join(lines)


def _availability_block(counts: dict[str, int] | None) -> str:
    """The live "free classrooms per academy type" section for the guide."""
    lines = ["[b]Free classrooms right now[/b]"]
    if counts is None:
        lines.append("Temporarily unavailable — I'll refresh this shortly.")
        return "\n".join(lines)
    for key, title in _DISCIPLINE_TITLES.items():
        lines.append(f"{title}: {counts.get(key, 0)}")
    return "\n".join(lines)
