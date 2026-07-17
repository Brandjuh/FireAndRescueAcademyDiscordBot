"""Static map rendering: OSM tiles + numbered hotspot markers.

Used by ``!hotspots`` to attach a small overview map. Tiles come from
the public OSM tile server — occasional, admin-triggered use with an
identifying User-Agent and on-image attribution, per the tile usage
policy (https://operations.osmfoundation.org/policies/tiles/).

Everything degrades gracefully: any failure (tile server down, Pillow
missing) makes ``render_map`` return None and the caller falls back to
text-only output.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math

import aiohttp

from .geocoder import _USER_AGENT as USER_AGENT

log = logging.getLogger(__name__)

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SIZE = 256
_MAX_ZOOM = 11   # city level; higher would need more tiles for no benefit
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LAT_CAP = 85.05112878  # Web Mercator singularity guard


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
    session: aiohttp.ClientSession, zoom: int, x: int, y: int
) -> bytes | None:
    url = TILE_URL.format(z=zoom, x=x, y=y)
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None
            return await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def render_map(
    points: list[tuple[float, float, int]], *, width: int = 900, height: int = 600
) -> bytes | None:
    """A PNG map of ``(latitude, longitude, weight)`` markers, numbered in
    list order with marker size scaled by weight. None when rendering is
    impossible (no points, Pillow absent, no tile reachable)."""
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
    center_x = (min(xs) + max(xs)) / 2
    center_y = (min(ys) + max(ys)) / 2
    left = center_x - width / 2
    top = center_y - height / 2

    tile_x0 = math.floor(left / TILE_SIZE)
    tile_y0 = math.floor(top / TILE_SIZE)
    tile_x1 = math.floor((left + width) / TILE_SIZE)
    tile_y1 = math.floor((top + height) / TILE_SIZE)
    max_index = 2 ** zoom - 1

    image = Image.new("RGB", (width, height), (222, 222, 222))
    fetched = 0
    async with aiohttp.ClientSession(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as session:
        for tile_y in range(tile_y0, tile_y1 + 1):
            if tile_y < 0 or tile_y > max_index:
                continue
            for tile_x in range(tile_x0, tile_x1 + 1):
                # Longitude wraps; latitude does not.
                raw = await _fetch_tile(session, zoom, tile_x % (max_index + 1), tile_y)
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
    if fetched == 0:
        return None

    draw = ImageDraw.Draw(image, "RGBA")
    try:
        font = ImageFont.load_default(size=15)
    except TypeError:  # Pillow < 10.1 has no size parameter
        font = ImageFont.load_default()
    heaviest = max(weight for _, _, weight in points) or 1
    for index, ((px, py), (_, _, weight)) in enumerate(zip(pixels, points), 1):
        x = px - left
        y = py - top
        radius = 11 + 11 * math.sqrt(weight / heaviest)
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(192, 57, 43, 200), outline=(255, 255, 255, 255), width=2,
        )
        draw.text((x, y), str(index), font=font, fill=(255, 255, 255, 255),
                  anchor="mm")
    # Attribution is required by the OSM tile policy.
    draw.rectangle((width - 130, height - 18, width, height), fill=(255, 255, 255, 180))
    draw.text((width - 126, height - 16), "© OpenStreetMap",
              fill=(60, 60, 60, 255))

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
