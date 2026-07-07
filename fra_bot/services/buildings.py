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

import json
import logging

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..geo.geocoder import GeocodeError, Geocoder
from ..geo.maps_links import find_maps_links
from ..mc.browser_builder import BrowserBuilder, BrowserUnavailable
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost
from ..mc.parsers.funds import parse_total_funds
from .board_requests import BoardRequestService

log = logging.getLogger(__name__)

KASSE_PATH = "/verband/kasse"

_HOSPITAL_TERMS = ("hospital", "medical center", "medical centre", "clinic", "ziekenhuis")
_PRISON_TERMS = ("prison", "jail", "correctional", "penitentiary", "detention", "gevangenis")


def detect_building_type(address: str | None, place_text: str | None) -> str | None:
    haystack = " ".join(t for t in (address, place_text) if t).lower()
    if not haystack:
        return None
    has_hospital = any(term in haystack for term in _HOSPITAL_TERMS)
    has_prison = any(term in haystack for term in _PRISON_TERMS)
    if has_hospital and not has_prison:
        return "hospital"
    if has_prison and not has_hospital:
        return "prison"
    return None  # ambiguous or neither


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

            building_type = detect_building_type(location.address, None)
            payload.update(
                {
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "address": location.address,
                    "building_type": building_type,
                }
            )
            if building_type is None:
                await self.requests.set_status(
                    request["id"], "failed",
                    "could not tell hospital from prison at this location",
                    payload=json.dumps(payload),
                )
                await self.reply(
                    f"@{requester}: I resolved the location "
                    f"({location.address or 'unknown address'}) but couldn't tell "
                    "whether it's a hospital or a prison. Please mention which one."
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
