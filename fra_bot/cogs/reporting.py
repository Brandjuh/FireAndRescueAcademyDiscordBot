"""Discord layer for the reporting framework.

Renders any registered report on demand (`!fra report …`) and posts
scheduled reports (daily/weekly/monthly) configured in config.yaml.
Reports are read-only, so this is safe to run while the bot is in
dry-run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from ..reporting import Period, ReportResult, resolve_period
from ..reporting.period import PERIODS

log = logging.getLogger(__name__)

_TITLE_LIMIT = 256
_DESC_LIMIT = 4096
_FIELD_LIMIT = 1024
_DEFAULT_COLOUR = discord.Colour.blurple()


def render_report(result: ReportResult) -> discord.Embed:
    colour = discord.Colour(result.colour) if result.colour else _DEFAULT_COLOUR
    embed = discord.Embed(
        title=result.title[:_TITLE_LIMIT],
        description=(result.description or "")[:_DESC_LIMIT],
        colour=colour,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    for f in result.fields[:25]:
        embed.add_field(
            name=f.name[:_TITLE_LIMIT],
            value=(f.value or "—")[:_FIELD_LIMIT],
            inline=f.inline,
        )
    return embed


class ReportingCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.registry = bot.reports
        self._tz = ZoneInfo(bot.cfg.reports.timezone)
        self._task = asyncio.create_task(self._schedule_loop())

    def cog_unload(self) -> None:
        self._task.cancel()

    async def build(self, name: str, period_name: str) -> discord.Embed | str:
        report = self.registry.get(name)
        if report is None:
            return (
                f"Unknown report `{name}`. Try `!fra report list`."
            )
        try:
            period = resolve_period(period_name)
        except ValueError as exc:
            return str(exc)
        if period.name not in report.periods:
            return (
                f"Report `{name}` supports periods: {', '.join(report.periods)}."
            )
        result = await report.builder(period)
        return render_report(result)

    # -- command wiring is delegated from AdminCog.report ---------------

    async def cmd_report(self, ctx: commands.Context, name: str, period: str) -> None:
        if name == "list":
            lines = [
                f"• `{r.name}` — {r.description} "
                f"(periods: {', '.join(r.periods)})"
                for r in self.registry.all()
            ]
            embed = discord.Embed(
                title="📊 Available reports",
                description="\n".join(lines) or "No reports registered.",
                colour=_DEFAULT_COLOUR,
            )
            embed.set_footer(text="Usage: !fra report <name> [period]")
            await ctx.send(embed=embed)
            return
        result = await self.build(name, period or self._default_period(name))
        if isinstance(result, str):
            await ctx.send(result)
        else:
            await ctx.send(embed=result)

    def _default_period(self, name: str) -> str:
        report = self.registry.get(name)
        return report.default_period if report else "today"

    # -- scheduled reports ----------------------------------------------

    async def _schedule_loop(self) -> None:
        await self.bot.wait_until_ready()
        # Fire shortly after each NY midnight; a simple daily tick covers
        # daily, weekly (on weekday) and monthly (on day) cadences.
        delay = self.bot.cfg.reports.daily_delay_minutes
        while True:
            try:
                now = dt.datetime.now(self._tz)
                target = now.replace(
                    hour=0, minute=max(5, delay), second=0, microsecond=0
                )
                if target <= now:
                    target += dt.timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                await self._run_scheduled(dt.datetime.now(self._tz))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled reports failed")
                await asyncio.sleep(300)

    async def _run_scheduled(self, fired_at: dt.datetime) -> None:
        for sched in self.bot.cfg.reports.scheduled:
            if not self._is_due(sched, fired_at):
                continue
            channel = self.bot.get_channel(sched.channel_id)
            if channel is None:
                continue
            embed = await self.build(sched.report, sched.period)
            if isinstance(embed, str):
                log.warning("Scheduled report %s error: %s", sched.report, embed)
                continue
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("Could not post scheduled report %s: %s", sched.report, exc)
            await asyncio.sleep(1.0)

    @staticmethod
    def _is_due(sched, fired_at: dt.datetime) -> bool:
        if sched.cadence == "daily":
            return True
        if sched.cadence == "weekly":
            return fired_at.weekday() == sched.weekday
        if sched.cadence == "monthly":
            return fired_at.day == sched.day
        if sched.cadence == "yearly":
            return fired_at.month == sched.month and fired_at.day == sched.day
        return False
