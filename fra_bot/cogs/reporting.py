"""Discord layer for the reporting framework.

Renders any registered report on demand (`!fra report …`) and posts
scheduled reports (daily/weekly/monthly) configured in config.yaml.
Reports are read-only, so this is safe to run while the bot is in
dry-run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
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
        await self._post_overviews(fired_at)
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

    async def _post_overviews(self, fired_at: dt.datetime) -> None:
        """The built-in overview posts: member digest to the reports
        channel, admin digest to the admin log — every morning for the
        finished game day, plus the finished month on the NY 1st. Needs
        no per-entry config (the channels are already part of the bot's
        setup); ``reports.overviews`` turns the pair off."""
        if not getattr(self.bot.cfg.reports, "overviews", True):
            return
        periods = ["yesterday"]
        if fired_at.day == 1:
            periods.append("prev-month")
        for period in periods:
            try:
                await self._post_member_card(period)
            except Exception:
                log.exception("Member overview card (%s) failed", period)
            await asyncio.sleep(1.0)
            channel = self.bot.channel_for("admin_log")
            if channel is None:
                continue
            try:
                embed = await self.build("overview-admin", period)
                if isinstance(embed, str):
                    log.warning("Overview admin error: %s", embed)
                    continue
                await channel.send(embed=embed)
            except Exception:
                log.exception("Overview report admin (%s) failed", period)
            await asyncio.sleep(1.0)

    async def _post_member_card(self, period_name: str) -> None:
        """The member-facing half as ONE message: a rendered card with a
        compact embed under it. The morning used to arrive as a handful of
        separate embeds, which is what made it feel scattered."""
        from ..reporting.analytics import gather_overview
        from ..services.report_card import card_from_overview, render_daily_card

        channel = self.bot.channel_for("reports")
        if channel is None:
            return
        period = resolve_period(period_name)
        data = await gather_overview(self.bot.db, period, admin=False)
        label = (
            period.start.astimezone(NY).strftime("%d %b %Y")
            if period.start is not None else period.label
        )
        heading = (
            "Monthly overview" if period_name == "prev-month"
            else "Daily overview"
        )
        card = card_from_overview(data, label)
        card.heading = heading
        png = render_daily_card(card)

        embed = discord.Embed(
            title=f"{heading} — {label}",
            colour=discord.Colour(0xF0521F),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        if data.outlook:
            embed.add_field(
                name="🔮 Outlook",
                value="\n".join(f"• {line}" for line in data.outlook)[:_FIELD_LIMIT],
                inline=False,
            )
        if data.fun_facts:
            embed.add_field(
                name="✨ Fun facts",
                value="\n".join(data.fun_facts)[:_FIELD_LIMIT],
                inline=False,
            )
        if png is None:
            # Pillow missing — fall back to the full text report rather
            # than posting an embed with no numbers in it at all.
            fallback = await self.build("overview-member", period_name)
            if not isinstance(fallback, str):
                await channel.send(embed=fallback)
                return
        file = (
            discord.File(io.BytesIO(png), "daily-overview.png")
            if png is not None else None
        )
        if file is not None:
            embed.set_image(url="attachment://daily-overview.png")
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)

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
