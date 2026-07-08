"""OpenStreetMap Overpass: find real hospital / prison locations to build on.

The daily auto-build places one real hospital and one real prison somewhere
in the world. We pick a random city from the worldwide pool, ask Overpass
for the hospitals/prisons around it, and build at one of those real
coordinates — rather than an arbitrary point in a city.

Only the query builder and the response parser are pure (and unit-tested);
the network fetch is a thin wrapper so tests can feed canned JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Tags that mean the facility isn't a live one to build on.
_DISUSED_TAG_KEYS = ("disused:amenity", "abandoned:amenity", "historic")
_INACTIVE_NAME_TERMS = ("former ", "abandoned", "disused", "ruins", "ruin ",
                        "memorial", "museum")


class OverpassError(RuntimeError):
    """Overpass was unreachable or returned an error/!ok status."""


@dataclass(frozen=True)
class OsmCandidate:
    building_type: str  # "hospital" | "prison"
    name: str
    latitude: float
    longitude: float
    address: str | None
    source_id: str


def candidate_type_from_tags(tags: dict) -> str | None:
    """Map OSM tags to our building type, or None if unsupported."""
    amenity = str(tags.get("amenity") or "").casefold()
    healthcare = str(tags.get("healthcare") or "").casefold()
    if amenity == "prison":
        return "prison"
    if amenity == "hospital" or healthcare == "hospital":
        return "hospital"
    return None


def _address_from_tags(tags: dict) -> str | None:
    street = " ".join(
        part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part
    ).strip()
    parts = [
        street,
        tags.get("addr:postcode"),
        tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
        tags.get("addr:state") or tags.get("addr:province"),
        tags.get("addr:country"),
    ]
    text = ", ".join(str(p).strip() for p in parts if str(p or "").strip())
    return text or None


def _is_inactive(tags: dict, name: str) -> bool:
    if any(tags.get(key) for key in _DISUSED_TAG_KEYS):
        return True
    lowered = name.casefold()
    return any(term in lowered for term in _INACTIVE_NAME_TERMS)


def build_candidate_query(
    south: float, west: float, north: float, east: float,
    building_type: str = "both",
) -> str:
    """Overpass QL for hospitals and/or prisons in a bounding box."""
    if south >= north:
        raise ValueError("south latitude must be lower than north")
    if west >= east:
        raise ValueError("west longitude must be lower than east")
    for lat in (south, north):
        if not -90 <= lat <= 90:
            raise ValueError("latitude must be between -90 and 90")
    for lng in (west, east):
        if not -180 <= lng <= 180:
            raise ValueError("longitude must be between -180 and 180")
    bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    want = str(building_type or "both").casefold().strip()
    clauses: list[str] = []
    if want in {"both", "all", "hospital", "hospitals"}:
        clauses.append(f'  nwr["amenity"="hospital"]({bbox});')
        clauses.append(f'  nwr["healthcare"="hospital"]({bbox});')
    if want in {"both", "all", "prison", "prisons", "jail", "jails"}:
        clauses.append(f'  nwr["amenity"="prison"]({bbox});')
    if not clauses:
        raise ValueError("building_type must be 'hospital', 'prison', or 'both'")
    return "\n".join(["[out:json][timeout:180];", "(", *clauses, ");", "out center tags;"])


def parse_candidates(data: dict, *, want: str | None = None) -> list[OsmCandidate]:
    """Turn an Overpass JSON response into clean, buildable candidates.

    Drops elements without a supported tag, a usable name, valid coordinates,
    or that look disused/historic. ``want`` filters to one building type.
    """
    out: list[OsmCandidate] = []
    for element in data.get("elements") or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        btype = candidate_type_from_tags(tags)
        if btype is None or (want and btype != want):
            continue
        name = str(
            tags.get("name") or tags.get("official_name")
            or tags.get("operator") or tags.get("brand") or ""
        ).strip()
        if not name or _is_inactive(tags, name):
            continue
        lat, lon = element.get("lat"), element.get("lon")
        if (lat is None or lon is None) and isinstance(element.get("center"), dict):
            lat = element["center"].get("lat")
            lon = element["center"].get("lon")
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        out.append(
            OsmCandidate(
                building_type=btype, name=name, latitude=lat, longitude=lon,
                address=_address_from_tags(tags),
                source_id=f"{element.get('type')}/{element.get('id')}",
            )
        )
    return out


class OverpassClient:
    """Thin async wrapper over an Overpass endpoint (the only network part)."""

    def __init__(self, *, url: str = DEFAULT_OVERPASS_URL, timeout: float = 90.0) -> None:
        self._url = url
        self._timeout = timeout

    async def fetch(self, query: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(self._url, data={"data": query}) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        raise OverpassError(f"Overpass HTTP {resp.status}: {body}")
                    return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise OverpassError(f"Overpass request failed: {exc}") from exc
