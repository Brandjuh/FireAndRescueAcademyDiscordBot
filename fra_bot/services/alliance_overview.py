"""The public alliance overview (``!fra alliance``): every player's data
added up into one card.

Two sources with very different coverage, and the embed says so:

* the **roster** (members table) covers every member — count, credits
  earned, contribution rate, joins and leaves;
* the **profile-sync** data (userscript) covers only the members who ran
  it — buildings and vehicles per type.

Pure functions only; the cog gathers the rows and sends the result.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import discord

Row = Mapping[str, Any]


@dataclass(frozen=True)
class RosterSummary:
    members: int
    credits_total: int
    credits_median: int | None
    contributing: int            # members donating more than 0%
    avg_contribution: float | None  # mean rate; an empty rate counts as 0%
    joined_30d: int
    left_30d: int


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def summarize_roster(
    active: Iterable[Row], former: Iterable[Row], *, now: dt.datetime
) -> RosterSummary:
    """Totals over the roster. Roster columns can be NULL mid-sweep: a
    missing credit figure is left out of the sums, a missing contribution
    rate is 0% (the same rule as the request gates)."""
    active = list(active)
    since = now - dt.timedelta(days=30)
    credits = [int(r["earned_credits"]) for r in active
               if r["earned_credits"] is not None]
    rates = [float(r["contribution_rate"] or 0.0) for r in active]
    joined = sum(1 for r in active
                 if (_parse_ts(r["first_seen_at"]) or now) >= since)
    left = sum(1 for r in former
               if (seen := _parse_ts(r["left_at"])) is not None and seen >= since)
    return RosterSummary(
        members=len(active),
        credits_total=sum(credits),
        credits_median=int(statistics.median(credits)) if credits else None,
        contributing=sum(1 for rate in rates if rate > 0),
        avg_contribution=(sum(rates) / len(rates)) if rates else None,
        joined_30d=joined,
        left_30d=left,
    )


def short_number(value: int) -> str:
    """28_580_945_816 -> '28.6B' — the card has no room for 11 digits."""
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value:,}"


def _type_lines(rows: list[tuple[str, int]]) -> str:
    return "\n".join(
        f"{count:,}× {name[:1].upper()}{name[1:]}" for name, count in rows
    )


def overview_embed(
    roster: RosterSummary,
    *,
    synced_members: int,
    building_total: int,
    vehicle_total: int,
    top_buildings: list[tuple[str, int]],
    top_vehicles: list[tuple[str, int]],
    date_label: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="🚒 Fire & Rescue Academy — alliance overview",
        description=f"All players' data added up · {date_label}",
        colour=discord.Colour.red(),
    )
    embed.add_field(
        name="👥 Members",
        value=(
            f"**{roster.members:,}** active\n"
            f"+{roster.joined_30d} joined · −{roster.left_30d} left (30 days)"
        ),
        inline=True,
    )
    credits = f"**{short_number(roster.credits_total)}** earned in total"
    if roster.credits_median is not None:
        credits += f"\nmedian {short_number(roster.credits_median)} per member"
    embed.add_field(name="💰 Credits", value=credits, inline=True)
    if roster.avg_contribution is not None:
        embed.add_field(
            name="🤝 Contribution",
            value=(
                f"**{roster.avg_contribution:.1f}%** on average\n"
                f"{roster.contributing:,} of {roster.members:,} donate"
            ),
            inline=True,
        )
    if synced_members:
        embed.add_field(
            name=f"🎮 Game data — synced by {synced_members:,} of "
                 f"{roster.members:,} members",
            value=(
                f"**{building_total:,}** buildings · "
                f"**{vehicle_total:,}** vehicles"
            ),
            inline=False,
        )
        if top_buildings:
            embed.add_field(name="🏢 Top buildings",
                            value=_type_lines(top_buildings)[:1024], inline=True)
        if top_vehicles:
            embed.add_field(name="🚑 Top vehicles",
                            value=_type_lines(top_vehicles)[:1024], inline=True)
    else:
        embed.add_field(
            name="🎮 Game data",
            value="Nobody has synced yet — FRA menu → 🔄 Sync to FRA in the game.",
            inline=False,
        )
    embed.set_footer(
        text="Buildings & vehicles only count members who use the FRA "
             "profile sync · !infographic · !fleet · !hotspots"
    )
    return embed
