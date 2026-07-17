"""The alliance snapshot / fleet infographic cards: dark PNG images.

Composed for ``!infographic`` (header, stat tiles, buildings-by-type
bar chart, vehicles-by-type bar chart, hotspot map + top list) and
``!fleet`` (vehicle-focused card). Single accent colour, values
direct-labeled, text in ink tones — and like the static map, a missing
Pillow install degrades to None so the callers can fall back to text.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from .game_sync import Hotspot

log = logging.getLogger(__name__)

# Dark-surface palette; the accent passes the dataviz lightness band and
# the >= 3:1 contrast check against this surface.
_BG = (23, 24, 29)
_PANEL = (35, 36, 43)
_INK = (236, 236, 241)          # primary text
_INK_SOFT = (166, 168, 180)     # secondary text
_INK_MUTED = (110, 112, 128)
_ACCENT = (240, 82, 31)

_WIDTH = 1080
_PAD = 48                        # outer margin
_PANEL_PAD = 28
_RADIUS = 20


@dataclass(frozen=True)
class AllianceSnapshot:
    title: str
    date_label: str
    members_synced: int
    building_total: int
    vehicle_total: int
    top_types: list[tuple[str, int]] = field(default_factory=list)
    top_vehicle_types: list[tuple[str, int]] = field(default_factory=list)
    spots: list[Hotspot] = field(default_factory=list)
    map_png: bytes | None = None


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no size parameter
        return ImageFont.load_default()


def _header(draw, *, title: str, heading: str, date_label: str) -> int:
    y = _PAD
    draw.text((_PAD, y), title.upper(), font=_font(20), fill=_INK_MUTED)
    draw.text((_WIDTH - _PAD, y), date_label, font=_font(20),
              fill=_INK_SOFT, anchor="ra")
    y += 34
    draw.text((_PAD, y), heading, font=_font(44), fill=_INK)
    return y + 78


def _stat_tiles(draw, y: int, tiles: list[tuple[str, int]]) -> int:
    inner = _WIDTH - 2 * _PAD
    tile_gap = 20
    tile_width = (inner - (len(tiles) - 1) * tile_gap) // len(tiles)
    tile_height = 140
    for column, (label, number) in enumerate(tiles):
        x = _PAD + column * (tile_width + tile_gap)
        draw.rounded_rectangle(
            (x, y, x + tile_width, y + tile_height), radius=_RADIUS, fill=_PANEL
        )
        draw.rounded_rectangle(
            (x, y + 24, x + 5, y + tile_height - 24), radius=2, fill=_ACCENT
        )
        draw.text((x + _PANEL_PAD, y + 26), f"{number:,}",
                  font=_font(48), fill=_INK)
        draw.text((x + _PANEL_PAD, y + 92), label, font=_font(17),
                  fill=_INK_MUTED)
    return y + tile_height + 28


def _bar_panel(draw, y: int, title: str, rows: list[tuple[str, int]]) -> int:
    """A rounded panel with one single-hue horizontal bar per row and the
    value direct-labeled at the bar end; the new cursor y."""
    if not rows:
        return y
    bar_height, bar_gap = 26, 20
    panel_height = 2 * _PANEL_PAD + 40 + len(rows) * (bar_height + bar_gap)
    draw.rounded_rectangle(
        (_PAD, y, _WIDTH - _PAD, y + panel_height), radius=_RADIUS, fill=_PANEL
    )
    draw.text((_PAD + _PANEL_PAD, y + _PANEL_PAD), title,
              font=_font(18), fill=_INK_MUTED)
    label_font, value_font = _font(20), _font(20)
    label_col = max(
        int(draw.textlength(name.capitalize(), font=label_font))
        for name, _ in rows
    ) + 24
    bar_x = _PAD + _PANEL_PAD + label_col
    bar_room = (_WIDTH - _PAD - _PANEL_PAD) - bar_x - 90
    heaviest = max(count for _, count in rows) or 1
    row_y = y + _PANEL_PAD + 40
    for name, count in rows:
        middle = row_y + bar_height // 2
        draw.text((_PAD + _PANEL_PAD, middle), name.capitalize(),
                  font=label_font, fill=_INK_SOFT, anchor="lm")
        bar = max(6, int(bar_room * count / heaviest))
        draw.rounded_rectangle(
            (bar_x, row_y, bar_x + bar, row_y + bar_height),
            radius=4, fill=_ACCENT,
        )
        draw.text((bar_x + bar + 14, middle), f"{count:,}",
                  font=value_font, fill=_INK, anchor="lm")
        row_y += bar_height + bar_gap
    return y + panel_height + 28


def _finish(image, draw, y: int, footer: str) -> bytes:
    draw.text((_PAD, y + 4), footer, font=_font(16), fill=_INK_MUTED)
    image = image.crop((0, 0, _WIDTH, y + 30 + _PAD - 20))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


_FOOTER = "Data: members' own game sync (FRA profile sync)"


def render_infographic(snapshot: AllianceSnapshot) -> bytes | None:
    """The snapshot card as PNG bytes; None when Pillow is missing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("infographic: Pillow not installed; skipping render")
        return None

    inner = _WIDTH - 2 * _PAD
    image = Image.new("RGB", (_WIDTH, 2800), _BG)
    draw = ImageDraw.Draw(image, "RGBA")
    y = _header(draw, title=snapshot.title, heading="Alliance Game Snapshot",
                date_label=snapshot.date_label)
    y = _stat_tiles(draw, y, [
        ("MEMBERS SYNCED", snapshot.members_synced),
        ("BUILDINGS", snapshot.building_total),
        ("VEHICLES", snapshot.vehicle_total),
    ])
    y = _bar_panel(draw, y, "BUILDINGS BY TYPE", snapshot.top_types)
    y = _bar_panel(draw, y, "VEHICLES BY TYPE", snapshot.top_vehicle_types)

    # -- hotspots: map + top list -----------------------------------------
    if snapshot.map_png or snapshot.spots:
        map_image = None
        if snapshot.map_png:
            try:
                map_image = Image.open(io.BytesIO(snapshot.map_png)).convert("RGB")
            except Exception:  # noqa: BLE001 — a bad map never kills the card
                map_image = None
        list_rows = snapshot.spots[:3]
        map_height = 0
        if map_image is not None:
            map_width = inner - 2 * _PANEL_PAD
            map_height = int(map_image.height * map_width / map_image.width)
        panel_height = (
            2 * _PANEL_PAD + 40
            + map_height + (16 if map_image is not None and list_rows else 0)
            + len(list_rows) * 40
        )
        draw.rounded_rectangle(
            (_PAD, y, _WIDTH - _PAD, y + panel_height), radius=_RADIUS, fill=_PANEL
        )
        draw.text((_PAD + _PANEL_PAD, y + _PANEL_PAD), "HOTSPOTS",
                  font=_font(18), fill=_INK_MUTED)
        row_y = y + _PANEL_PAD + 40
        if map_image is not None:
            map_width = inner - 2 * _PANEL_PAD
            map_image = map_image.resize((map_width, map_height))
            mask = Image.new("L", map_image.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, map_width, map_height), radius=14, fill=255
            )
            image.paste(map_image, (_PAD + _PANEL_PAD, row_y), mask)
            row_y += map_height + 16
        rank_font, place_font = _font(20), _font(22)
        for rank, spot in enumerate(list_rows, 1):
            middle = row_y + 16
            draw.ellipse((_PAD + _PANEL_PAD, middle - 14,
                          _PAD + _PANEL_PAD + 28, middle + 14), fill=_ACCENT)
            draw.text((_PAD + _PANEL_PAD + 14, middle), str(rank),
                      font=rank_font, fill=_INK, anchor="mm")
            where = spot.place or f"{spot.latitude:.2f}, {spot.longitude:.2f}"
            draw.text((_PAD + _PANEL_PAD + 44, middle), where,
                      font=place_font, fill=_INK, anchor="lm")
            detail = (f"{spot.buildings:,} buildings · {spot.members} "
                      f"member{'s' if spot.members != 1 else ''}")
            draw.text((_WIDTH - _PAD - _PANEL_PAD, middle), detail,
                      font=_font(19), fill=_INK_SOFT, anchor="rm")
            row_y += 40
        y += panel_height + 28

    return _finish(image, draw, y, _FOOTER)


def render_fleet_card(
    *, title: str, date_label: str, members_synced: int, vehicle_total: int,
    type_count: int, top_vehicle_types: list[tuple[str, int]],
) -> bytes | None:
    """The vehicle-focused card (``!fleet``); None when Pillow is missing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("fleet card: Pillow not installed; skipping render")
        return None

    image = Image.new("RGB", (_WIDTH, 1600), _BG)
    draw = ImageDraw.Draw(image, "RGBA")
    y = _header(draw, title=title, heading="Alliance Fleet",
                date_label=date_label)
    y = _stat_tiles(draw, y, [
        ("VEHICLES", vehicle_total),
        ("VEHICLE TYPES", type_count),
        ("MEMBERS SYNCED", members_synced),
    ])
    y = _bar_panel(draw, y, "TOP VEHICLE TYPES", top_vehicle_types)
    return _finish(image, draw, y, _FOOTER)
