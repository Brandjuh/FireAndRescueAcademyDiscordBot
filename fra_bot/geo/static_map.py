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

#: Hotspot marker: warm "heat" orange, drawn as a translucent bubble with
#: a solid rim over a smooth radial glow (kepler.gl-style graduated circles).
_MARKER_COLOUR = (240, 82, 31)
_BUBBLE_ALPHA = 72        # the basemap stays visible through the bubble
_GLOW_PEAK_ALPHA = 95     # glow alpha at its centre, fading to 0
_SS = 2                   # supersampling factor: markers render at 2x, then
                          # downscale with Lanczos for anti-aliased edges

_glow_sprite_cache = None


def _glow_sprite():
    """A cached greyscale radial-falloff sprite (quadratic ease-out), the
    alpha mask for marker glows — a real gradient, not stepped rings."""
    global _glow_sprite_cache
    if _glow_sprite_cache is None:
        from PIL import Image

        size = 128
        sprite = Image.new("L", (size, size), 0)
        pixels = sprite.load()
        centre = (size - 1) / 2
        for j in range(size):
            for i in range(size):
                distance = math.hypot(i - centre, j - centre) / centre
                pixels[i, j] = int(max(0.0, 1.0 - distance) ** 2 * 255)
        _glow_sprite_cache = sprite
    return _glow_sprite_cache


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

    # Markers render on a supersampled transparent overlay, then downscale
    # onto the map — ImageDraw has no anti-aliasing of its own, and jagged
    # circle edges are what makes a map look dated.
    overlay = Image.new("RGBA", (width * _SS, height * _SS), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.load_default(size=14 * _SS)
    except TypeError:  # Pillow < 10.1 has no size parameter
        font = ImageFont.load_default()
    heaviest = max(weight for _, _, weight in points) or 1
    # Reverse draw order: the list is sorted most-important-first, so when
    # cells overlap, marker 1 must end up ON TOP of marker 2, not under it.
    markers = list(enumerate(zip(pixels, points), 1))
    for index, ((px, py), (_, _, weight)) in reversed(markers):
        x = (px - left) * _SS
        y = (py - top) * _SS
        radius = (12.0 + 14.0 * math.sqrt(weight / heaviest)) * _SS
        glow_radius = int(radius * 2.1)
        sprite = _glow_sprite().resize((glow_radius * 2, glow_radius * 2))
        glow = Image.new("RGBA", sprite.size, _MARKER_COLOUR + (0,))
        glow.putalpha(sprite.point(lambda v: v * _GLOW_PEAK_ALPHA // 255))
        overlay.alpha_composite(
            glow, (int(x - glow_radius), int(y - glow_radius))
        )
        odraw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=_MARKER_COLOUR + (_BUBBLE_ALPHA,),
            outline=_MARKER_COLOUR + (255,), width=2 * _SS,
        )
        shadow = 1.5 * _SS  # soft shadow keeps the number readable anywhere
        odraw.text((x + shadow, y + shadow), str(index), font=font,
                   fill=(0, 0, 0, 140), anchor="mm")
        odraw.text((x, y), str(index), font=font,
                   fill=(255, 255, 255, 255), anchor="mm")
    overlay = overlay.resize((width, height), Image.LANCZOS)
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    # Attribution is required by the OSM/CARTO tile policies.
    draw = ImageDraw.Draw(image, "RGBA")
    box_width = 16 + 6 * len(style.attribution)
    draw.rounded_rectangle(
        (width - box_width - 6, height - 22, width - 6, height - 6),
        radius=8, fill=style.attribution_bg,
    )
    draw.text((width - box_width - 1, height - 19), style.attribution,
              fill=style.attribution_fg)

    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
