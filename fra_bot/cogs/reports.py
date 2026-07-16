"""Income reports: daily and monthly top-10 contributors.

MissionChief resets the income lists at midnight America/New_York. The
treasury sync captures a final snapshot at 23:55 NY, keyed by NY game
day / month, so shortly after midnight this cog reads the *completed*
period's snapshot — the reset race of the old bot cannot occur because
post-reset scrapes land under the NEW period key.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from ..db.repos import TreasuryRepo

log = logging.getLogger(__name__)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# If the snapshot behind a report was captured more than this long before
# the period's reset, the standings are probably incomplete (e.g. the bot
# was offline for the pre-reset capture), so we flag it on the embed.
_STALE_AFTER_MINUTES = 65


def _format_top10(rows) -> str:
    lines = []
    for row in rows[:10]:
        medal = _MEDALS.get(row["rank"], f"`#{row['rank']:>2}`")
        lines.append(f"{medal} **{row['username']}** — {row['amount']:,} credits")
    return "\n".join(lines) if lines else "No contributions recorded."


def _partial_note(reset_dt: dt.datetime, taken_at_iso: str | None, tz) -> str | None:
    """Warn when the snapshot predates the reset by too much to be final."""
    if not taken_at_iso:
        return None
    try:
        taken = dt.datetime.fromisoformat(taken_at_iso)
    except ValueError:
        return None
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=dt.timezone.utc)
    if reset_dt - taken.astimezone(reset_dt.tzinfo) <= dt.timedelta(
        minutes=_STALE_AFTER_MINUTES
    ):
        return None
    local = taken.astimezone(tz)
    return (
        f"⚠️ Possibly incomplete — last capture {local:%H:%M} NY on "
        f"{local:%b %d}, well before the reset (bot may have been offline)."
    )


class ReportsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._treasury = TreasuryRepo(bot.db)
        self._tz = ZoneInfo(bot.cfg.reports.timezone)
        self._report_task = asyncio.create_task(self._report_loop())

    def cog_unload(self) -> None:
        self._report_task.cancel()

    async def _report_loop(self) -> None:
        await self.bot.wait_until_ready()
        delay_minutes = self.bot.cfg.reports.daily_delay_minutes
        while True:
            try:
                now = dt.datetime.now(self._tz)
                # timedelta, NOT minute=delay: the setting allows up to 120
                # and .replace(minute=60+) raises, killing every report.
                target = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + dt.timedelta(minutes=max(5, delay_minutes))
                if target <= now:
                    target += dt.timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())

                fired_at = dt.datetime.now(self._tz)
                yesterday = (fired_at - dt.timedelta(days=1)).date()
                await self.post_daily_report(yesterday)
                if fired_at.day == 1:
                    last_month = (fired_at - dt.timedelta(days=2)).strftime("%Y-%m")
                    await self.post_monthly_report(last_month)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Report loop iteration failed")
                await asyncio.sleep(300)

    # ------------------------------------------------------------------

    async def post_daily_report(self, day: dt.date) -> bool:
        channel = self.bot.channel_for("reports")
        if channel is None:
            return False
        rows = await self._treasury.latest_snapshot("daily", day.isoformat())
        if not rows:
            log.warning("No daily income snapshot stored for %s", day)
            await self.bot.notify_admin(
                f"⚠️ Daily report skipped: no income snapshot for {day}."
            )
            return False
        reset = dt.datetime.combine(
            day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=self._tz
        )
        note = _partial_note(reset, rows[0]["taken_at"], self._tz)
        description = _format_top10(rows)
        if note:
            description = f"{note}\n\n{description}"
        embed = discord.Embed(
            title=f"💰 Daily top contributors — {day.strftime('%A %B %d, %Y')}",
            colour=discord.Colour.orange() if note else discord.Colour.gold(),
            description=description,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text="Alliance income, reset at midnight New York time")
        await channel.send(embed=embed)
        return True

    async def post_monthly_report(self, month_key: str) -> bool:
        channel = self.bot.channel_for("reports")
        if channel is None:
            return False
        rows = await self._treasury.latest_snapshot("monthly", month_key)
        if not rows:
            log.warning("No monthly income snapshot stored for %s", month_key)
            await self.bot.notify_admin(
                f"⚠️ Monthly report skipped: no income snapshot for {month_key}."
            )
            return False
        year, month = (int(part) for part in month_key.split("-"))
        next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
        reset = dt.datetime.combine(next_month, dt.time(0, 0), tzinfo=self._tz)
        note = _partial_note(reset, rows[0]["taken_at"], self._tz)
        pretty = dt.datetime.strptime(month_key, "%Y-%m").strftime("%B %Y")
        description = _format_top10(rows)
        if note:
            description = f"{note}\n\n{description}"
        embed = discord.Embed(
            title=f"🏆 Monthly top contributors — {pretty}",
            colour=discord.Colour.orange() if note else discord.Colour.gold(),
            description=description,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.set_footer(text="Alliance income, reset at month end New York time")
        await channel.send(embed=embed)
        return True
