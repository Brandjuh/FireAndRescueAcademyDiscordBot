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
from ..mc.browser_builder import BrowserBuilder, BrowserUnavailable
from ..mc.buildings_api import nearest_duplicate, parse_api_buildings
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost
from ..mc.parsers.funds import parse_total_funds
from .board_requests import BoardRequestService

log = logging.getLogger(__name__)

KASSE_PATH = "/verband/kasse"
API_BUILDINGS_PATH = "/api/buildings"

# Daily auto-build tuning.
AUTO_BUILD_TYPES = ("hospital", "prison")     # one of each, every day
DUPLICATE_RADIUS_M = 250                      # skip a spot within this of a same-type building
OVERPASS_BBOX_DELTA = 0.18                    # ~20 km box around a chosen city
MAX_CITY_ATTEMPTS = 6                         # cities to try before giving up on a type
DAILY_BUILD_STATE_KEY = "daily_build_last_date"

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


class BuildingsService(BoardRequestService):
    kind = "building"

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

    @property
    def thread_id(self) -> int:
        return self._auto.thread_id

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
                await self.reply(
                    f"@{requester}: could not resolve your location link "
                    f"({exc}). Please share a Google Maps pin."
                )
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
                await self.reply(
                    f"@{requester}: that location "
                    f"({location.place_text or location.address or 'the pin'}) isn't a "
                    "hospital or a prison, so nothing was built. Only hospitals and "
                    "prisons are built automatically."
                )
                return

        await self._attempt_build(
            request["id"], requester, building_type, location, payload, announce=announce
        )

    # ------------------------------------------------------------------

    async def _attempt_build(
        self,
        request_id: int,
        requester: str | None,
        building_type,
        location,
        payload: dict,
        *,
        announce: bool = True,
    ) -> None:
        funds = await self._live_funds()
        if funds is None:
            await self.requests.set_status(
                request_id, "waiting",
                "could not read live alliance funds; will retry",
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
                await self.reply(
                    f"@{requester}: your {building_type} request is on hold — alliance "
                    f"funds ({funds:,}) are below the {self._auto.min_alliance_funds:,} "
                    "safety floor. I'll build it once funds recover."
                )
            return

        name = location.address.split(",")[0] if location.address else f"{building_type}"

        if self.dry_run or not BrowserBuilder.available():
            reason = "dry-run" if self.dry_run else "Playwright not installed"
            await self.requests.set_status(
                request_id, "skipped",
                f"{reason}: resolved to {building_type} at "
                f"{location.latitude:.5f},{location.longitude:.5f}",
                payload=json.dumps(payload),
            )
            await self.reply(
                f"@{requester}: {building_type} request resolved to "
                f"{location.address or 'the pin'} "
                f"({location.latitude:.5f}, {location.longitude:.5f}). "
                f"[{reason} — build it manually for now]"
            )
            return

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

        if result.ok:
            payload["building_id"] = result.building_id
            await self.requests.set_status(
                request_id, "done",
                f"built {building_type} #{result.building_id}",
                payload=json.dumps(payload),
            )
            await self.reply(
                f"✅ {building_type.capitalize()} built for {requester} at "
                f"{location.address or 'the pin'} — "
                f"https://www.missionchief.com/buildings/{result.building_id}"
            )
        else:
            await self.requests.set_status(
                request_id, "failed",
                f"build failed: {result.detail}", payload=json.dumps(payload),
            )
            await self.reply(
                f"@{requester}: I couldn't build the {building_type} automatically "
                f"({result.detail}). An admin will handle it."
            )

    async def _live_funds(self) -> int | None:
        try:
            html = await self.client.fetch_page(KASSE_PATH)
        except MissionChiefError as exc:
            log.warning("Building funds check failed: %s", exc)
            return None
        return parse_total_funds(html)

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
        funds = await self._live_funds()
        if funds is not None:
            lines.append(
                f"💰 Alliance funds: {funds:,} (floor {self._auto.min_alliance_funds:,})"
            )

        if not BrowserBuilder.available():
            lines.append(
                "⚠️ Playwright isn't installed, so the form can't be driven. "
                "Install it to test the browser build."
            )
            return "\n".join(lines)

        name = location.address.split(",")[0] if location.address else building_type
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
        ``force`` bypasses the once-a-day guard (for a manual trigger)."""
        if not self._auto.daily_build_enabled:
            return []
        today = dt.datetime.now(ZoneInfo(self.cfg.reports.timezone)).strftime("%Y-%m-%d")
        if not force and await self.state.get(DAILY_BUILD_STATE_KEY) == today:
            log.debug("daily build already ran for %s; skipping", today)
            return []
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
        funds = await self._live_funds()
        if funds is None:
            return f"⏳ {building_type}: could not read alliance funds — skipped today"
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
            return (
                f"✅ {building_type}: built '{candidate.name}' at {where} — "
                f"https://www.missionchief.com/buildings/{result.building_id}"
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
        """Our current buildings (with coords) for the proximity dedup. An
        empty list on failure — we'd rather build than block on a read error,
        and the game still rejects an exact overlap."""
        try:
            raw = await self.client.fetch_page(API_BUILDINGS_PATH)
        except MissionChiefError as exc:
            log.warning("daily build: could not read %s (%s); dedup skipped",
                        API_BUILDINGS_PATH, exc)
            return []
        try:
            return parse_api_buildings(raw)
        except (ValueError, TypeError) as exc:
            log.warning("daily build: could not parse %s (%s); dedup skipped",
                        API_BUILDINGS_PATH, exc)
            return []
