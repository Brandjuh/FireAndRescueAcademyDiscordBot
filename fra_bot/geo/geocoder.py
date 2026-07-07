"""Geocoding: Google Maps link → coordinates + address.

Strategy, cheapest first:

1. Expand short links by following redirects (no API needed).
2. Read coordinates straight from the expanded URL — covers nearly all
   Google Maps share links.
3. Only when a link carries just a place name, forward-geocode it via
   OSM Nominatim. Reverse geocoding turns coordinates into a street
   address for naming buildings.

Nominatim usage policy is respected: identifying User-Agent, max 1
request/second, and results are cached in the database forever (a
street address for fixed coordinates doesn't go stale).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass

import aiohttp

from ..db.repos import StateRepo
from .maps_links import MapsLocation, is_short_link, parse_maps_url

log = logging.getLogger(__name__)

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_USER_AGENT = "FireAndRescueAcademyBot/1.0 (alliance admin tooling; contact via Discord)"
_MIN_INTERVAL = 1.1  # Nominatim policy: max 1 req/s


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    address: str | None
    source: str  # url | nominatim_search | nominatim_reverse


class Geocoder:
    """Shared geocoder with its own polite pacing and DB-backed cache.

    Talks to any Nominatim-compatible endpoint. With no ``api_key`` it uses
    free OSM Nominatim; set ``base_url`` + ``api_key`` (e.g. maps.co,
    LocationIQ) to use your own quota. Google Maps links never need a key —
    coordinates are read straight from the expanded URL.
    """

    def __init__(
        self,
        state: StateRepo,
        *,
        base_url: str = NOMINATIM_BASE,
        api_key: str = "",
        api_key_param: str = "api_key",
        contact_email: str = "",
        min_interval: float = _MIN_INTERVAL,
    ) -> None:
        self._state = state
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._base_url = (base_url or NOMINATIM_BASE).rstrip("/")
        self._api_key = api_key or ""
        self._api_key_param = api_key_param or "api_key"
        self._min_interval = min_interval
        self._user_agent = (
            f"FireAndRescueAcademyBot/1.0 ({contact_email})"
            if contact_email
            else _USER_AGENT
        )

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._user_agent},
                timeout=aiohttp.ClientTimeout(total=30),
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------

    async def resolve_maps_link(self, url: str) -> GeocodeResult:
        """Google Maps link → coordinates (+ address when available)."""
        await self.start()
        expanded = url
        if is_short_link(url):
            expanded = await self._expand_short_link(url)

        location = parse_maps_url(expanded)
        if location.has_coordinates:
            address = None
            try:
                address = await self.reverse(location.latitude, location.longitude)
            except GeocodeError as exc:
                log.warning("Reverse geocode failed for %s: %s", url, exc)
            return GeocodeResult(
                latitude=location.latitude,
                longitude=location.longitude,
                address=address or location.place_text,
                source="url",
            )

        if location.place_text:
            return await self.search(location.place_text)

        raise GeocodeError(f"No coordinates or place name found in {url}")

    async def _expand_short_link(self, url: str) -> str:
        assert self._session is not None
        try:
            async with self._session.get(url, allow_redirects=True) as resp:
                return str(resp.url)
        except aiohttp.ClientError as exc:
            raise GeocodeError(f"Could not expand short link {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # Nominatim
    # ------------------------------------------------------------------

    async def search(self, query: str) -> GeocodeResult:
        cached = await self._cache_get("search", query)
        if cached is not None:
            return GeocodeResult(**cached)

        data = await self._nominatim(
            "/search", {"q": query, "format": "jsonv2", "limit": "1"}
        )
        if not data:
            raise GeocodeError(f"Nominatim found nothing for {query!r}")
        result = GeocodeResult(
            latitude=float(data[0]["lat"]),
            longitude=float(data[0]["lon"]),
            address=data[0].get("display_name"),
            source="nominatim_search",
        )
        await self._cache_set("search", query, result)
        return result

    async def reverse(self, latitude: float, longitude: float) -> str | None:
        key = f"{latitude:.5f},{longitude:.5f}"
        cached = await self._cache_get("reverse", key)
        if cached is not None:
            return cached.get("address")

        data = await self._nominatim(
            "/reverse",
            {"lat": str(latitude), "lon": str(longitude), "format": "jsonv2"},
        )
        address = data.get("display_name") if isinstance(data, dict) else None
        await self._cache_set(
            "reverse",
            key,
            GeocodeResult(latitude, longitude, address, "nominatim_reverse"),
        )
        return address

    def _geocode_url(self, path: str, params: dict[str, str]) -> str:
        """Build the request URL, injecting the API key when configured."""
        query = dict(params)
        if self._api_key:
            query[self._api_key_param] = self._api_key
        return f"{self._base_url}{path}?{urllib.parse.urlencode(query)}"

    async def _nominatim(self, path: str, params: dict[str, str]):
        assert self._session is not None
        async with self._lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        url = self._geocode_url(path, params)
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    raise GeocodeError(f"Nominatim HTTP {resp.status} for {path}")
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise GeocodeError(f"Nominatim request failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Cache (scraper_state keyspace: geocode/<kind>/<key>)
    # ------------------------------------------------------------------

    async def _cache_get(self, kind: str, key: str) -> dict | None:
        raw = await self._state.get(f"geocode/{kind}/{key}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def _cache_set(self, kind: str, key: str, result: GeocodeResult) -> None:
        await self._state.set(
            f"geocode/{kind}/{key}",
            json.dumps(
                {
                    "latitude": result.latitude,
                    "longitude": result.longitude,
                    "address": result.address,
                    "source": result.source,
                }
            ),
        )
