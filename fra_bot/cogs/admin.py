"""Admin commands: health, manual syncs and quick data lookups."""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from ..db.repos import (
    ApplicationsRepo,
    LogsRepo,
    MembersRepo,
    RunsRepo,
    StateRepo,
    TreasuryRepo,
    ny_period_keys,
)
from ..services.treasury_sync import STATE_BACKFILL_DONE, STATE_BACKFILL_NEXT_PAGE

log = logging.getLogger(__name__)


def is_fra_admin():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        if ctx.author.guild_permissions.administrator:
            return True
        allowed = set(ctx.bot.cfg.discord.admin_role_ids)
        return any(role.id in allowed for role in ctx.author.roles)

    return commands.check(predicate)


class AdminCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._members = MembersRepo(bot.db)
        self._apps = ApplicationsRepo(bot.db)
        self._logs = LogsRepo(bot.db)
        self._treasury = TreasuryRepo(bot.db)
        self._runs = RunsRepo(bot.db)
        self._state = StateRepo(bot.db)

    @commands.group(name="fra", invoke_without_command=True)
    @is_fra_admin()
    async def fra(self, ctx: commands.Context) -> None:
        await ctx.send_help(ctx.command)

    @fra.command(name="status")
    async def status(self, ctx: commands.Context) -> None:
        """Bot health: data counts, backfill progress, recent sync runs."""
        member_count = await self._members.active_count()
        open_apps = await self._apps.open_count()
        log_count = await self._logs.count()
        expense_count = await self._treasury.expense_count()
        balance = await self._treasury.latest_balance()

        embed = discord.Embed(
            title="🚒 FRA bot status",
            colour=discord.Colour.blue(),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="Active members", value=f"{member_count:,}")
        embed.add_field(name="Open applications", value=str(open_apps))
        embed.add_field(name="Alliance logs stored", value=f"{log_count:,}")
        embed.add_field(name="Expenses stored", value=f"{expense_count:,}")
        if balance is not None:
            embed.add_field(
                name="Alliance funds",
                value=f"{balance['total_funds']:,} credits",
            )
        if await self._state.get(STATE_BACKFILL_DONE) == "1":
            embed.add_field(name="Expenses backfill", value="✅ complete")
        else:
            next_page = await self._state.get(STATE_BACKFILL_NEXT_PAGE, "1")
            staged = await self._treasury.staging_count()
            embed.add_field(
                name="Expenses backfill",
                value=f"⏳ at page {next_page} ({staged:,} rows staged)",
            )
        if self.bot.pacer.circuit_open:
            embed.add_field(
                name="⚠️ Circuit breaker",
                value="OPEN — MissionChief traffic paused",
                inline=False,
            )

        runs = await self._runs.recent(limit=8)
        if runs:
            lines = []
            for run in runs:
                icon = {"success": "✅", "failed": "❌"}.get(run["status"], "⏳")
                lines.append(
                    f"{icon} `{run['scraper']}` {run['started_at'][11:19]} UTC — "
                    f"{run['rows_new']} new"
                )
            embed.add_field(name="Recent runs", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed)

    @fra.command(name="sync")
    async def sync(self, ctx: commands.Context, scraper: str) -> None:
        """Manually run a sync: members, applications, logs, treasury, expenses."""
        jobs = {
            "members": self.bot.members_sync.run,
            "applications": self.bot.applications_sync.run,
            "logs": self.bot.logs_sync.run,
            "treasury": self.bot.treasury_sync.sync_balance_and_income,
            "expenses": self.bot.treasury_sync.sync_expenses_incremental,
            "backfill": self.bot.treasury_sync.backfill_step,
        }
        job = jobs.get(scraper.lower())
        if job is None:
            await ctx.send(f"Unknown scraper. Options: {', '.join(sorted(jobs))}")
            return
        message = await ctx.send(f"⏳ Running `{scraper}` sync…")
        try:
            await job()
        except Exception as exc:  # surfaced to the invoking admin
            log.exception("Manual %s sync failed", scraper)
            await message.edit(content=f"❌ `{scraper}` sync failed: {exc}")
            return
        await message.edit(content=f"✅ `{scraper}` sync finished.")

    @fra.command(name="balance")
    async def balance(self, ctx: commands.Context) -> None:
        """Latest known alliance funds."""
        row = await self._treasury.latest_balance()
        if row is None:
            await ctx.send("No balance recorded yet.")
            return
        await ctx.send(
            f"💰 Alliance funds: **{row['total_funds']:,} credits** "
            f"(as of {row['scraped_at']} UTC)"
        )

    @fra.command(name="top10")
    async def top10(self, ctx: commands.Context, period: str = "daily") -> None:
        """Current income top 10 (period: daily or monthly)."""
        period = period.lower()
        if period not in ("daily", "monthly"):
            await ctx.send("Period must be `daily` or `monthly`.")
            return
        day_key, month_key = ny_period_keys()
        key = day_key if period == "daily" else month_key
        rows = await self._treasury.latest_snapshot(period, key)
        if not rows:
            await ctx.send(f"No {period} income snapshot for {key} yet.")
            return
        from .reports import _format_top10

        embed = discord.Embed(
            title=f"💰 {period.capitalize()} top contributors ({key})",
            colour=discord.Colour.gold(),
            description=_format_top10(rows),
        )
        await ctx.send(embed=embed)

    @fra.command(name="report")
    async def report(self, ctx: commands.Context, kind: str = "daily") -> None:
        """Repost the most recent daily/monthly report on demand."""
        reports_cog = self.bot.get_cog("ReportsCog")
        if reports_cog is None:
            await ctx.send("Reports cog not loaded.")
            return
        now_ny = dt.datetime.now(ZoneInfo("America/New_York"))
        if kind.lower() == "daily":
            ok = await reports_cog.post_daily_report(
                (now_ny - dt.timedelta(days=1)).date()
            )
        elif kind.lower() == "monthly":
            first = now_ny.replace(day=1) - dt.timedelta(days=1)
            ok = await reports_cog.post_monthly_report(first.strftime("%Y-%m"))
        else:
            await ctx.send("Kind must be `daily` or `monthly`.")
            return
        if not ok:
            await ctx.send("No snapshot available for that period (yet).")
