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
    #: (label, value, sub) chips. Deliberately NOT bars: 12 courses next
    #: to 2 events share no unit, so bar length compared nothing.
    activity: list[tuple[str, str, str]] = field(default_factory=list)
    footer: str = ""


def _tile_row(draw, y: int, tiles: list[tuple[str, str, str]], *,
              height: int = 150, value_size: int = 44, pad: int = 28,
              label_size: int = 17, sub_size: int = 16,
              accent: bool = True) -> int:
    """A row of equal-width stat tiles: big value, label, trend line.

    The whole point of a daily card is "compared to what", so every tile
    carries its own comparison. Used twice at different scales — the
    headline row and the compact activity chips.
    """
    if not tiles:
        return y
    inner = _WIDTH - 2 * _PAD
    gap = 20
    width = (inner - (len(tiles) - 1) * gap) // len(tiles)
    for column, (label, value, sub) in enumerate(tiles):
        x = _PAD + column * (width + gap)
        draw.rounded_rectangle(
            (x, y, x + width, y + height), radius=_RADIUS, fill=_PANEL
        )
        if accent:
            draw.rounded_rectangle(
                (x, y + 24, x + 5, y + height - 24), radius=2, fill=_ACCENT
            )
        # Shrink the value font until it fits the tile — a 12-digit credit
        # total must never run into the next tile.
        size = value_size
        font = _font(size)
        room = width - 2 * pad
        while size > 16 and draw.textlength(value, font=font) > room:
            size -= 3
            font = _font(size)
        top = y + (24 if height >= 130 else 18)
        draw.text((x + pad, top), value, font=font, fill=_INK)
        draw.text((x + pad, top + size + 14), label, font=_font(label_size),
                  fill=_INK_MUTED)
        if sub:
            draw.text((x + pad, top + size + 14 + label_size + 9), sub,
                      font=_font(sub_size), fill=_INK_SOFT)
    return y + height + 28


def _two_column_bars(draw, y: int, left: tuple[str, list], right: tuple[str, list]) -> int:
    """Two bar panels side by side; the cursor below the taller one.

    Top earners and top donors are the same shape of list and are read
    together — stacked full-width they doubled the card height and made
    the reader scroll past one to reach the other.
    """
    left_title, left_rows = left
    right_title, right_rows = right
    if not left_rows and not right_rows:
        return y
    if not left_rows or not right_rows:
        title, rows = (left if left_rows else right)
        return _bar_panel(draw, y, title, rows, cap=False)
    gap = 20
    column = (_WIDTH - 2 * _PAD - gap) // 2
    bottom_left = _bar_panel(
        draw, y, left_title, left_rows, cap=False, x=_PAD, width=column
    )
    bottom_right = _bar_panel(
        draw, y, right_title, right_rows, cap=False,
        x=_PAD + column + gap, width=column,
    )
    return max(bottom_left, bottom_right)


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
        y = _tile_row(draw, y, card.tiles)
    y = _two_column_bars(
        draw, y,
        ("TOP EARNERS", card.top_earners),
        ("TOP DONORS", card.top_donors),
    )
    if card.activity:
        y = _tile_row(
            draw, y, card.activity, height=112, value_size=32,
            label_size=16, sub_size=15, accent=False,
        )
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


def _short_trend(metric, *, suffix: str = "vs prev") -> str:
    """A chip-sized comparison. The tile is ~180 px wide, so the embed's
    "(+3 vs previous)" has to lose weight without losing meaning."""
    if metric.previous is None:
        return ""
    delta = metric.value - metric.previous
    return f"same {suffix}" if delta == 0 else f"{delta:+,} {suffix}"


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
    # Alliance funds are a headline number and used to vanish entirely the
    # moment donations existed (the two shared one tile through an elif),
    # so on any normal day the card never showed the balance at all.
    if data.balance is not None:
        tiles.append((
            "Alliance funds", _fmt(data.balance),
            f"{data.balance_change:+,} today"
            if data.balance_change is not None else "",
        ))
    score_sub = ""
    if data.activity_score_previous is not None:
        delta = data.activity_score - data.activity_score_previous
        score_sub = "same as previous" if delta == 0 else f"{delta:+} vs previous"
    tiles.append(("Activity score", f"{data.activity_score}/100", score_sub))

    # Compact chips, not bars: 12 courses beside 2 events share no unit,
    # so bar length compared nothing while eating a third of the card.
    activity: list[tuple[str, str, str]] = []
    if data.donations_total is not None:
        activity.append((
            "Donated", _fmt(data.donations_total),
            f"top {data.donations_contributors or 0} shown"
            if data.donations_contributors else "",
        ))
    for label, metric in (
        ("Courses started", data.courses_started),
        ("Courses done", data.courses_completed),
        ("Large missions", data.missions_started),
        ("Alliance events", data.events_started),
    ):
        activity.append((label, _fmt(metric.value), _short_trend(metric)))
    # An all-zero row is noise; drop it entirely.
    if not any(
        value not in ("0", "?") for _, value, _ in activity
    ):
        activity = []

    return DailyCard(
        date_label=date_label,
        tiles=tiles,
        top_earners=[(ascii_only(n) or n, v) for n, v in data.top_earners[:5]],
        top_donors=[(ascii_only(n) or n, v) for n, v in data.top_donors[:5]],
        # An all-zero panel is noise; drop it entirely.
        activity=activity,
        # The outlook lines are written for embeds and carry em dashes.
        footer=ascii_only(data.outlook[0]) if data.outlook else "",
    )
