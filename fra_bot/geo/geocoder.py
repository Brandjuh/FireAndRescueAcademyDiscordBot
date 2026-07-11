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
import re
import time
import urllib.parse
from dataclasses import dataclass

import aiohttp

from ..db.repos import StateRepo
from .maps_links import (
    MapsLocation,
    extract_location_from_html,
    is_short_link,
    parse_maps_url,
)

log = logging.getLogger(__name__)

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_USER_AGENT = "FireAndRescueAcademyBot/1.0 (alliance admin tooling; contact via Discord)"
_MIN_INTERVAL = 1.1  # Nominatim policy: max 1 req/s


def _simplify_query(query: str) -> str:
    """Strip punctuation that commonly differs between colloquial names and
    OSM's ("St. Olav's University Hospital" → "St Olavs University Hospital")."""
    simplified = query.replace("+", " ")
    for char in ".'’`\"":
        simplified = simplified.replace(char, "")
    return " ".join(simplified.split())


_NEAR_RE = re.compile(r"\s+(?:near|by|around|close to)\s+", re.IGNORECASE)


def _near_fallbacks(query: str) -> list[str]:
    """Fallback queries for relative descriptions: "X near Y" → try "X",
    then "Y" — geocoders can find either alone but not the combination."""
    parts = _NEAR_RE.split(query, maxsplit=1)
    if len(parts) != 2:
        return []
    return [p.strip(" ,") for p in parts if p.strip(" ,")]


class GeocodeError(RuntimeError):
    """A geocoding lookup failed. ``transient`` marks errors worth retrying
    (network blips, rate limits, 5xx); auth (401/403) and 'not found' are
    permanent until config/input changes."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    address: str | None
    source: str  # url | nominatim_search | nominatim_reverse
    # The place NAME the member pointed at (from the maps link / search
    # query), kept separately from the reverse-geocoded street address.
    place_text: str | None = None
    # The OSM feature type at the location (e.g. "hospital", "prison",
    # "clinic") — the authoritative signal for classifying the place.
    place_type: str | None = None


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
        expanded, body = url, None
        if is_short_link(url):
            expanded, body = await self._expand_short_link(url)

        location = parse_maps_url(expanded)
        if not location.has_coordinates and not location.place_text and body:
            # The short link did not redirect to a parseable Maps URL —
            # Google sometimes hands bots an interstitial page (HTTP 200)
            # with the real URL/coordinates only inside the HTML.
            location = extract_location_from_html(body)
        if location.has_coordinates:
            address, place_type = None, None
            try:
                address, place_type = await self.reverse(
                    location.latitude, location.longitude
                )
            except GeocodeError as exc:
                log.warning("Reverse geocode failed for %s: %s", url, exc)
            return GeocodeResult(
                latitude=location.latitude,
                longitude=location.longitude,
                address=address or location.place_text,
                source="url",
                place_text=location.place_text,
                place_type=place_type,
            )

        if location.place_text:
            return await self.search(location.place_text)

        detail = f"No coordinates or place name found in {url}"
        if expanded != url:
            detail += f" (expanded to {expanded})"
        elif body is not None:
            detail += " (the short link did not redirect; interstitial page?)"
        raise GeocodeError(detail)

    #: Browser UA for the GOOGLE short-link fetch only — Google hands bot
    #: user agents an interstitial page instead of the redirect. Nominatim
    #: keeps the identifying UA its usage policy requires.
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    async def _expand_short_link(self, url: str) -> tuple[str, str | None]:
        """Follow a short link to ``(final url, html body or None)``. The
        body travels along so the caller can mine an interstitial page
        when the redirect chain didn't reach a parseable Maps URL."""
        assert self._session is not None
        try:
            async with self._session.get(
                url, allow_redirects=True,
                headers={"User-Agent": self._BROWSER_UA},
            ) as resp:
                body = None
                if "text/html" in (resp.headers.get("Content-Type") or ""):
                    try:
                        body = (await resp.text())[:500_000]
                    except (aiohttp.ClientError, UnicodeDecodeError):
                        body = None
                return str(resp.url), body
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
            # Names copied out of URLs or typed by members often carry
            # punctuation OSM doesn't use ("St. Olav's" vs "St Olavs") —
            # one retry with the punctuation stripped rescues those.
            cleaned = _simplify_query(query)
            if cleaned and cleaned != query:
                data = await self._nominatim(
                    "/search", {"q": cleaned, "format": "jsonv2", "limit": "1"}
                )
        if not data:
            # Members describe places relatively ("X near Y") — geocoders
            # don't parse that. Try each side of the "near", most specific
            # first, so "Okanogan-Wenatchee National Forest near Yakima, WA"
            # still lands in the right area.
            for part in _near_fallbacks(query):
                data = await self._nominatim(
                    "/search", {"q": part, "format": "jsonv2", "limit": "1"}
                )
                if data:
                    break
        if not data:
            raise GeocodeError(f"Nominatim found nothing for {query!r}")
        result = GeocodeResult(
            latitude=float(data[0]["lat"]),
            longitude=float(data[0]["lon"]),
            address=data[0].get("display_name"),
            source="nominatim_search",
            place_text=query,
            place_type=data[0].get("type"),
        )
        await self._cache_set("search", query, result)
        return result

    async def reverse(
        self, latitude: float, longitude: float
    ) -> tuple[str | None, str | None]:
        """Return (address, OSM feature type) for a coordinate."""
        key = f"{latitude:.5f},{longitude:.5f}"
        cached = await self._cache_get("reverse", key)
        if cached is not None:
            return cached.get("address"), cached.get("place_type")

        data = await self._nominatim(
            "/reverse",
            {"lat": str(latitude), "lon": str(longitude), "format": "jsonv2"},
        )
        address = data.get("display_name") if isinstance(data, dict) else None
        place_type = data.get("type") if isinstance(data, dict) else None
        await self._cache_set(
            "reverse",
            key,
            GeocodeResult(
                latitude, longitude, address, "nominatim_reverse", place_type=place_type
            ),
        )
        return address, place_type

    async def reverse_details(
        self, latitude: float, longitude: float
    ) -> dict | None:
        """The structured address parts (state, country, country_code, …)
        at a coordinate — what the event pinger maps to a region role.
        Returns None when the geocoder has no address there."""
        key = f"{latitude:.5f},{longitude:.5f}"
        cached = await self._cache_get("reverse_details", key)
        if cached is not None:
            return cached or None

        data = await self._nominatim(
            "/reverse",
            {
                "lat": str(latitude),
                "lon": str(longitude),
                "format": "jsonv2",
                "addressdetails": "1",
            },
        )
        details = data.get("address") if isinstance(data, dict) else None
        if not isinstance(details, dict):
            details = None
        await self._state.set(
            f"geocode/reverse_details/{key}", json.dumps(details or {})
        )
        return details

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
        host = urllib.parse.urlparse(self._base_url).netloc or self._base_url
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    hint = ""
                    if resp.status in (401, 403):
                        hint = (
                            " — the geocoding provider rejected the request; "
                            "check GEOCODER_API_KEY in .env (and geocoding.base_url)"
                        )
                    raise GeocodeError(
                        f"geocoder {host} returned HTTP {resp.status} for {path}{hint}",
                        status=resp.status,
                        transient=resp.status in (429, 500, 502, 503, 504),
                    )
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise GeocodeError(
                f"geocoder {host} request failed: {exc}", transient=True
            ) from exc

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
                    "place_text": result.place_text,
                    "place_type": result.place_type,
                }
            ),
        )
