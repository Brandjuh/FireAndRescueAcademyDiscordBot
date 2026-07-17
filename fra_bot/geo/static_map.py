"""Static map rendering: basemap tiles + numbered hotspot markers.

Used by ``!hotspots`` to attach a small overview map. The primary
basemap is CARTO "Dark Matter" (free basemap tiles, requires the
OSM + CARTO attribution drawn on the image) — a modern dark style that
matches Discord. When CARTO is unreachable the classic OSM tile server
is tried instead, each with an identifying User-Agent per the tile
usage policies.

Everything degrades gracefully: any failure (tile servers down, Pillow
missing) makes ``render_map`` return None and the caller falls back to
text-only output.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass

import aiohttp

from .geocoder import _USER_AGENT as USER_AGENT

log = logging.getLogger(__name__)

TILE_SIZE = 256
_MAX_ZOOM = 11   # city level; higher would need more tiles for no benefit
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LAT_CAP = 85.05112878  # Web Mercator singularity guard

#: Hotspot marker: warm "heat" orange with a soft two-step glow.
_MARKER_FILL = (255, 106, 61, 235)
_MARKER_GLOW_OUTER = (255, 106, 61, 26)
_MARKER_GLOW_INNER = (255, 106, 61, 52)
_MARKER_RING = (255, 255, 255, 230)


@dataclass(frozen=True)
class _Style:
    template: str
    background: tuple[int, int, int]     # fill behind missing tiles
    attribution: str
    attribution_fg: tuple[int, int, int, int]
    attribution_bg: tuple[int, int, int, int]


STYLES: tuple[_Style, ...] = (
    _Style(
        template="https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        background=(32, 32, 38),
        attribution="© OpenStreetMap © CARTO",
        attribution_fg=(190, 190, 198, 255),
        attribution_bg=(18, 18, 24, 190),
    ),
    _Style(
        template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        background=(222, 222, 222),
        attribution="© OpenStreetMap",
        attribution_fg=(60, 60, 60, 255),
        attribution_bg=(255, 255, 255, 190),
    ),
)


def _global_px(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator global pixel coordinates at ``zoom``."""
    scale = (2 ** zoom) * TILE_SIZE
    latitude = max(-_LAT_CAP, min(_LAT_CAP, latitude))
    x = (longitude + 180.0) / 360.0 * scale
    y = (1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2.0 * scale
    return x, min(max(y, 0.0), scale)


def pick_zoom(
    points: list[tuple[float, float]], width: int, height: int, *, pad: int = 60
) -> int:
    """The highest zoom (≤ city level) where all points fit the canvas."""
    for zoom in range(_MAX_ZOOM, 1, -1):
        xs, ys = zip(*(_global_px(lat, lng, zoom) for lat, lng in points))
        if (max(xs) - min(xs) <= width - 2 * pad
                and max(ys) - min(ys) <= height - 2 * pad):
            return zoom
    return 2


async def _fetch_tile(
    session: aiohttp.ClientSession, template: str, zoom: int, x: int, y: int
) -> bytes | None:
    url = template.format(z=zoom, x=x, y=y)
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None
            return await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def _paste_tiles(
    image, style: _Style, zoom: int, left: float, top: float,
) -> int:
    """Fetch and paste every tile covering the canvas; the count pasted."""
    from PIL import Image

    width, height = image.size
    tile_x0 = math.floor(left / TILE_SIZE)
    tile_y0 = math.floor(top / TILE_SIZE)
    tile_x1 = math.floor((left + width) / TILE_SIZE)
    tile_y1 = math.floor((top + height) / TILE_SIZE)
    max_index = 2 ** zoom - 1

    fetched = 0
    async with aiohttp.ClientSession(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as session:
        for tile_y in range(tile_y0, tile_y1 + 1):
            if tile_y < 0 or tile_y > max_index:
                continue
            for tile_x in range(tile_x0, tile_x1 + 1):
                # Longitude wraps; latitude does not.
                raw = await _fetch_tile(
                    session, style.template, zoom, tile_x % (max_index + 1), tile_y
                )
                if raw is None:
                    continue
                try:
                    tile = Image.open(io.BytesIO(raw)).convert("RGB")
                except Exception:  # noqa: BLE001 — corrupt tile, skip it
                    continue
                image.paste(
                    tile,
                    (int(tile_x * TILE_SIZE - left), int(tile_y * TILE_SIZE - top)),
                )
                fetched += 1
    return fetched


async def render_map(
    points: list[tuple[float, float, int]], *, width: int = 1000, height: int = 667
) -> bytes | None:
    """A PNG map of ``(latitude, longitude, weight)`` markers, numbered in
    list order with marker size scaled by weight. None when rendering is
    impossible (no points, Pillow absent, no tile server reachable)."""
    if not points:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # optional dependency — the text list still works
        log.warning("static map: Pillow not installed; skipping map render")
        return None

    zoom = pick_zoom([(lat, lng) for lat, lng, _ in points], width, height)
    pixels = [_global_px(lat, lng, zoom) for lat, lng, _ in points]
    xs, ys = zip(*pixels)
    left = (min(xs) + max(xs)) / 2 - width / 2
    top = (min(ys) + max(ys)) / 2 - height / 2

    image = style = None
    for candidate in STYLES:
        canvas = Image.new("RGB", (width, height), candidate.background)
        if await _paste_tiles(canvas, candidate, zoom, left, top) > 0:
            image, style = canvas, candidate
            break
        log.warning("static map: no tiles from %s", candidate.template)
    if image is None:
        return None

    draw = ImageDraw.Draw(image, "RGBA")
    try:
        font = ImageFont.load_default(size=15)
    except TypeError:  # Pillow < 10.1 has no size parameter
        font = ImageFont.load_default()
    heaviest = max(weight for _, _, weight in points) or 1
    # Reverse draw order: the list is sorted most-important-first, so when
    # cells overlap, marker 1 must end up ON TOP of marker 2, not under it.
    markers = list(enumerate(zip(pixels, points), 1))
    for index, ((px, py), (_, _, weight)) in reversed(markers):
        x = px - left
        y = py - top
        radius = 11 + 11 * math.sqrt(weight / heaviest)
        for factor, glow_fill in ((1.9, _MARKER_GLOW_OUTER), (1.45, _MARKER_GLOW_INNER)):
            glow = radius * factor
            draw.ellipse((x - glow, y - glow, x + glow, y + glow), fill=glow_fill)
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=_MARKER_FILL, outline=_MARKER_RING, width=2,
        )
        draw.text((x, y), str(index), font=font, fill=(255, 255, 255, 255),
                  anchor="mm")
    # Attribution is required by the OSM/CARTO tile policies.
    box_width = 16 + 6 * len(style.attribution)
    draw.rectangle(
        (width - box_width, height - 18, width, height), fill=style.attribution_bg
    )
    draw.text((width - box_width + 4, height - 16), style.attribution,
              fill=style.attribution_fg)

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
