"""Hospital / prison building from board requests (thread 6165).

Flow per new post: find a Google Maps link → geocode to coordinates +
address → detect hospital vs prison from the address → check the LIVE
alliance funds floor → build via browser emulation → reply.

Safety:
* funds are read LIVE from /verband/kasse right before building; if the
  fetch fails or funds are below the floor, the request goes to
  ``waiting`` and is retried, never built blindly,
* without Playwright, or in dry-run, the request is recorded and the
  resolved location is reported for a human to build manually.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
from zoneinfo import ZoneInfo

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..geo.geocoder import GeocodeError, Geocoder
from ..geo.maps_links import find_maps_links
from ..geo.overpass import (
    OverpassClient,
    OverpassError,
    build_candidate_query,
    parse_candidates,
)
from ..geo.world_locations import random_world_location
from ..mc.browser_builder import BrowserBuilder, BrowserUnavailable, BuildResult
from ..mc.buildings_api import (
    BUILDING_TYPE_IDS as API_TYPE_IDS,
    haversine_meters,
    nearest_duplicate,
    parse_api_buildings,
)
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost
from ..mc.parsers.funds import parse_total_funds
from .board_requests import BoardRequestService

log = logging.getLogger(__name__)

KASSE_PATH = "/verband/kasse"
API_BUILDINGS_PATH = "/api/buildings"
# The daily build creates ALLIANCE buildings, which live on their own API
# endpoint — dedup must see both our personal and the alliance buildings.
API_ALLIANCE_BUILDINGS_PATH = "/api/alliance_buildings"

# Daily auto-build tuning.
AUTO_BUILD_TYPES = ("hospital", "prison")     # one of each, every day
DUPLICATE_RADIUS_M = 250                      # skip a spot within this of a same-type building
OVERPASS_BBOX_DELTA = 0.18                    # ~20 km box around a chosen city
MAX_CITY_ATTEMPTS = 6                         # cities to try before giving up on a type
DAILY_BUILD_STATE_KEY = "daily_build_last_date"
# Fresh builds the finisher keeps revisiting (prison extensions unlock
# only after the previous one finishes real-time construction).
PENDING_COMPLETION_KEY = "building_pending_completion"
COMPLETION_IDLE_LIMIT = 24    # consecutive no-progress checks (~12h at 30min)

# Post-build verification: an alliance build doesn't redirect to
# /buildings/<id>, so after a submit we look for a NEW same-type building
# near the pin on the building APIs. Loose radius is safe — pre-existing
# buildings are excluded by the before/after diff.
CONFIRM_RADIUS_M = 300.0
CONFIRM_ATTEMPTS = 3
CONFIRM_RETRY_SECONDS = 10.0

# OSM feature types (amenity/…) that ARE the building — the strongest,
# language-independent signal.
_HOSPITAL_OSM_TYPES = ("hospital",)
_PRISON_OSM_TYPES = ("prison", "jail")

# Name/address hints (secondary signal). Substring match, so "hospitalier"
# and "ziekenhuis" are covered.
_HOSPITAL_TERMS = ("hospital", "hospitalier", "hopital", "medical center",
                   "medical centre", "ziekenhuis")
_PRISON_TERMS = ("prison", "jail", "correctional", "penitentiary",
                 "detention", "remand", "gevangenis")

# Look-alikes that must NOT be built (unless the OSM tag confirms the type).
_HOSPITAL_REJECT = ("clinic", "clinique", "kliniek", "doctor", "physician",
                    "pharmacy", "apotheek", "urgent care", "medical office",
                    "veterin", "dental")
_PRISON_REJECT = ("courthouse", "court house", "police station",
                  "police department", "sheriff", "law office", "probation")

# Not an operating facility at all.
_INACTIVE_TERMS = ("museum", "memorial", "historic", "former ", "abandoned",
                   "ruins", "monument")


def _has_any(text: str, terms) -> bool:
    return any(term in text for term in terms)


def detect_building_type(
    address: str | None,
    place_text: str | None = None,
    place_type: str | None = None,
) -> str | None:
    """Classify a location as ``'hospital'`` / ``'prison'`` / ``None``.

    The OSM feature type (``place_type``, e.g. amenity=hospital/prison) is
    the strongest, language-independent signal; the place name/address is a
    weaker one; and clinic/police/inactive terms veto a false match unless
    the OSM tag confirms the type. Scored so conflicting signals resolve to
    ``None`` (refused) rather than guessing. Built fresh, mirroring the
    approach the reference bot converged on.
    """
    text = " ".join(t for t in (place_text, address) if t).lower()
    osm = (place_type or "").lower()
    if not text.strip() and not osm:
        return None
    if _has_any(text, _INACTIVE_TERMS):
        return None

    hospital_osm = osm in _HOSPITAL_OSM_TYPES
    prison_osm = osm in _PRISON_OSM_TYPES
    hospital_score = (3 if hospital_osm else 0) + (2 if _has_any(text, _HOSPITAL_TERMS) else 0)
    prison_score = (3 if prison_osm else 0) + (2 if _has_any(text, _PRISON_TERMS) else 0)

    # A look-alike name kills the score unless the OSM tag confirms the type.
    if _has_any(text, _HOSPITAL_REJECT) and not hospital_osm:
        hospital_score = 0
    if _has_any(text, _PRISON_REJECT) and not prison_osm:
        prison_score = 0

    if hospital_score and prison_score:
        if hospital_score > prison_score:
            return "hospital"
        if prison_score > hospital_score:
            return "prison"
        return None  # conflicting signals — refuse
    if hospital_score:
        return "hospital"
    if prison_score:
        return "prison"
    return None


GUIDE_MARKER = "[FRA] 📋 How to request a BUILDING"

_TYPE_EMOJI = {"hospital": "🏥", "prison": "🔒"}


def _reply_received(
    requester: str | None, request_id, building_type: str | None,
    name: str | None, coords: str | None, status_line: str,
) -> str:
    """The reference bot's 'request received' reply structure."""
    lines = [
        f"Building request received for {requester or 'member'}.",
        "",
        f"Request ID: {request_id}",
        f"Type: {building_type or 'unknown'}",
    ]
    if name:
        lines.append(f"Detected name: {name}")
    if coords:
        lines.append(f"Coordinates: {coords}")
    lines.append("")
    lines.append(status_line)
    return "\n".join(lines)


def _reply_error(requester: str | None, reason: str) -> str:
    """The reference bot's 'could not be processed' reply, verbatim."""
    return (
        f"Building request could not be processed for {requester or 'member'}.\n\n"
        f"Reason: {reason}\n\n"
        "Use one of these formats:\n"
        "Hospital: <Google Maps link>\n"
        "Prison: <Google Maps link>"
    )


def _building_guide(min_funds: int) -> str:
    """The how-to-request post for the building board, structured like the
    old bot's request guides. Starts with :data:`GUIDE_MARKER` so it's never
    re-parsed as a request; the base appends a "last updated" line."""
    return "\n".join([
        GUIDE_MARKER,
        "[b]Building Request Guide[/b]",
        "",
        "This post is maintained automatically by the Fire & Rescue "
        "Academy bot.",
        "",
        "[b]How to request[/b]",
        "- Post a Google Maps link to a REAL hospital or prison, on its "
        "own line.",
        "- I work out which it is from the pin and build it for the "
        "alliance.",
        "- Only hospitals and prisons are built automatically; clinics, "
        "doctors, police stations, courthouses and museums are refused.",
        f"- Nothing is built while alliance funds are below {min_funds:,} "
        "credits; the request waits until funds recover.",
        "- One link per post.",
        "",
        "[b]Examples[/b]",
        "https://maps.app.goo.gl/xxxxx",
        "https://www.google.com/maps/place/…",
    ])


class BuildingsService(BoardRequestService):
    kind = "building"
    guide_marker = GUIDE_MARKER

    def __init__(
        self,
        cfg: Config,
        client: MissionChiefClient,
        db: Database,
        geocoder: Geocoder,
    ) -> None:
        super().__init__(cfg, client, db)
        self._auto = cfg.automation.building
        self._geocoder = geocoder
        self._builder = BrowserBuilder(
            cfg.missionchief.base_url, self._playwright_cookies
        )
        self._overpass = OverpassClient()
        self._rng = random.Random()
        # Post-creation automation: tax + level + extensions on new builds.
        from .building_upgrade import BuildingUpgradeService

        self._upgrader = BuildingUpgradeService(cfg, client, db)

    @property
    def thread_id(self) -> int:
        return self._auto.thread_id

    def guide_body(self) -> str:
        return _building_guide(self._auto.min_alliance_funds)

    def _playwright_cookies(self) -> list[dict]:
        cookies = []
        for cookie in self.client.session.cookie_jar:
            cookies.append(
                {
                    "name": cookie.key,
                    "value": cookie.value,
                    "url": self.cfg.missionchief.base_url,
                }
            )
        return cookies

    async def parse_request(self, post: BoardPost) -> dict | None:
        links = find_maps_links(post.content)
        if not links:
            return None  # no location shared
        return self.request_data(post, {"link": links[0]})

    async def execute_request(self, request: aiosqlite.Row, *, announce: bool) -> None:
        from ..geo.geocoder import GeocodeResult

        payload = json.loads(request["payload"] or "{}")
        requester = request["requester_name"]

        if payload.get("latitude"):
            # Coordinates resolved on a prior attempt — reuse them.
            location = GeocodeResult(
                latitude=payload["latitude"],
                longitude=payload["longitude"],
                address=payload.get("address"),
                source="cache",
            )
            building_type = payload["building_type"]
        else:
            try:
                location = await self._geocoder.resolve_maps_link(payload.get("link"))
            except GeocodeError as exc:
                await self.requests.set_status(
                    request["id"], "failed", f"geocoding failed: {exc}"
                )
                await self.reply_for(request, _reply_error(
                    requester,
                    "The location could not be resolved to GPS coordinates. "
                    "Please use a Google Maps place link with a visible "
                    f"marker. ({exc})",
                ))
                return

            building_type = detect_building_type(
                location.address, location.place_text, location.place_type
            )
            payload.update(
                {
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "address": location.address,
                    "building_type": building_type,
                }
            )
            if building_type is None:
                # Members just drop a maps pin; only hospitals and prisons are
                # auto-built. Anything else is refused (not an error).
                await self.requests.set_status(
                    request["id"], "skipped",
                    "refused: location is not a hospital or prison",
                    payload=json.dumps(payload),
                )
                await self.reply_for(request, _reply_error(
                    requester,
                    f"{location.place_text or location.address or 'The pin'} "
                    "was not detected as a hospital or a prison. Only "
                    "hospitals and prisons are built automatically.",
                ))
                return

        await self._attempt_build(
            request, requester, building_type, location, payload, announce=announce
        )

    # ------------------------------------------------------------------

    async def _attempt_build(
        self,
        request,
        requester: str | None,
        building_type,
        location,
        payload: dict,
        *,
        announce: bool = True,
    ) -> None:
        request_id = request["id"]

        # A prior submit whose result couldn't be proven yet: ONLY verify —
        # never rebuild. A second submit could create a duplicate building.
        pending = payload.get("pending_confirm")
        if pending:
            await self._resolve_pending_confirm(request, requester, payload, pending)
            return

        # Dry-run (or browser-less) never spends credits, so it must not
        # block on the funds gate — resolve and give feedback immediately.
        # A transient kasse hiccup used to park these requests in 'waiting'
        # with no reply, exactly when members expect a quick answer.
        if self.dry_run or not BrowserBuilder.available():
            reason = "dry-run" if self.dry_run else "Playwright not installed"
            await self.requests.set_status(
                request_id, "skipped",
                f"{reason}: resolved to {building_type} at "
                f"{location.latitude:.5f},{location.longitude:.5f}",
                payload=json.dumps(payload),
            )
            await self.reply_for(request, _reply_received(
                requester, request_id, building_type,
                location.address.split(",")[0] if location.address else None,
                f"{location.latitude:.5f}, {location.longitude:.5f}",
                f"Resolved and recorded. [{reason} — an admin can build it "
                "manually for now]",
            ))
            return

        funds, funds_error = await self._live_funds()
        if funds is None:
            await self.requests.set_status(
                request_id, "waiting",
                f"could not read live alliance funds ({funds_error}); will retry",
                payload=json.dumps(payload), bump_attempts=True, announce=False,
            )
            return
        if funds < self._auto.min_alliance_funds:
            # A legitimate condition-wait: don't bump attempts (funds may
            # take a long time to recover — it must not hit the cap).
            await self.requests.set_status(
                request_id, "waiting",
                f"alliance funds {funds:,} below floor "
                f"{self._auto.min_alliance_funds:,}; waiting",
                payload=json.dumps(payload), announce=announce,
            )
            if announce:
                await self.reply_for(request, _reply_received(
                    requester, request_id, building_type,
                    location.address.split(",")[0] if location.address else None,
                    f"{location.latitude:.5f}, {location.longitude:.5f}",
                    "Queued for automatic build. No building was created yet. "
                    f"Current alliance funds are {funds:,} credits; minimum "
                    "required before auto-building is "
                    f"{self._auto.min_alliance_funds:,} credits.",
                ))
            return

        name = location.address.split(",")[0] if location.address else f"{building_type}"

        # Snapshot the same-type buildings near the pin BEFORE building, so
        # the after-the-fact API check can tell a genuinely new building
        # from one that was already there. The endpoint coverage travels
        # with the snapshot: a diff against a snapshot that silently MISSED
        # an endpoint would call every pre-existing building on it "new".
        before, before_coverage = await self._same_type_near(
            building_type, location.latitude, location.longitude
        )
        try:
            result = await self._builder.build(
                building_type=building_type,
                latitude=location.latitude,
                longitude=location.longitude,
                name=name,
                address=location.address,
            )
        except BrowserUnavailable as exc:
            await self.requests.set_status(
                request_id, "skipped", str(exc), payload=json.dumps(payload)
            )
            return

        if result.ok and result.building_id is None:
            # Alliance builds don't redirect to /buildings/<id>, so the
            # browser can't tell whether the game accepted the build. Only
            # the API can prove it — and only a proof may reach the member.
            payload["pending_confirm"] = {
                "building_type": building_type,
                "lat": location.latitude,
                "lng": location.longitude,
                "address": location.address,
                "before": sorted(before),
                "coverage": sorted(before_coverage),
                "submit_note": result.detail,
            }
            outcome, confirmed = await self._confirm_build(
                building_type, location.latitude, location.longitude,
                set(before), before_coverage,
            )
            if outcome == "confirmed":
                payload.pop("pending_confirm", None)
                result = confirmed
            else:
                # Not proven either way (API lag, unreachable API, or a
                # coverage gap). Park the request; the next poll re-enters
                # via pending_confirm and ONLY verifies — never rebuilds.
                await self.requests.set_status(
                    request_id, "waiting",
                    f"build submitted; API confirmation pending ({outcome})",
                    payload=json.dumps(payload), bump_attempts=True,
                    announce=False,
                )
                return

        if result.ok:
            await self._finish_build_done(
                request, requester, payload, building_type,
                location.address, result.building_id,
            )
        else:
            await self._finish_build_failed(
                request, requester, payload, building_type, result.detail
            )

    async def _resolve_pending_confirm(
        self, request, requester: str | None, payload: dict, pending: dict
    ) -> None:
        """Re-verify an earlier submit (minutes later — generous to API
        propagation lag). A terminal 'refused' requires a read window that
        actually saw the APIs and found nothing new; unreadable APIs keep
        the request waiting until MAX_ATTEMPTS ends it honestly."""
        building_type = pending["building_type"]
        outcome, confirmed = await self._confirm_build(
            building_type, float(pending["lat"]), float(pending["lng"]),
            set(pending.get("before") or []), set(pending.get("coverage") or []),
        )
        if outcome == "confirmed":
            payload.pop("pending_confirm", None)
            await self._finish_build_done(
                request, requester, payload, building_type,
                pending.get("address"), confirmed.building_id,
            )
            return
        if outcome == "absent":
            payload.pop("pending_confirm", None)
            note = pending.get("submit_note") or ""
            await self._finish_build_failed(
                request, requester, payload, building_type,
                "the game accepted the submit but no new building appeared "
                "on the APIs — MissionChief refused it silently. Check the "
                "bot account's alliance permissions"
                + (f" [{note}]" if note else ""),
            )
            return
        await self.requests.set_status(
            request["id"], "waiting",
            "build submitted; APIs unreadable — confirmation still pending",
            payload=json.dumps(payload), bump_attempts=True, announce=False,
        )

    async def _finish_build_done(
        self, request, requester: str | None, payload: dict,
        building_type: str, address: str | None, building_id: int | None,
    ) -> None:
        payload["building_id"] = building_id
        name = address.split(",")[0] if address else None
        coords = None
        if payload.get("latitude") is not None:
            coords = f"{payload['latitude']:.5f}, {payload['longitude']:.5f}"
        link = (
            f"https://www.missionchief.com/buildings/{building_id}"
            if building_id
            else None
        )
        await self.requests.set_status(
            request["id"], "done",
            f"built {building_type}"
            + (f" #{building_id}" if building_id else " (id unknown)"),
            payload=json.dumps(payload),
        )
        status_line = (
            "Building request approved and automatically created in "
            "MissionChief." + (f"\n{link}" if link else "")
        )
        await self.reply_for(request, _reply_received(
            requester, request["id"], building_type, name, coords, status_line,
        ))
        # Board requesters get the reference bot's personal notification as
        # an in-game PM (no Discord identity on a board post).
        if not self.is_discord_request(request):
            await self._notify_ingame_built(
                requester, building_type, name, coords, address, link
            )
        # Post-creation automation (reference-bot behaviour): tax to the
        # configured percentage, level to max, every extension except the
        # large one. Feedback lands as a follow-up board reply.
        note = await self._post_creation(building_id, building_type, name)
        if note:
            await self.reply_for(
                request,
                f"Post-creation for {building_type}"
                + (f" #{building_id}" if building_id else "")
                + f":\n{note}",
            )

    async def _post_creation(
        self, building_id: int | None, building_type: str, name: str | None
    ) -> str:
        """Tax + level-to-max + extensions (except large) for a NEW
        building, plus registration with the FINISHER: prisons reveal each
        next extension only after the previous one finishes CONSTRUCTION
        (real build time), so one pass can never complete a building — the
        periodic finisher keeps revisiting it until it is done. Returns a
        short human summary ('' when skipped)."""
        if not building_id or self.dry_run:
            return ""
        from ..mc.building_tax import set_building_tax

        notes: list[str] = []
        tax_ok = True
        percent = self._auto.set_tax_percent
        if percent:
            tax_ok, detail = await set_building_tax(self.client, building_id, percent)
            notes.append(("✅ " if tax_ok else "⚠️ ") + detail)
            if not tax_ok:
                log.warning("post-creation: %s #%s: %s", building_type,
                            building_id, detail)
        report = await self._upgrader.upgrade_one(
            building_id, kind=building_type, name=name,
        )
        notes.append(
            f"levels raised: {report.levels_raised} · "
            f"extensions bought: {report.extensions_bought}"
        )
        await self._register_completion(
            building_id, building_type, name, tax_done=tax_ok,
        )
        notes.append(
            "🔁 I will keep revisiting this building until it is fully "
            "upgraded (new extensions unlock as construction finishes)."
        )
        log.info("post-creation %s #%s: %s", building_type, building_id,
                 " | ".join(notes))
        return "\n".join(notes)

    # -- the finisher: complete fresh builds over time ---------------------

    async def _load_pending_completion(self) -> dict:
        raw = await self.state.get(PENDING_COMPLETION_KEY)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def _register_completion(
        self, building_id: int, kind: str, name: str | None, *, tax_done: bool,
    ) -> None:
        data = await self._load_pending_completion()
        data[str(building_id)] = {
            "kind": kind,
            "name": name,
            "tax_done": tax_done,
            "idle": 0,
            "added_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        await self.state.set(PENDING_COMPLETION_KEY, json.dumps(data))

    async def finish_pending(self) -> list[str]:
        """One finisher pass over every registered fresh build: retry the
        tax until it sticks, buy whatever new levels/extensions have become
        available, and retire a building after enough consecutive
        no-progress checks (construction done, nothing left to buy)."""
        if self.dry_run:
            return []
        data = await self._load_pending_completion()
        if not data:
            return []
        from ..mc.building_tax import set_building_tax

        lines: list[str] = []
        for key, entry in list(data.items()):
            building_id = int(key)
            progressed = False
            if not entry.get("tax_done") and self._auto.set_tax_percent:
                ok, detail = await set_building_tax(
                    self.client, building_id, self._auto.set_tax_percent
                )
                if ok:
                    entry["tax_done"] = True
                    progressed = True
                lines.append(f"#{building_id}: {'✅' if ok else '⚠️'} {detail}")
            report = await self._upgrader.upgrade_one(
                building_id, kind=entry.get("kind", "hospital"),
                name=entry.get("name"),
            )
            if report.actions > 0:
                progressed = True
                lines.append(
                    f"#{building_id}: +{report.levels_raised} levels, "
                    f"+{report.extensions_bought} extensions"
                )
            entry["idle"] = 0 if progressed else entry.get("idle", 0) + 1
            if entry["idle"] >= COMPLETION_IDLE_LIMIT and entry.get("tax_done"):
                del data[key]
                lines.append(f"#{building_id}: complete — finisher done")
                log.info("finisher: building %s considered complete", building_id)
        await self.state.set(PENDING_COMPLETION_KEY, json.dumps(data))
        for line in lines:
            log.info("building finisher: %s", line)
        return lines

    async def _notify_ingame_built(
        self, requester: str | None, building_type: str, name: str | None,
        coords: str | None, address: str | None, link: str | None,
    ) -> None:
        from ..mc.messages import send_ingame_message

        if not requester:
            return
        emoji = _TYPE_EMOJI.get(building_type, "🏢")
        lines = [
            "Your building request has been APPROVED.",
            "",
            f"{emoji} {building_type}: {name or address or 'your location'}",
        ]
        if coords:
            lines.append(f"📍 Coordinates: {coords}")
        if address:
            lines.append(f"📫 Address: {address}")
        if link:
            lines.append(f"Building: {link}")
        try:
            await send_ingame_message(
                self.client, requester, "Building request", "\n".join(lines)
            )
        except Exception:  # noqa: BLE001 - a PM must never fail the request
            log.exception("building: in-game PM to %s failed", requester)

    async def _finish_build_failed(
        self, request, requester: str | None, payload: dict,
        building_type: str, detail: str,
    ) -> None:
        name = (payload.get("address") or "").split(",")[0] or None
        coords = None
        if payload.get("latitude") is not None:
            coords = f"{payload['latitude']:.5f}, {payload['longitude']:.5f}"
        await self.requests.set_status(
            request["id"], "failed",
            f"build failed: {detail} — ADMIN ACTION NEEDED",
            payload=json.dumps(payload),
        )
        await self.reply_for(request, _reply_received(
            requester, request["id"], building_type, name, coords,
            "Building request approved, but automatic creation needs staff "
            "follow-up. Admins have been notified.",
        ))

    async def _live_funds(self) -> tuple[int | None, str | None]:
        """Live alliance funds from /verband/kasse, as ``(funds, error)``.

        Exactly one side is set — the error string says WHY the read failed
        (fetch error vs. unparseable page) so waiting requests and daily
        summaries can show the real reason instead of a bare "could not
        read". When the plain HTML carries no funds figure (parts of the
        kasse page are drawn by JavaScript), a browser-rendered fetch is
        tried before giving up — the reference bot needed the same fallback.
        """
        try:
            html = await self.client.fetch_page(KASSE_PATH)
        except MissionChiefError as exc:
            log.warning("Building funds check failed: %s", exc)
            return None, str(exc)
        funds = parse_total_funds(html)
        if funds is not None:
            return funds, None
        if BrowserBuilder.available():
            from ..mc.browser_builder import render_page

            try:
                rendered = await render_page(
                    self.cfg.missionchief.base_url,
                    self._playwright_cookies(),
                    KASSE_PATH,
                )
                funds = parse_total_funds(rendered)
                if funds is not None:
                    log.info("Alliance funds read via rendered kasse page: %s", funds)
                    return funds, None
            except Exception as exc:  # noqa: BLE001 — report, never crash the poll
                log.warning("Rendered funds fetch failed: %s", exc)
                return None, (
                    "no funds figure in the plain kasse HTML and the rendered "
                    f"fetch failed ({exc})"
                )
        log.warning("No alliance funds figure found on %s", KASSE_PATH)
        return None, "no funds figure found on the kasse page — layout change?"

    async def _same_type_near(
        self, building_type: str, lat: float, lng: float
    ) -> tuple[dict, set]:
        """``(buildings, coverage)`` — same-type buildings within
        :data:`CONFIRM_RADIUS_M` of the pin from BOTH building APIs, keyed
        by id (or coords) as JSON-safe strings, each valued
        ``(building, endpoint)``. ``coverage`` records WHICH endpoints
        actually answered: a before/after diff is only trustworthy for
        endpoints present in both snapshots — treating a silently-missed
        endpoint as empty would make every pre-existing building on it
        look new."""
        from ..core.pacing import CircuitOpenError

        found: dict = {}
        coverage: set = set()
        type_id = API_TYPE_IDS.get(building_type)
        for path in (API_BUILDINGS_PATH, API_ALLIANCE_BUILDINGS_PATH):
            try:
                rows = parse_api_buildings(await self.client.fetch_page(path))
            except (MissionChiefError, CircuitOpenError, ValueError) as exc:
                log.warning("build confirm: could not read %s: %s", path, exc)
                continue
            coverage.add(path)
            for b in rows:
                if (
                    type_id is not None
                    and b.building_type_id is not None
                    and b.building_type_id != type_id
                ):
                    continue
                if haversine_meters(lat, lng, b.latitude, b.longitude) > CONFIRM_RADIUS_M:
                    continue
                key = (
                    str(b.building_id)
                    if b.building_id is not None
                    else f"{b.latitude:.5f},{b.longitude:.5f}"
                )
                found[key] = (b, path)
        return found, coverage

    async def _confirm_build(
        self,
        building_type: str,
        lat: float,
        lng: float,
        before_keys: set,
        before_coverage: set,
    ) -> tuple[str, BuildResult | None]:
        """Verdict on a submitted-but-unconfirmed build, via the APIs.

        Returns ``(outcome, result)``:

        * ``('confirmed', BuildResult)`` — a NEW same-type building near the
          pin, on an endpoint the before-snapshot covered. Proof it landed.
        * ``('absent', None)`` — endpoints read fine and show nothing new.
        * ``('unknown', None)`` — endpoints unreadable, or the only
          candidate sits on an endpoint the before-snapshot missed
          (indistinguishable from a pre-existing building). NEVER treated
          as a refusal — the caller re-checks later.
        """
        saw_reads = False
        ambiguous = False
        for attempt in range(CONFIRM_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CONFIRM_RETRY_SECONDS)
            after, coverage = await self._same_type_near(building_type, lat, lng)
            if not coverage:
                continue
            saw_reads = True
            provable = [
                b for key, (b, path) in after.items()
                if key not in before_keys and path in before_coverage
            ]
            if provable:
                provable.sort(
                    key=lambda b: haversine_meters(lat, lng, b.latitude, b.longitude)
                )
                winner = provable[0]
                log.info(
                    "build confirmed via API: %s #%s at %.5f,%.5f",
                    building_type, winner.building_id,
                    winner.latitude, winner.longitude,
                )
                return "confirmed", BuildResult(
                    True, winner.building_id, "created (confirmed via the API)"
                )
            if any(
                key not in before_keys and path not in before_coverage
                for key, (b, path) in after.items()
            ):
                ambiguous = True
        if not saw_reads or ambiguous:
            return "unknown", None
        return "absent", None

    async def test_build(
        self, building_type: str | None, location_text: str
    ) -> str:
        """Admin diagnostic: geocode a location and drive the build form for
        it, honouring dry_run (in dry-run it stops short of submitting). Runs
        the whole chain on demand — no board post needed. When
        ``building_type`` is None it's auto-detected from the address, like
        the board flow. Returns a summary."""
        from ..geo.geocoder import GeocodeError

        try:
            if find_maps_links(location_text):
                location = await self._geocoder.resolve_maps_link(location_text)
            else:
                location = await self._geocoder.search(location_text)
        except GeocodeError as exc:
            return f"❌ Geocoding failed: {exc}"

        lines = [
            f"📍 Resolved to **{location.address or 'unknown address'}** "
            f"({location.latitude:.5f}, {location.longitude:.5f})"
        ]
        if building_type is None:
            building_type = detect_building_type(
                location.address, location.place_text, location.place_type
            )
            if building_type is None:
                lines.append(
                    "🚫 Refused — not a hospital or prison. Only those are auto-built. "
                    "(Force it with `!fra testbuild hospital <location>`.)"
                )
                return "\n".join(lines)
            lines.append(f"🏗️ Detected type: **{building_type}**")
        funds, funds_error = await self._live_funds()
        if funds is not None:
            lines.append(
                f"💰 Alliance funds: {funds:,} (floor {self._auto.min_alliance_funds:,})"
            )
        else:
            lines.append(f"⚠️ Could not read alliance funds: {funds_error}")

        if not BrowserBuilder.available():
            lines.append(
                "⚠️ Playwright isn't installed, so the form can't be driven. "
                "Install it to test the browser build."
            )
            return "\n".join(lines)

        name = location.address.split(",")[0] if location.address else building_type
        before: set = set()
        before_coverage: set = set()
        if not self.dry_run:
            # Live test builds get the same API confirmation as the real
            # flow — a ✅ must mean PROVEN built, never just "submitted".
            found, before_coverage = await self._same_type_near(
                building_type, location.latitude, location.longitude
            )
            before = set(found)
        try:
            result = await self._builder.build(
                building_type=building_type,
                latitude=location.latitude,
                longitude=location.longitude,
                name=name,
                address=location.address,
                dry_run=self.dry_run,
            )
        except BrowserUnavailable as exc:
            lines.append(f"⚠️ {exc}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - surface it, don't crash the cog
            lines.append(f"❌ Browser build errored: {exc}")
            return "\n".join(lines)

        if result.ok and result.building_id is None and not self.dry_run:
            outcome, confirmed = await self._confirm_build(
                building_type, location.latitude, location.longitude,
                before, before_coverage,
            )
            if outcome == "confirmed":
                result = confirmed
            elif outcome == "absent":
                lines.append(
                    f"❌ [LIVE] submit accepted but NO new {building_type} "
                    f"appeared on the APIs — the game refused it ({result.detail})"
                )
                return "\n".join(lines)
            else:
                lines.append(
                    f"⚠️ [LIVE] submitted, but the APIs could not confirm it — "
                    f"check the map ({result.detail})"
                )
                return "\n".join(lines)

        detail = result.detail
        if result.building_id:
            detail += f" — https://www.missionchief.com/buildings/{result.building_id}"
        icon = "✅" if result.ok else "❌"
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        lines.append(f"{icon} [{mode}] {detail}")
        return "\n".join(lines)

    # -- daily worldwide auto-build -------------------------------------

    async def daily_build(self, *, force: bool = False) -> list[str]:
        """Build one hospital + one prison per day at real, deduped worldwide
        locations. Returns a per-building summary (also logged).

        Runs at most once per calendar day (in the reports timezone). Each
        build is gated on the live alliance funds floor — if funds are below
        it, that building is skipped until tomorrow (never dips the treasury).
        Honours ``dry_run``: reports what it would build without submitting.
        ``force`` (the manual `!fra dailybuild` trigger) bypasses the enabled
        switch and the once-a-day guard; it only consumes the day's slot when
        it actually built something for real."""
        if not force and not self._auto.daily_build_enabled:
            return []
        today = dt.datetime.now(ZoneInfo(self.cfg.reports.timezone)).strftime("%Y-%m-%d")
        if not force and await self.state.get(DAILY_BUILD_STATE_KEY) == today:
            log.debug("daily build already ran for %s; skipping", today)
            return []
        if not force:
            # Claim the day up front so a restart can't double-build.
            await self.state.set(DAILY_BUILD_STATE_KEY, today)

        run_id = await self.runs.start("daily_build")
        summary: list[str] = []
        built = 0
        try:
            existing = await self._existing_buildings()
            for building_type in AUTO_BUILD_TYPES:
                line = await self._auto_build_one(building_type, existing)
                summary.append(line)
                if line.startswith("✅"):
                    built += 1
            if force and built:
                # A real manual build uses up today's slot too.
                await self.state.set(DAILY_BUILD_STATE_KEY, today)
            await self.runs.finish(
                run_id, status="success", rows_parsed=len(AUTO_BUILD_TYPES),
                rows_new=built, message=" | ".join(summary)[:500],
            )
        except Exception as exc:  # noqa: BLE001 — a daily job must not crash the loop
            log.exception("daily build failed: %s", exc)
            await self.runs.finish(run_id, status="failed", message=str(exc))
        for line in summary:
            log.info("daily build: %s", line)
        return summary

    async def _auto_build_one(self, building_type: str, existing: list) -> str:
        funds, funds_error = await self._live_funds()
        if funds is None:
            return (
                f"⏳ {building_type}: could not read alliance funds "
                f"({funds_error}) — skipped today"
            )
        if funds < self._auto.min_alliance_funds:
            return (
                f"⏳ {building_type}: alliance funds {funds:,} below floor "
                f"{self._auto.min_alliance_funds:,} — skipped until tomorrow"
            )

        candidate = await self._find_osm_candidate(building_type, existing)
        if candidate is None:
            return (
                f"❔ {building_type}: no fresh real location found this run "
                "(all nearby ones already built, or Overpass unavailable)"
            )

        where = candidate.address or candidate.name
        coords = f"{candidate.latitude:.5f},{candidate.longitude:.5f}"
        if self.dry_run or not BrowserBuilder.available():
            reason = "dry-run" if self.dry_run else "Playwright not installed"
            return (
                f"📝 {building_type}: [{reason}] would build '{candidate.name}' "
                f"at {where} ({coords})"
            )

        before, before_coverage = await self._same_type_near(
            building_type, candidate.latitude, candidate.longitude
        )
        try:
            result = await self._builder.build(
                building_type=building_type,
                latitude=candidate.latitude,
                longitude=candidate.longitude,
                name=candidate.name,
                address=candidate.address,
            )
        except BrowserUnavailable as exc:
            return f"⚠️ {building_type}: {exc}"
        if result.ok and result.building_id is None:
            outcome, confirmed = await self._confirm_build(
                building_type, candidate.latitude, candidate.longitude,
                set(before), before_coverage,
            )
            if outcome == "confirmed":
                result = confirmed
            elif outcome == "absent":
                return (
                    f"❌ {building_type}: submit accepted but no new building "
                    f"appeared on the APIs — likely refused ({result.detail})"
                )
            else:
                return (
                    f"⚠️ {building_type}: submitted but could not verify via "
                    f"the APIs — check the map ({result.detail})"
                )
        if result.ok:
            # Fold the new building into the in-memory list so a same-type
            # second build this run won't land on top of it.
            existing.append(
                parse_api_buildings(
                    [{
                        "id": result.building_id,
                        "building_type": {"hospital": 2, "prison": 10}[building_type],
                        "latitude": candidate.latitude,
                        "longitude": candidate.longitude,
                    }]
                )[0]
            )
            link = (
                f"https://www.missionchief.com/buildings/{result.building_id}"
                if result.building_id
                else "(id unknown — see the alliance buildings list)"
            )
            note = await self._post_creation(
                result.building_id, building_type, candidate.name
            )
            suffix = f" · {note.replace(chr(10), ' · ')}" if note else ""
            return (
                f"✅ {building_type}: built '{candidate.name}' at {where} — "
                f"{link}{suffix}"
            )
        return f"❌ {building_type}: build failed — {result.detail}"

    async def _find_osm_candidate(self, building_type: str, existing: list):
        """Pick a random worldwide city, ask Overpass for real facilities of
        the type around it, drop ones within the dedup radius of an existing
        same-type building, and return a random survivor. Retries other
        cities. Returns an :class:`OsmCandidate` or None."""
        for _ in range(MAX_CITY_ATTEMPTS):
            city = random_world_location(self._rng)
            try:
                loc = await self._geocoder.search(city)
            except GeocodeError as exc:
                log.debug("daily build: geocode of %r failed (%s)", city, exc)
                continue
            south = max(-90.0, loc.latitude - OVERPASS_BBOX_DELTA)
            north = min(90.0, loc.latitude + OVERPASS_BBOX_DELTA)
            west = max(-180.0, loc.longitude - OVERPASS_BBOX_DELTA)
            east = min(180.0, loc.longitude + OVERPASS_BBOX_DELTA)
            if south >= north or west >= east:
                continue
            query = build_candidate_query(south, west, north, east, building_type)
            try:
                data = await self._overpass.fetch(query)
            except OverpassError as exc:
                log.warning("daily build: Overpass failed for %s (%s)", city, exc)
                continue
            fresh = [
                c
                for c in parse_candidates(data, want=building_type)
                if nearest_duplicate(
                    c.latitude, c.longitude, building_type, existing,
                    radius_m=DUPLICATE_RADIUS_M,
                ) is None
            ]
            if fresh:
                chosen = self._rng.choice(fresh)
                log.info(
                    "daily build: chose %s '%s' near %s (%d candidates, %d fresh)",
                    building_type, chosen.name, city,
                    len(parse_candidates(data, want=building_type)), len(fresh),
                )
                return chosen
        return None

    async def _existing_buildings(self) -> list:
        """Current buildings (with coords) for the proximity dedup: our own
        AND the alliance's — the daily build creates alliance buildings, so
        deduping against /api/buildings alone would let it stack a second
        facility next to one it built on an earlier day. Per-endpoint failures
        degrade to whatever did load — we'd rather build than block on a read
        error, and the game still rejects an exact overlap."""
        existing: list = []
        for path in (API_BUILDINGS_PATH, API_ALLIANCE_BUILDINGS_PATH):
            try:
                raw = await self.client.fetch_page(path)
            except MissionChiefError as exc:
                log.warning("daily build: could not read %s (%s); dedup partial",
                            path, exc)
                continue
            try:
                existing.extend(parse_api_buildings(raw))
            except (ValueError, TypeError) as exc:
                log.warning("daily build: could not parse %s (%s); dedup partial",
                            path, exc)
        return existing
