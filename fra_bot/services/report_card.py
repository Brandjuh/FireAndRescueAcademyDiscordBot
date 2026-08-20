"""The daily overview as ONE image card.

The morning used to scatter four separate messages across the reports
channel — member overview embed, admin overview embed, daily top-10
contributors, plus whatever was in ``reports.scheduled``. This renders
the member-facing half as a single card in the same visual language as
the alliance infographic and fleet cards, so the day arrives as one
picture with one short embed under it.

Pillow-optional, exactly like :mod:`fra_bot.services.infographic`: a
missing Pillow returns None and the caller falls back to the embed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .infographic import (
    _ACCENT,
    _BG,
    _INK,
    _INK_MUTED,
    _INK_SOFT,
    _PAD,
    _PANEL,
    _PANEL_PAD,
    _RADIUS,
    _WIDTH,
    _bar_panel,
    _finish,
    _font,
    _header,
)


@dataclass
class DailyCard:
    date_label: str
    title: str = "Fire & Rescue Academy"
    heading: str = "Daily overview"
    #: (label, value, sub) — sub is the trend line under the number.
    tiles: list[tuple[str, str, str]] = field(default_factory=list)
    top_earners: list[tuple[str, int]] = field(default_factory=list)
    top_donors: list[tuple[str, int]] = field(default_factory=list)
    activity: list[tuple[str, int]] = field(default_factory=list)
    footer: str = ""


def _value_tiles(draw, y: int, tiles: list[tuple[str, str, str]]) -> int:
    """Like infographic's stat tiles, but the value is pre-formatted text
    and each tile carries a trend line — the whole point of a daily card
    is "compared to what"."""
    inner = _WIDTH - 2 * _PAD
    gap = 20
    width = (inner - (len(tiles) - 1) * gap) // len(tiles)
    height = 150
    for column, (label, value, sub) in enumerate(tiles):
        x = _PAD + column * (width + gap)
        draw.rounded_rectangle(
            (x, y, x + width, y + height), radius=_RADIUS, fill=_PANEL
        )
        draw.rounded_rectangle(
            (x, y + 24, x + 5, y + height - 24), radius=2, fill=_ACCENT
        )
        # Shrink the value font until it fits the tile — a 12-digit credit
        # total must never run into the next tile.
        size = 44
        font = _font(size)
        room = width - 2 * _PANEL_PAD
        while size > 20 and draw.textlength(value, font=font) > room:
            size -= 3
            font = _font(size)
        draw.text((x + _PANEL_PAD, y + 24), value, font=font, fill=_INK)
        draw.text((x + _PANEL_PAD, y + 84), label, font=_font(17),
                  fill=_INK_MUTED)
        if sub:
            draw.text((x + _PANEL_PAD, y + 110), sub, font=_font(16),
                      fill=_INK_SOFT)
    return y + height + 28


def render_daily_card(card: DailyCard) -> bytes | None:
    """The daily overview card as PNG bytes; None when Pillow is missing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    # Tall canvas, cropped to the real content by _finish().
    image = Image.new("RGB", (_WIDTH, 1800), _BG)
    draw = ImageDraw.Draw(image)
    y = _header(
        draw, title=card.title, heading=card.heading,
        date_label=card.date_label,
    )
    if card.tiles:
        y = _value_tiles(draw, y, card.tiles)
    if card.top_earners:
        y = _bar_panel(draw, y, "TOP EARNERS", card.top_earners, cap=False)
    if card.top_donors:
        y = _bar_panel(draw, y, "TOP DONORS", card.top_donors, cap=False)
    if card.activity:
        y = _bar_panel(draw, y, "ALLIANCE ACTIVITY", card.activity)
    return _finish(image, draw, y, card.footer)


#: The bundled PIL font covers ASCII only. Typographic punctuation from
#: the embed-facing texts (em dashes, arrows, ellipses) would otherwise
#: render as tofu boxes on the card, so fold what has an ASCII twin and
#: drop the rest.
_ASCII_FOLD = {
    "\u2014": "-", "\u2013": "-", "\u2212": "-",     # em/en dash, minus
    "\u2018": "'", "\u2019": "'",                    # curly quotes
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00b7": "-", "\u00a0": " ",  # ellipsis, middot, nbsp
    "\u2191": "+", "\u2192": "->", "\u2193": "-",   # arrows
}


def ascii_only(text: str) -> str:
    """``text`` with every glyph the card font can actually draw.

    Accents are DECOMPOSED first, so a member called "Ångström" prints as
    "Angstrom" instead of losing half its letters.
    """
    import unicodedata

    for char, replacement in _ASCII_FOLD.items():
        text = text.replace(char, replacement)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(
        c for c in decomposed if not unicodedata.combining(c)
    )
    return "".join(c for c in stripped if c.isascii()).strip()


def _fmt(value: int | None) -> str:
    return "?" if value is None else f"{value:,}"


def card_from_overview(data, date_label: str) -> DailyCard:
    """Build the card from a gathered :class:`OverviewData`.

    ASCII only — the bundled PIL font has no arrow or emoji glyphs, so
    anything fancier renders as tofu boxes on the card.
    """
    net = data.joined.value - data.left.value
    tiles: list[tuple[str, str, str]] = [
        ("Members", _fmt(data.active_members),
         f"{net:+} today" if (data.joined.value or data.left.value) else "no changes"),
        ("Credits earned", _fmt(data.credits_total),
         f"by {data.credits_earners} member(s)" if data.credits_earners else ""),
    ]
    if data.donations_total is not None:
        tiles.append((
            "Donated", _fmt(data.donations_total),
            f"by {data.donations_contributors or 0} member(s)",
        ))
    elif data.balance is not None:
        tiles.append(("Alliance funds", _fmt(data.balance),
                      f"{data.balance_change:+,} today"
                      if data.balance_change is not None else ""))
    score_sub = ""
    if data.activity_score_previous is not None:
        delta = data.activity_score - data.activity_score_previous
        score_sub = "same as previous" if delta == 0 else f"{delta:+} vs previous"
    tiles.append(("Activity score", f"{data.activity_score}/100", score_sub))

    activity = [
        ("Courses started", data.courses_started.value),
        ("Courses done", data.courses_completed.value),
        ("Large missions", data.missions_started.value),
        ("Alliance events", data.events_started.value),
    ]
    return DailyCard(
        date_label=date_label,
        tiles=tiles,
        top_earners=[(ascii_only(n) or n, v) for n, v in data.top_earners[:5]],
        top_donors=[(ascii_only(n) or n, v) for n, v in data.top_donors[:5]],
        # An all-zero panel is noise; drop it entirely.
        activity=activity if any(n for _, n in activity) else [],
        # The outlook lines are written for embeds and carry em dashes.
        footer=ascii_only(data.outlook[0]) if data.outlook else "",
    )
