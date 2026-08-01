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

import discord
from discord.ext import commands

from ..reporting import Period, ReportResult, resolve_period
from ..reporting.period import NY, PERIODS

log = logging.getLogger(__name__)

_TITLE_LIMIT = 256
_DESC_LIMIT = 4096
_FIELD_LIMIT = 1024
_DEFAULT_COLOUR = discord.Colour.blurple()
#: NY game-day date (ISO) of the last scheduled-reports run — the restart
#: catch-up and the double-fire guard both key off it.
_LAST_FIRED_KEY = "reports:last_fired_day"


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
        # Fire shortly after each NY midnight — the GAME-day rollover — a
        # simple daily tick covers daily, weekly (on weekday) and monthly
        # (on day) cadences. Pinned to America/New_York, NOT the display
        # timezone: keyed to reports.timezone (e.g. Europe/Amsterdam) the
        # "daily" reports fired six hours before the game day ended and
        # showed partial standings; every period label and income key is
        # NY-based, so the trigger must be too.
        from ..db.repos import StateRepo

        state = StateRepo(self.bot.db)
        delay = self.bot.cfg.reports.daily_delay_minutes
        while True:
            try:
                now = dt.datetime.now(NY)
                # timedelta, NOT minute=delay: the setting allows up to 120
                # and .replace(minute=60+) raises, killing every report.
                target = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + dt.timedelta(minutes=max(5, delay))
                if target <= now:
                    # Today's fire minute already passed. A restart across
                    # it (an `!fra update` at the wrong moment) used to
                    # skip that day's reports entirely — catch up, unless
                    # the fired-day state says today already ran. The same
                    # state guards against double-posting after a clock
                    # step or restart later in the day.
                    if await state.get(_LAST_FIRED_KEY) != now.date().isoformat():
                        await state.set(_LAST_FIRED_KEY, now.date().isoformat())
                        await self._run_scheduled(now)
                    target += dt.timedelta(days=1)
                    now = dt.datetime.now(NY)
                await asyncio.sleep(max(1.0, (target - now).total_seconds()))
                fired_at = dt.datetime.now(NY)
                if await state.get(_LAST_FIRED_KEY) != fired_at.date().isoformat():
                    await state.set(_LAST_FIRED_KEY, fired_at.date().isoformat())
                    await self._run_scheduled(fired_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled reports failed")
                await asyncio.sleep(300)

    async def _run_scheduled(self, fired_at: dt.datetime) -> None:
        for sched in self.bot.cfg.reports.scheduled:
            # Per-entry isolation: one broken builder must not abort every
            # remaining scheduled report for the day.
            try:
                if not self._is_due(sched, fired_at):
                    continue
                channel = self.bot.get_channel(sched.channel_id)
                if channel is None:
                    continue
                embed = await self.build(sched.report, sched.period)
                if isinstance(embed, str):
                    log.warning(
                        "Scheduled report %s error: %s", sched.report, embed
                    )
                    continue
                await channel.send(embed=embed)
            except Exception:
                log.exception(
                    "Scheduled report %s failed; continuing with the rest",
                    getattr(sched, "report", "?"),
                )
            await asyncio.sleep(1.0)

    @staticmethod
    def _is_due(sched, fired_at: dt.datetime) -> bool:
        import calendar

        if sched.cadence == "daily":
            return True
        if sched.cadence == "weekly":
            return fired_at.weekday() == sched.weekday
        if sched.cadence == "monthly":
            # Clamp: day 29-31 fires on the last day of shorter months
            # instead of silently never firing there.
            last = calendar.monthrange(fired_at.year, fired_at.month)[1]
            return fired_at.day == min(sched.day, last)
        if sched.cadence == "yearly":
            last = calendar.monthrange(fired_at.year, sched.month)[1]
            return (
                fired_at.month == sched.month
                and fired_at.day == min(sched.day, last)
            )
        return False
