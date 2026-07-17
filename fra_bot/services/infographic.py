"""The alliance snapshot infographic: one dark PNG card.

Composed for ``!infographic``: header, three stat tiles (synced
members / buildings / vehicles), a single-hue bar chart of the top
building types, the hotspot map and the top-hotspot list. Single
accent colour, values direct-labeled, text in ink tones — and like the
static map, a missing Pillow install degrades to None so the caller
can fall back to text.
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
    spots: list[Hotspot] = field(default_factory=list)
    map_png: bytes | None = None


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no size parameter
        return ImageFont.load_default()


def render_infographic(snapshot: AllianceSnapshot) -> bytes | None:
    """The snapshot card as PNG bytes; None when Pillow is missing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("infographic: Pillow not installed; skipping render")
        return None

    inner = _WIDTH - 2 * _PAD
    image = Image.new("RGB", (_WIDTH, 2200), _BG)
    draw = ImageDraw.Draw(image, "RGBA")
    y = _PAD

    # -- header ------------------------------------------------------------
    draw.text((_PAD, y), snapshot.title.upper(), font=_font(20), fill=_INK_MUTED)
    draw.text((_WIDTH - _PAD, y), snapshot.date_label, font=_font(20),
              fill=_INK_SOFT, anchor="ra")
    y += 34
    draw.text((_PAD, y), "Alliance Game Snapshot", font=_font(44), fill=_INK)
    y += 78

    # -- stat tiles --------------------------------------------------------
    tiles = [
        ("MEMBERS SYNCED", snapshot.members_synced),
        ("BUILDINGS", snapshot.building_total),
        ("VEHICLES", snapshot.vehicle_total),
    ]
    tile_gap = 20
    tile_width = (inner - 2 * tile_gap) // 3
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
    y += tile_height + 28

    # -- building types bar chart -----------------------------------------
    if snapshot.top_types:
        rows = snapshot.top_types
        bar_height, bar_gap = 26, 20
        panel_height = 2 * _PANEL_PAD + 40 + len(rows) * (bar_height + bar_gap)
        draw.rounded_rectangle(
            (_PAD, y, _WIDTH - _PAD, y + panel_height), radius=_RADIUS, fill=_PANEL
        )
        draw.text((_PAD + _PANEL_PAD, y + _PANEL_PAD), "BUILDINGS BY TYPE",
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
        y += panel_height + 28

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

    # -- footer ------------------------------------------------------------
    draw.text((_PAD, y + 4), "Data: members' own game sync (FRA profile sync)",
              font=_font(16), fill=_INK_MUTED)
    y += 30

    image = image.crop((0, 0, _WIDTH, y + _PAD - 20))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
