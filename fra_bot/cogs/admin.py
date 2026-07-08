"""Admin commands: health, manual syncs and quick data lookups."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands

from ..db.repos import (
    ApplicationsRepo,
    AutomationRepo,
    LogsRepo,
    MembersRepo,
    MissionsRepo,
    RunsRepo,
    StateRepo,
    TreasuryRepo,
    ny_period_keys,
)
from ..services.treasury_sync import STATE_BACKFILL_DONE, STATE_BACKFILL_NEXT_PAGE

log = logging.getLogger(__name__)


def is_fra_admin_ctx(ctx: commands.Context) -> bool:
    """True when the invoker may run admin commands."""
    if ctx.guild is None:
        return False
    if ctx.author.guild_permissions.administrator:
        return True
    allowed = set(ctx.bot.cfg.discord.admin_role_ids)
    return any(role.id in allowed for role in ctx.author.roles)


def is_fra_admin():
    async def predicate(ctx: commands.Context) -> bool:
        return is_fra_admin_ctx(ctx)

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
        self._automation = AutomationRepo(bot.db)
        self._missions = MissionsRepo(bot.db)

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Gate EVERY command in this cog.

        A group-level check on ``fra`` does NOT propagate to its
        subcommands when ``invoke_without_command=True`` (discord.py
        skips the group's prepare/checks and dispatches straight to the
        subcommand), so authorization must live at the cog level where
        it always runs. Without this, any member could run !fra update.
        """
        return is_fra_admin_ctx(ctx)

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

    @fra.command(name="automation")
    async def automation(self, ctx: commands.Context) -> None:
        """Board automation status: switches, dry-run, recent requests."""
        auto = self.bot.cfg.automation
        embed = discord.Embed(
            title="🤖 Board automation",
            colour=discord.Colour.orange() if auto.dry_run else discord.Colour.green(),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(
            name="Mode",
            value="🧪 DRY-RUN (no actions)" if auto.dry_run else "🟢 LIVE",
            inline=False,
        )
        embed.add_field(
            name="Trainings",
            value=f"{'on' if auto.training.enabled else 'off'} · thread {auto.training.thread_id}",
        )
        embed.add_field(
            name="Buildings",
            value=f"{'on' if auto.building.enabled else 'off'} · thread {auto.building.thread_id}",
        )
        embed.add_field(
            name="Events",
            value=f"{'on' if auto.events.enabled else 'off'} · thread {auto.events.thread_id}",
        )
        embed.add_field(name="Open requests", value=str(await self._automation.open_count()))

        recent = await self._automation.recent(limit=8)
        if recent:
            lines = []
            for row in recent:
                icon = {"done": "✅", "failed": "❌", "skipped": "⏭️", "waiting": "⏳"}.get(
                    row["status"], "•"
                )
                lines.append(
                    f"{icon} `{row['kind']}` #{row['post_id']} — "
                    f"{row['status_detail'] or row['status']}"[:100]
                )
            embed.add_field(name="Recent requests", value="\n".join(lines), inline=False)
        await ctx.send(embed=embed)

    @fra.command(name="sync")
    async def sync(self, ctx: commands.Context, scraper: str) -> None:
        """Run a sync/poll: members, applications, logs, treasury, expenses,
        backfill, trainings, buildings, events."""
        # (func, canonical job name shared with the scheduler's lock).
        jobs = {
            "members": (self.bot.members_sync.run, "members"),
            "applications": (self.bot.applications_sync.run, "applications"),
            "logs": (self.bot.logs_sync.run, "logs"),
            "treasury": (self.bot.treasury_sync.sync_balance_and_income, "treasury"),
            "expenses": (self.bot.treasury_sync.sync_expenses_incremental, "expenses"),
            "backfill": (self.bot.treasury_sync.backfill_step, "expenses-backfill"),
            "trainings": (self.bot.trainings.poll, "board-trainings"),
            "buildings": (self.bot.buildings.poll, "board-buildings"),
            "events": (self.bot.events.poll, "board-events"),
        }
        entry = jobs.get(scraper.lower())
        if entry is None:
            await ctx.send(f"Unknown scraper. Options: {', '.join(sorted(jobs))}")
            return
        job, job_name = entry
        lock = self.bot.job_lock(job_name)
        if lock.locked():
            await ctx.send(f"⏳ `{scraper}` is already running — skipped.")
            return
        message = await ctx.send(f"⏳ Running `{scraper}` sync…")
        async with lock:
            self.bot.presence.mark_running(job_name)
            try:
                await job()
            except Exception as exc:  # surfaced to the invoking admin
                log.exception("Manual %s sync failed", scraper)
                await message.edit(content=f"❌ `{scraper}` sync failed: {exc}")
                return
            finally:
                self.bot.presence.mark_done(job_name)
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

    @fra.command(name="update")
    async def update(self, ctx: commands.Context) -> None:
        """Pull the latest code, install deps and restart the bot."""
        from ..selfupdate import perform_update, write_restart_marker

        message = await ctx.send("⏳ Checking for updates…")
        try:
            result = await perform_update()
        except Exception as exc:  # surfaced to the admin
            log.exception("Self-update failed")
            await message.edit(content=f"❌ Update failed: {exc}")
            return

        if not result.ok:
            await message.edit(content=f"❌ {result.summary}\n```\n{result.detail[:1500]}\n```")
            return
        if not result.changed:
            await message.edit(content=f"✅ {result.summary}")
            return

        embed = discord.Embed(
            title="⬆️ Updating and restarting",
            colour=discord.Colour.green(),
            description=result.summary,
        )
        if result.detail:
            embed.add_field(name="Changes", value=f"```\n{result.detail[:1000]}\n```", inline=False)
        embed.set_footer(text="Restarting now — I'll confirm here in ~15s.")
        await message.edit(content=None, embed=embed)

        # Remember where to confirm once the fresh process is up.
        write_restart_marker(
            self.bot.cfg.database.path,
            channel_id=ctx.channel.id,
            old_rev=result.old_rev,
            new_rev=result.new_rev,
        )

        log.info("Self-update applied (%s); restarting", result.summary)
        await self._restart_process()

    @fra.command(name="restart")
    async def restart(self, ctx: commands.Context) -> None:
        """Restart the bot (reloads config.yaml / .env) without updating code."""
        from ..selfupdate import current_rev, write_restart_marker

        rev = await current_rev()
        embed = discord.Embed(
            title="🔁 Restarting",
            colour=discord.Colour.blue(),
            description="Reloading configuration and restarting.",
        )
        embed.set_footer(text="I'll confirm here in ~15s.")
        await ctx.send(embed=embed)

        write_restart_marker(
            self.bot.cfg.database.path,
            channel_id=ctx.channel.id,
            old_rev=rev,
            new_rev=rev,
            reason="restart",
        )
        log.info("Restart requested via Discord; restarting")
        await self._restart_process()

    async def _restart_process(self) -> None:
        """Clean up resources, then replace the process with a fresh one."""
        from ..selfupdate import reexec

        try:
            await self.bot.presence.stop()
            await self.bot.scheduler.stop()
            await self.bot.geocoder.close()
            await self.bot.mc.close()
            await self.bot.db.close()
        except Exception:
            log.exception("Error during pre-restart cleanup; restarting anyway")
        reexec()

    # Friendly aliases for the historical income reports so muscle memory
    # (`!fra report daily`) still works after the framework generalisation.
    _REPORT_ALIASES = {"daily": "income-daily", "monthly": "income-monthly"}

    @fra.command(name="report")
    async def report(
        self, ctx: commands.Context, name: str = "list", period: str = ""
    ) -> None:
        """Render any registered report: `!fra report <name> [period]`.

        `!fra report list` shows everything available. The framework is
        read-only, so it is safe to run while the bot is in dry-run.
        """
        reporting = self.bot.get_cog("ReportingCog")
        if reporting is None:
            await ctx.send("Reporting cog not loaded.")
            return
        name = self._REPORT_ALIASES.get(name.lower(), name)
        await reporting.cmd_report(ctx, name, period)

    # -- custom "Own mission" scheduling --------------------------------

    @fra.command(name="missionpanel")
    async def mission_panel(self, ctx: commands.Context) -> None:
        """Post the mission-request panel to the configured channel."""
        missions = self.bot.get_cog("MissionsCog")
        if missions is None:
            await ctx.send("Missions cog not loaded.")
            return
        channel_id = self.bot.cfg.automation.mission.panel_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            await ctx.send(
                f"Configured panel channel `{channel_id}` not found; "
                "post from the target channel or fix `automation.mission.panel_channel_id`."
            )
            return
        await missions.post_panel(channel)
        if channel.id != ctx.channel.id:
            await ctx.send(f"✅ Mission panel posted in <#{channel.id}>.")

    @fra.command(name="missions")
    async def missions_list(self, ctx: commands.Context, limit: int = 10) -> None:
        """Show the most recent scheduled missions and their status."""
        rows = await self._missions.recent(limit=max(1, min(limit, 25)))
        if not rows:
            await ctx.send("No scheduled missions yet.")
            return
        auto = self.bot.cfg.automation.mission
        lines = []
        for r in rows:
            where = r["address"] or r["location_text"] or "?"
            lines.append(
                f"`#{r['id']:>3}` **{r['status']}** — {r['source']} — {where[:60]}"
                + (f" — {r['status_detail']}" if r["status_detail"] else "")
            )
        embed = discord.Embed(
            title="🚨 Scheduled missions",
            colour=discord.Colour.blurple(),
            description="\n".join(lines)[:4096],
        )
        embed.set_footer(
            text=(
                f"scheduler: {'on' if auto.enabled else 'off'} · "
                f"board: {'on' if auto.board_enabled else 'off'} · "
                f"dry_run: {self.bot.cfg.automation.dry_run} · "
                f"open: {await self._missions.open_count()}"
            )
        )
        await ctx.send(embed=embed)

    @fra.command(name="cancelmission")
    async def mission_cancel(self, ctx: commands.Context, mission_id: int) -> None:
        """Cancel a not-yet-started scheduled mission."""
        if await self._missions.cancel(mission_id):
            await ctx.send(f"🚫 Mission #{mission_id} cancelled.")
        else:
            await ctx.send(
                f"Mission #{mission_id} could not be cancelled "
                "(not found, or already started/finished)."
            )

    @fra.command(name="dump")
    async def dump(
        self, ctx: commands.Context, path: str, mode: str = "http"
    ) -> None:
        """Fetch a MissionChief page's HTML for inspection.

        `!fra dump /missionAllianceNew?tlat=40.7&tlng=-74` — server HTML.
        `!fra dump /buildings/new rendered` — HTML after JavaScript runs
        (needs Playwright). CSRF tokens are redacted before upload.
        """
        import io

        from ..mc.page_dump import redact_html, sanitize_dump_path

        try:
            path = sanitize_dump_path(path)
        except ValueError as exc:
            await ctx.send(f"⚠️ {exc}")
            return

        rendered = mode.lower() in ("rendered", "js", "browser")
        await ctx.send(
            f"⏳ Fetching `{path}` ({'rendered' if rendered else 'http'})…"
        )
        try:
            if rendered:
                from ..mc.browser_builder import (
                    BrowserBuilder,
                    cookies_for,
                    render_page,
                )

                if not BrowserBuilder.available():
                    await ctx.send(
                        "Playwright isn't installed here — try without `rendered`, "
                        "or `pip install playwright && python -m playwright install chromium`."
                    )
                    return
                base = self.bot.cfg.missionchief.base_url
                cookies = cookies_for(base, self.bot.mc.session.cookie_jar)
                html = await render_page(base, cookies, path)
            else:
                html = await self.bot.mc.fetch_page(path)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the cog
            await ctx.send(f"❌ Dump failed: {exc}")
            return

        html = redact_html(html)
        slug = path.strip("/").split("?")[0].replace("/", "_") or "page"
        suffix = "rendered" if rendered else "http"
        buffer = io.BytesIO(html.encode("utf-8"))
        try:
            await ctx.send(
                content=(
                    f"📄 `{path}` — {len(html):,} bytes "
                    f"({suffix}, CSRF tokens redacted)."
                ),
                file=discord.File(buffer, filename=f"{slug}-{suffix}.html"),
            )
        except discord.HTTPException as exc:
            await ctx.send(f"Fetched {len(html):,} bytes but couldn't upload it: {exc}")
