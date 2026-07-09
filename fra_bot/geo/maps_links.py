"""Extract coordinates / place text from Google Maps links.

Members share locations as ``maps.app.goo.gl`` short links or full
Google Maps URLs. Most expanded links already carry coordinates, so an
external geocoding service is only needed as a fallback:

* ``!3d<lat>!4d<lng>`` — the actual pin position (most precise),
* ``@<lat>,<lng>,<zoom>z`` — the viewport centre (good fallback),
* ``?q=<lat>,<lng>`` / ``?ll=<lat>,<lng>`` — query-style links,
* ``/maps/place/<text>/`` — place name, needs forward geocoding.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

MAPS_LINK_RE = re.compile(
    r"https?://(?:"
    r"maps\.app\.goo\.gl/[\w\-]+"
    r"|goo\.gl/maps/[\w\-]+"
    r"|(?:www\.)?google\.[a-z.]+/maps[^\s<>\"']*"
    r"|maps\.google\.[a-z.]+[^\s<>\"']*"
    r")",
    re.IGNORECASE,
)

_PIN_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
_QUERY_KEYS = ("q", "ll", "query", "center", "destination")
_LATLNG_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_PLACE_RE = re.compile(r"/maps/place/([^/@?]+)")


@dataclass(frozen=True)
class MapsLocation:
    latitude: float | None
    longitude: float | None
    place_text: str | None

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def find_maps_links(text: str) -> list[str]:
    """All Google Maps links found in a blob of text."""
    return [match.group(0).rstrip(".,;)>]") for match in MAPS_LINK_RE.finditer(text)]


def _valid(lat: float, lng: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


def _unwrap_redirect(url: str) -> str:
    """Follow Google's consent/redirect wrappers to the real Maps URL.

    Mobile short links often expand to ``consent.google.com/...?continue=<url>``
    (EU cookie consent) or ``google.com/url?q=<url>``; the actual Maps URL —
    with its coordinates — is percent-encoded inside a query parameter.
    """
    for _ in range(3):
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        if "consent.google." in host or parsed.path.startswith("/url"):
            query = urllib.parse.parse_qs(parsed.query)
            target = next(
                (v[0] for k in ("continue", "url", "q") for v in [query.get(k)] if v),
                None,
            )
            if target and target.startswith(("http://", "https://")):
                url = target
                continue
        break
    return url


def parse_maps_url(url: str) -> MapsLocation:
    """Extract what we can from an (expanded) Google Maps URL."""
    url = _unwrap_redirect(url)
    lat = lng = None

    # Pin coordinates beat viewport coordinates. Coordinates can hide behind
    # percent-encoding (e.g. ``!3d`` as ``%213d`` inside a wrapped URL), so
    # retry each pattern on the decoded string before giving up on it.
    decoded = urllib.parse.unquote(url)
    for pattern in (_PIN_RE, _AT_RE):
        match = pattern.search(url) or pattern.search(decoded)
        if match is not None:
            cand_lat, cand_lng = float(match.group(1)), float(match.group(2))
            if _valid(cand_lat, cand_lng):
                lat, lng = cand_lat, cand_lng
                break

    if lat is None:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        for key in _QUERY_KEYS:
            for value in query.get(key, []):
                qmatch = _LATLNG_RE.match(value)
                if qmatch:
                    cand_lat, cand_lng = float(qmatch.group(1)), float(qmatch.group(2))
                    if _valid(cand_lat, cand_lng):
                        lat, lng = cand_lat, cand_lng
                        break
            if lat is not None:
                break

    place_text = None
    place = _PLACE_RE.search(url)
    if place:
        place_text = _clean_place_text(urllib.parse.unquote_plus(place.group(1)))
    else:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("q", "query", "destination"):
            for value in query.get(key, []):
                if value and not _LATLNG_RE.match(value):
                    place_text = _clean_place_text(value)
                    break
            if place_text:
                break

    return MapsLocation(latitude=lat, longitude=lng, place_text=place_text)


def _clean_place_text(text: str) -> str | None:
    """Normalise a place name pulled out of a URL for geocoding.

    Double-encoded links leave literal ``+`` separators behind after one
    round of unquoting ("St.+Olav's+University+Hospital") — Nominatim then
    searches for the plus signs verbatim and finds nothing.
    """
    if "%" in text:
        text = urllib.parse.unquote(text)
    text = text.replace("+", " ")
    return " ".join(text.split()) or None


def is_short_link(url: str) -> bool:
    lowered = url.lower()
    return "maps.app.goo.gl" in lowered or "goo.gl/maps" in lowered
