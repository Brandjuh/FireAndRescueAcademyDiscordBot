"""Discord-panel academy builds at a fixed address (overlap allowed).

A member with the configured role clicks Fire / Police / Rescue / Coastal in
the academy panel; the bot builds that academy type at ONE fixed address
(duplicates allowed — no dedup), naming it ``[AA] <label> #N`` where N is the
next number found by scanning the alliance's LIVE building list. When alliance
funds are below the floor the build is QUEUED (an ``automation_requests`` row,
``kind="academy"``) and retried by the queue poller until funds recover.
Honours the global ``dry_run`` (reports what it would build, spends nothing).

This is a hospital/prison build minus the geocode-from-link, dedup and OSM
machinery: fixed location, fixed type per button, custom name. It reuses the
proven :class:`BuildingsService` browser builder, live-funds read and geocoder
rather than duplicating them.
"""

from __future__ import annotations

import json
import logging
import re

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..db.repos import AutomationRepo
from ..geo.geocoder import GeocodeError
from ..mc.browser_builder import BrowserBuilder, BrowserUnavailable
from ..mc.errors import MissionChiefError
from ..mc.parsers.academy import find_next_page_path, parse_alliance_buildings_page

log = logging.getLogger(__name__)

KIND = "academy"
ALLIANCE_BUILDINGS_PATH = "/verband/gebauede"
MAX_LIST_PAGES = 12
# Transient build/geocode failures are bounded; a funds-wait does NOT bump
# attempts, so a low-funds build can wait as long as it needs to.
MAX_ATTEMPTS = 8

# Button key → build-type key (must exist in BUILDING_TYPE_IDS), display label
# and emoji. The name is "[AA] <label> #N".
# build_key must match a BUILDING_TYPE_IDS key (the lowercased form label);
# label is the display name used in "[AA] <label> #N".
ACADEMIES: dict[str, dict[str, str]] = {
    "fire":    {"build_key": "fire academy",          "label": "Fire academy",          "emoji": "🚒"},
    "police":  {"build_key": "police academy",        "label": "Police academy",        "emoji": "🚓"},
    "rescue":  {"build_key": "rescue (ems) academy",  "label": "Rescue academy",        "emoji": "🚑"},
    "coastal": {"build_key": "coastal rescue school", "label": "Coastal Rescue school", "emoji": "🌊"},
}


class AcademyService:
    def __init__(self, cfg: Config, db: Database, buildings) -> None:
        self.cfg = cfg
        # Reuse the building service's browser builder, live-funds read and
        # geocoder (an academy build is a building build minus dedup/geocode).
        self._buildings = buildings
        self.requests = AutomationRepo(db)
        self._auto = cfg.automation.academy
        self._coords: tuple[float, float, str] | None = None
        self._geocoded_for: str | None = None

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    @property
    def client(self):
        return self._buildings.client

    # -- enqueue + drain -------------------------------------------------

    async def enqueue(
        self, academy_kind: str, *, requester_name: str | None,
        discord_user_id: int | None, channel_id: int | None,
    ) -> int:
        """Create a pending academy-build request; returns its id."""
        if academy_kind not in ACADEMIES:
            raise ValueError(f"unknown academy kind {academy_kind!r}")
        payload = json.dumps({
            "academy": academy_kind,
            "discord_user_id": discord_user_id,
            "channel_id": channel_id,
        })
        return await self.requests.create(
            kind=KIND, thread_id=0, post_id=int(discord_user_id or 0),
            requester_name=requester_name, requester_mc_id=discord_user_id,
            payload=payload,
        )

    async def run_one(self, request_id: int) -> aiosqlite.Row | None:
        """Execute one just-enqueued request NOW (the button's immediate
        feedback), and return its row afterwards."""
        request = await self.requests.get(request_id)
        if request is not None and request["status"] == "pending":
            await self._claim_and_run(request)
        return await self.requests.get(request_id)

    async def process_queue(self) -> int:
        """Execute every claimable academy build (fresh + due funds-waits).
        Driven by the scheduled poller so a queued low-funds build resumes
        automatically once funds recover."""
        rows = await self.requests.claimable(KIND)
        for request in rows:
            await self._claim_and_run(request)
        return len(rows)

    async def _claim_and_run(self, request: aiosqlite.Row) -> None:
        request_id = request["id"]
        if request["attempts"] >= MAX_ATTEMPTS:
            if await self.requests.claim(request_id):
                await self.requests.set_status(
                    request_id, "failed",
                    f"gave up after {request['attempts']} failed attempts",
                )
            return
        first_attempt = request["status"] == "pending"
        if not await self.requests.claim(request_id):
            return  # another poll / the immediate kick won the claim
        try:
            await self._execute(request, announce=first_attempt)
        except MissionChiefError as exc:
            await self.requests.set_status(
                request_id, "waiting",
                f"MissionChief error ({exc}); will retry",
                bump_attempts=True, announce=False,
            )
        except Exception:  # noqa: BLE001 — one build must not crash the loop
            log.exception("academy build %s crashed", request_id)
            current = await self.requests.get(request_id)
            if current is not None and current["status"] == "processing":
                await self.requests.set_status(
                    request_id, "failed", "internal error while building",
                )

    async def _execute(self, request: aiosqlite.Row, *, announce: bool) -> None:
        request_id = request["id"]
        payload = json.loads(request["payload"] or "{}")
        spec = ACADEMIES.get(payload.get("academy"))
        if spec is None:
            await self.requests.set_status(
                request_id, "failed", f"unknown academy {payload.get('academy')!r}",
            )
            return

        # dry-run / no browser: say what WOULD be built, spend nothing.
        if self.dry_run or not BrowserBuilder.available():
            reason = "dry-run" if self.dry_run else "no browser available on the host"
            name = await self._next_name(spec)
            await self.requests.set_status(
                request_id, "skipped",
                f"[{reason}] would build {name} at {self._auto.address}",
            )
            return

        # Funds gate → queue when low (never spend below the floor).
        funds, funds_error = await self._buildings._live_funds()
        if funds is None:
            await self.requests.set_status(
                request_id, "waiting",
                f"could not read alliance funds ({funds_error}); will retry",
                bump_attempts=True, announce=False,
            )
            return
        if funds < self._auto.min_alliance_funds:
            await self.requests.set_status(
                request_id, "waiting",
                f"alliance funds {funds:,} below floor "
                f"{self._auto.min_alliance_funds:,}; queued until funds recover",
                announce=announce,
            )
            return

        coords = await self._coordinates()
        if coords is None:
            await self.requests.set_status(
                request_id, "waiting",
                f"could not geocode {self._auto.address!r}; will retry",
                bump_attempts=True, announce=False,
            )
            return
        latitude, longitude, address = coords
        name = await self._next_name(spec)

        try:
            result = await self._buildings._builder.build(
                building_type=spec["build_key"],
                latitude=latitude, longitude=longitude,
                name=name, address=address,
            )
        except BrowserUnavailable as exc:
            await self.requests.set_status(
                request_id, "waiting",
                f"browser unavailable ({exc}); will retry",
                bump_attempts=True, announce=False,
            )
            return

        merged = dict(payload)
        merged["name"] = name
        if result.ok:
            if result.building_id:
                merged["building_id"] = result.building_id
            detail = (
                f"built {name} (#{result.building_id})" if result.building_id
                else f"built {name} (submitted — {result.detail})"
            )
            await self.requests.set_status(
                request_id, "done", detail, payload=json.dumps(merged), announce=True,
            )
        else:
            await self.requests.set_status(
                request_id, "failed", f"{name}: {result.detail}",
                payload=json.dumps(merged), announce=True,
            )

    # -- helpers ---------------------------------------------------------

    async def _coordinates(self) -> tuple[float, float, str] | None:
        """Geocode the configured address and cache it, keyed on the address
        so a live ``!fra set automation.academy.address`` re-geocodes instead
        of silently building at the old spot (Nominatim is also DB-cached, so
        an unchanged address stays a single lookup)."""
        address = self._auto.address
        if self._coords is not None and self._geocoded_for == address:
            return self._coords
        try:
            location = await self._buildings._geocoder.search(address)
        except GeocodeError as exc:
            log.warning("academy: could not geocode %r (%s)", address, exc)
            return None
        self._coords = (
            location.latitude, location.longitude,
            location.address or address,
        )
        self._geocoded_for = address
        return self._coords

    async def _next_name(self, spec: dict[str, str]) -> str:
        return f"[AA] {spec['label']} #{await self._next_number(spec)}"

    async def _next_number(self, spec: dict[str, str]) -> int:
        """Scan the alliance's LIVE building list for existing
        ``[AA] <label> #N`` and return ``max(N) + 1`` (1 when none)."""
        pattern = re.compile(
            r"^\s*\[AA\]\s*" + re.escape(spec["label"]) + r"\s*#(\d+)\s*$",
            re.IGNORECASE,
        )
        highest = 0
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_LIST_PAGES):
            html = await self.client.fetch_page(path)
            for listing in parse_alliance_buildings_page(html):
                match = pattern.match(listing.name or "")
                if match:
                    highest = max(highest, int(match.group(1)))
            nxt = find_next_page_path(html)
            if not nxt:
                break
            path = nxt
        return highest + 1
