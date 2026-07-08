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
    RotationRepo,
    RunsRepo,
    StateRepo,
    TreasuryRepo,
    ny_period_keys,
)
from ..services.treasury_sync import STATE_BACKFILL_DONE, STATE_BACKFILL_NEXT_PAGE

log = logging.getLogger(__name__)

# Only these Discord user IDs may spend alliance coins via !fra coinmission.
# Everything else in the bot is strictly free-only; this is a deliberate,
# owner-only exception, and it still previews unless `| confirm` is given.
COIN_AUTHORIZED_USER_IDS = {132620654087241729}


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
        self._rotation = RotationRepo(bot.db)

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
        apps = await self._logs.applications_received()
        apps_accepted = apps.get("added_to_alliance", 0)
        apps_denied = apps.get("application_denied", 0)
        log_count = await self._logs.count()
        expense_count = await self._treasury.expense_count()
        balance = await self._treasury.latest_balance()

        embed = discord.Embed(
            title="🚒 FRA bot status",
            colour=discord.Colour.blue(),
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="Active members", value=f"{member_count:,}")
        embed.add_field(
            name="Applications received",
            value=f"{apps_accepted + apps_denied:,}  (✅ {apps_accepted:,} · 🚫 {apps_denied:,})",
        )
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
        embed.add_field(
            name="Missions",
            value=(
                f"{'on' if auto.mission.enabled else 'off'} · "
                f"board {'on' if auto.mission.board_enabled else 'off'} · "
                f"rotation {await self._rotation.active_count()}"
            ),
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
            # Events + missions are one engine now (the events board and the
            # mission board are both scanned by the mission scheduler).
            "events": (self.bot.missions_service.poll, "missions"),
            "missions": (self.bot.missions_service.poll, "missions"),
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

    @fra.command(name="synccommands", aliases=["syncslash", "synccmds"])
    @commands.cooldown(1, 600, commands.BucketType.guild)
    async def sync_commands(self, ctx: commands.Context, scope: str = "guild") -> None:
        """Re-sync the slash (application) commands with Discord.

        The bot already syncs on startup, so after `!fra update` the new
        commands appear on their own. Use this only when Discord is still
        showing a stale command (e.g. old parameters).

        `!fra synccommands` — sync to this guild (appears within seconds).
        `!fra synccommands global` — sync globally (can take up to ~1 hour).

        Rate-limited to once per 10 minutes: Discord throttles command syncs
        hard, so it must not be run repeatedly.
        """
        scope = scope.lower().strip()
        tree = self.bot.tree
        try:
            if scope == "global":
                synced = await tree.sync()
                where = "globally (can take up to ~1h to appear)"
            else:
                guild_id = self.bot.cfg.discord.guild_id
                if not guild_id:
                    ctx.command.reset_cooldown(ctx)
                    await ctx.send(
                        "No `discord.guild_id` configured — use "
                        "`!fra synccommands global` instead."
                    )
                    return
                guild = discord.Object(id=guild_id)
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
                where = f"to guild `{guild_id}` (appears within seconds)"
        except discord.HTTPException as exc:
            ctx.command.reset_cooldown(ctx)
            await ctx.send(f"❌ Command sync failed: {exc}")
            return
        names = ", ".join(sorted(c.name for c in synced)) or "none"
        await ctx.send(f"✅ Synced {len(synced)} command(s) {where}.\nCommands: {names}")

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

    @fra.command(name="requestpanel", aliases=["requestspanel"])
    async def request_panel(self, ctx: commands.Context) -> None:
        """Post the training/building request panel in THIS channel."""
        requests_cog = self.bot.get_cog("RequestsCog")
        if requests_cog is None:
            await ctx.send("Requests cog not loaded.")
            return
        await requests_cog.post_panel(ctx.channel)

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
            recur = " 🔁" if r["recurring"] else ""
            lines.append(
                f"`#{r['id']:>3}` **{r['status']}** — {r['kind']}/{r['mission_source']}"
                f"{recur} — {where[:50]}"
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

    @fra.command(name="deletemission", aliases=["delmission", "rmmission"])
    async def mission_delete(self, ctx: commands.Context, target: str) -> None:
        """Delete a mission from the list — any status.

        `!fra deletemission <id>` removes one row. `!fra deletemission all`
        clears every FINISHED mission (done/failed/skipped/cancelled), leaving
        open ones alone. Use `!fra cancelmission` to stop an open one without
        deleting it.
        """
        if target.lower() == "all":
            n = await self._missions.delete_terminal()
            await ctx.send(f"🗑️ Deleted {n} finished mission(s).")
            return
        try:
            mission_id = int(target)
        except ValueError:
            await ctx.send("Usage: `!fra deletemission <id|all>`")
            return
        if await self._missions.delete(mission_id):
            await ctx.send(f"🗑️ Mission #{mission_id} deleted.")
        else:
            await ctx.send(f"Mission #{mission_id} not found.")

    @fra.command(name="coinmission", aliases=["paidmission"])
    async def coin_mission(self, ctx: commands.Context, *, spec_text: str = "") -> None:
        """Start a mission/event using COINS — owner-only.

        Unlike every other path (which is strictly free-only), this spends
        alliance coins and ignores the free-mission cooldown, so it can start
        right away. Restricted to specific Discord user id(s).

        `!fra coinmission <location> [| kind: event] [| preset: Pile-up]
        [| custom: need_lf=25 …] [| saved: <name>] [| name: <caption>] [| confirm]`

        Without `| confirm` it only PREVIEWS the cost — nothing is spent. Add
        `| confirm` to actually start and spend coins (works even while the
        rest of the bot is in dry-run).
        """
        if ctx.author.id not in COIN_AUTHORIZED_USER_IDS:
            await ctx.send("⛔ This command is restricted to the alliance owner.")
            return
        spec_text = spec_text.strip()
        if not spec_text:
            await ctx.send(
                "Usage: `!fra coinmission <location> [| kind: event] "
                "[| preset: Pile-up] [| custom: need_lf=25 …] [| saved: <name>] "
                "[| name: <caption>] [| confirm]`\n"
                "_Add `| confirm` to actually spend coins; without it you get a preview._"
            )
            return

        # Pull the `confirm` flag out of the pipe-separated segments.
        confirm = False
        kept: list[str] = []
        for seg in (s.strip() for s in spec_text.split("|")):
            if seg.lower() in ("confirm", "confirm: yes", "confirm:yes", "yes"):
                confirm = True
            elif seg:
                kept.append(seg)
        try:
            spec = self._parse_rotation_spec(" | ".join(kept))
        except Exception as exc:  # noqa: BLE001 - surface the reason
            await ctx.send(f"⚠️ {exc}")
            return

        verb = "Starting (PAID)" if confirm else "Previewing"
        message = await ctx.send(
            f"⏳ {verb} — {spec.describe()} at *{spec.location_text}*…"
        )
        try:
            outcome = await self.bot.missions_service.run_coin_mission(spec, confirm=confirm)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the cog
            log.exception("coinmission failed")
            await message.edit(content=f"❌ Failed: {exc}")
            return

        icon = {
            "started": "💰🚨", "dry_run": "🧪", "not_found": "❌",
            "form_error": "❌", "http_error": "❌", "refused": "❌",
            "unverified": "⚠️",
        }.get(outcome.state, "•")
        tail = (
            "\n_Add `| confirm` to actually spend coins._"
            if outcome.state == "dry_run" else ""
        )
        await message.edit(content=f"{icon} {outcome.detail}{tail}"[:1900])

    @fra.command(name="guides", aliases=["guidesync", "syncguides"])
    async def guides(self, ctx: commands.Context, mode: str = "") -> None:
        """Sync every board guide RIGHT NOW and report where each one is.

        `!fra guides` creates/edits the guides in place. `!fra guides repost`
        deletes each existing guide and posts a fresh one at the BOTTOM of its
        thread — use this when a guide is buried under newer posts. Guides are
        informational posts, so this works in dry-run too.
        """
        repost = mode.strip().lower() == "repost"
        auto = self.bot.cfg.automation
        message = await ctx.send(
            "⏳ Syncing board guides… (MissionChief pacing applies — the "
            "trainings guide fetches classroom availability, so give it a "
            "few minutes)"
        )
        lines: list[str] = []

        async def _report() -> None:
            await message.edit(
                content=("\n".join(lines) + "\n⏳ …")[:1990]
            )

        try:
            if auto.training.enabled:
                async with self.bot.job_lock("board-trainings"):
                    lines.append(await self.bot.trainings.force_guide(repost=repost))
                await _report()
            if auto.building.enabled:
                async with self.bot.job_lock("board-buildings"):
                    lines.append(await self.bot.buildings.force_guide(repost=repost))
                await _report()
            boards = self.bot.missions_service._request_boards()
            if boards:
                async with self.bot.job_lock("missions"):
                    for thread_id, kind in boards:
                        lines.append(
                            await self.bot.missions_service.force_guide(
                                thread_id, kind, repost=repost
                            )
                        )
        except Exception as exc:  # noqa: BLE001 — surface it to the admin
            log.exception("guide sync failed")
            lines.append(f"❌ guide sync aborted: {exc}")
        if not lines:
            lines.append(
                "No boards enabled — turn on training/building/events/mission "
                "board switches in config.yaml first."
            )
        await message.edit(content="\n".join(lines)[:1990])

    @fra.command(name="upgradebuildings", aliases=["upgradebuilding", "buildingupgrade"])
    async def upgrade_buildings(self, ctx: commands.Context, *, arg: str = "") -> None:
        """Level up + extend all alliance hospitals & prisons.

        Raises hospital levels to the max and buys every extension EXCEPT the
        final "large" one (Large Hospital / Large Prison). Prisons are walked
        one extension at a time.

        `!fra upgradebuildings` previews (no changes). `!fra upgradebuildings
        confirm` executes — owner-only, spends alliance credits (works even
        while the bot is in dry-run), and never lets funds drop below the
        floor. Runs a bounded chunk per call; re-run to continue.
        """
        execute = arg.strip().lower() in ("confirm", "confirm yes", "yes", "live", "go")
        if execute and ctx.author.id not in COIN_AUTHORIZED_USER_IDS:
            await ctx.send(
                "⛔ Executing upgrades spends alliance credits and is restricted to "
                "the alliance owner. Run it without `confirm` for a preview."
            )
            return
        lock = self.bot.job_lock("building-upgrade")
        if lock.locked():
            await ctx.send("⏳ A building-upgrade run is already in progress — skipped.")
            return
        verb = "Executing" if execute else "Previewing"
        message = await ctx.send(
            f"⏳ {verb} alliance hospital/prison upgrades — this can take a while…"
        )
        floor = self.bot.cfg.automation.building.min_alliance_funds
        async with lock:
            self.bot.presence.mark_running("building-upgrade")
            try:
                report = await self.bot.building_upgrade.upgrade_all(execute=execute)
            except Exception as exc:  # noqa: BLE001 — report, don't crash the cog
                log.exception("building upgrade failed")
                await message.edit(content=f"❌ Building upgrade failed: {exc}")
                return
            finally:
                self.bot.presence.mark_done("building-upgrade")
        summary = report.summary(floor=floor)
        await message.edit(content=summary[:1990])
        for start in range(1990, len(summary), 1990):
            await ctx.send(summary[start : start + 1990])

    @fra.command(name="nextmission")
    async def next_mission(self, ctx: commands.Context) -> None:
        """Show which mission/event is up next and where (for the eventpinger)."""
        nxt = await self.bot.missions_service.next_up()
        if nxt is None:
            await ctx.send(
                "No mission is queued and the rotation is empty. "
                "Add locations with `!fra rotation add`."
            )
            return
        origin = "member request" if nxt["origin"] == "request" else "rotation"
        who = f" (for {nxt['requester']})" if nxt.get("requester") else ""
        cap = f" · {nxt['caption']}" if nxt.get("caption") else ""
        embed = discord.Embed(
            title="⏭️ Next up",
            colour=discord.Colour.blurple(),
            description=(
                f"**{nxt['kind']}** · {nxt['mission_source']}{cap}\n"
                f"📍 {nxt['location'] or '?'}\n"
                f"_source: {origin}{who}_"
            ),
        )
        await ctx.send(embed=embed)

    # -- mission rotation list ------------------------------------------

    @fra.group(name="rotation", invoke_without_command=True)
    async def rotation(self, ctx: commands.Context) -> None:
        """Manage the auto-start rotation list. Subcommands: add, list,
        remove, on, off."""
        await self._render_rotation_list(ctx)

    @rotation.command(name="add")
    async def rotation_add(self, ctx: commands.Context, *, spec_text: str = "") -> None:
        """Add a location to the rotation.

        `!fra rotation add Grand Rapids`
        `!fra rotation add Amsterdam | kind: event`
        `!fra rotation add NYC | custom: need_lf=25 need_elw1=6 | name: Big fire`
        `!fra rotation add Berlin | saved: Wildfire`
        """
        spec_text = spec_text.strip()
        if not spec_text:
            await ctx.send(
                "Usage: `!fra rotation add <location> [| kind: event] "
                "[| preset: Pile-up] [| custom: need_lf=25 …] [| saved: <name>] "
                "[| name: <caption>]`"
            )
            return
        try:
            spec = self._parse_rotation_spec(spec_text)
        except Exception as exc:  # noqa: BLE001 - surface the reason
            await ctx.send(f"⚠️ {exc}")
            return
        import json as _json

        rid = await self._rotation.add(
            location_text=spec.location_text,
            kind=spec.kind,
            mission_source=spec.source,
            preset_type_id=spec.preset_type_id,
            caption=spec.custom.caption if spec.custom else spec.saved_name,
            custom_values=_json.dumps(spec.custom.values) if spec.custom else None,
            saved_name=spec.saved_name,
            active=1,
            created_by=ctx.author.display_name,
        )
        await ctx.send(
            f"🔁 Rotation **#{rid}** added — {spec.describe()} — at "
            f"*{spec.location_text}*."
        )

    @rotation.command(name="list")
    async def rotation_list(self, ctx: commands.Context) -> None:
        """Show the rotation list and which entry is next."""
        await self._render_rotation_list(ctx)

    async def _render_rotation_list(self, ctx: commands.Context) -> None:
        rows = await self._rotation.list_all()
        if not rows:
            await ctx.send(
                "The rotation list is empty. Add one with `!fra rotation add <location>`."
            )
            return
        nxt = await self._rotation.next_entry()
        next_id = nxt["id"] if nxt else None
        lines = []
        for r in rows:
            mark = "▶️" if r["id"] == next_id else ("✅" if r["active"] else "⏸️")
            where = r["address"] or r["location_text"] or "?"
            started = r["last_started_at"][:10] if r["last_started_at"] else "never"
            lines.append(
                f"{mark} `#{r['id']:>3}` {r['kind']}/{r['mission_source']} — "
                f"{where[:45]} — started ×{r['start_count']} ({started})"
            )
        auto = self.bot.cfg.automation.mission
        embed = discord.Embed(
            title="🔁 Mission rotation",
            colour=discord.Colour.blurple(),
            description="\n".join(lines)[:4096],
        )
        embed.set_footer(
            text=(
                f"▶️ next · ✅ active · ⏸️ paused · "
                f"scheduler: {'on' if auto.enabled else 'off'} · "
                f"dry_run: {self.bot.cfg.automation.dry_run}"
            )
        )
        await ctx.send(embed=embed)

    @rotation.command(name="remove", aliases=["rm", "delete"])
    async def rotation_remove(self, ctx: commands.Context, rotation_id: int) -> None:
        """Remove an entry from the rotation."""
        if await self._rotation.remove(rotation_id):
            await ctx.send(f"🗑️ Rotation #{rotation_id} removed.")
        else:
            await ctx.send(f"Rotation #{rotation_id} not found.")

    @rotation.command(name="off", aliases=["pause"])
    async def rotation_off(self, ctx: commands.Context, rotation_id: int) -> None:
        """Pause an entry (kept in the list, but skipped)."""
        if await self._rotation.set_active(rotation_id, False):
            await ctx.send(f"⏸️ Rotation #{rotation_id} paused.")
        else:
            await ctx.send(f"Rotation #{rotation_id} not found.")

    @rotation.command(name="on", aliases=["resume"])
    async def rotation_on(self, ctx: commands.Context, rotation_id: int) -> None:
        """Resume a paused entry."""
        if await self._rotation.set_active(rotation_id, True):
            await ctx.send(f"▶️ Rotation #{rotation_id} resumed.")
        else:
            await ctx.send(f"Rotation #{rotation_id} not found.")

    @staticmethod
    def _parse_rotation_spec(text: str):
        """Parse `<location> | key: value | …` into a validated MissionSpec."""
        from ..cogs.missions import build_spec

        # Segment key -> build_spec kwarg (most are identical).
        aliases = {"event": "event_type", "type": "event_type", "volume": "call_volume",
                   "call": "call_volume"}
        allowed = {
            "kind", "preset", "saved", "custom", "name", "schedule",
            "event", "type", "area", "shape", "call_volume", "volume", "call",
        }
        segments = [s.strip() for s in text.split("|")]
        location = segments[0]
        kwargs: dict[str, str] = {}
        for seg in segments[1:]:
            if not seg:
                continue
            key, sep, value = seg.partition(":")
            if not sep:
                raise ValueError(f"segment '{seg}' must be `key: value`")
            key = key.strip().lower()
            if key not in allowed:
                raise ValueError(
                    f"unknown option '{key}' (use: {', '.join(sorted(allowed))})"
                )
            kwargs[aliases.get(key, key)] = value.strip()
        return build_spec(location=location, **kwargs)

    @fra.command(name="testbuild")
    async def testbuild(self, ctx: commands.Context, *, args: str = "") -> None:
        """Test the building flow for a location without a board post.

        `!fra testbuild <address or maps link>` — the type is auto-detected
        from the address (hospital/prison). `!fra testbuild hospital <loc>`
        forces the type. In dry-run this drives the real form (type, pin,
        address, alliance button) but does NOT submit; with dry_run off it
        actually builds.
        """
        args = args.strip()
        parts = args.split(maxsplit=1)
        building_type = None
        location = args
        if parts and parts[0].lower() in ("hospital", "prison"):
            building_type = parts[0].lower()
            location = parts[1].strip() if len(parts) > 1 else ""
        if not location:
            await ctx.send(
                "Usage: `!fra testbuild [hospital|prison] <address or maps link>`"
            )
            return
        await ctx.send(
            f"⏳ Testing build at `{location}`… (browser start can take a moment)"
        )
        try:
            result = await self.bot.buildings.test_build(building_type, location)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            log.exception("testbuild failed")
            await ctx.send(f"❌ Test failed: {exc}")
            return
        await ctx.send(result[:1900])

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
