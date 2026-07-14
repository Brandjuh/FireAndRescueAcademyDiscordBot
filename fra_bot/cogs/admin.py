"""Admin commands: health, manual syncs and quick data lookups."""

from __future__ import annotations

import asyncio
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
        backlog = self.bot.mc.pacer_backlog
        if backlog:
            bulk = self.bot.mc.pacer_backlog_bulk
            interactive = backlog - bulk
            embed.add_field(
                name="⏳ MC request backlog",
                value=(
                    f"{backlog} waiting ({interactive} board/interactive · "
                    f"{bulk} bulk backfill; bulk yields to board work) — if the "
                    "interactive count keeps growing, lower "
                    "`missionchief.max_delay` (default 9.0)"
                ),
            )
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
        from ..services.logs_sync import (
            STATE_BACKFILL_DONE as LOGS_DONE,
            STATE_BACKFILL_NEXT_PAGE as LOGS_NEXT_PAGE,
        )

        if await self._state.get(LOGS_DONE) == "1":
            embed.add_field(name="Logs backfill", value="✅ complete")
        else:
            logs_page = await self._state.get(LOGS_NEXT_PAGE, "1")
            embed.add_field(
                name="Logs backfill", value=f"⏳ at page {logs_page}"
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

    # -- runtime settings -------------------------------------------------

    @fra.command(name="set")
    async def set_setting(self, ctx: commands.Context, key: str = "", *, value: str = "") -> None:
        """Change any config setting, e.g. `!fra set dry_run off`.

        Keys match by unique suffix (`dry_run`, `training.enabled`,
        `max_delay`, …) — `!fra settings` lists them all. Booleans accept
        on/off, yes/no, aan/uit; channels and roles accept #/@ mentions.
        The change persists across restarts (stored as an override on top
        of config.yaml); undo with `!fra settings reset <key>`.
        """
        from ..core import settings as rt

        if not key:
            await ctx.send(
                "Usage: `!fra set <setting> <value>` — e.g. `!fra set dry_run off`, "
                "`!fra set max_delay 9`, `!fra set training.enabled on`.\n"
                "`!fra settings` shows every setting and its current value."
            )
            return
        try:
            setting = rt.resolve(key)
            parsed = rt.parse_value(setting, value, self.bot.cfg)
        except rt.SettingError as exc:
            await ctx.send(f"⚠️ {exc}")
            return

        old = rt.current(self.bot.cfg, setting)
        rt.apply(self.bot.cfg, setting, parsed)
        await rt.store_override(self._state, setting, parsed)
        try:
            rt.post_apply(self.bot, setting)
        except Exception:  # noqa: BLE001 — the value is stored; side-effect only
            log.exception("post-apply for %s failed", setting.path)

        when = (
            "active immediately"
            if setting.live else "takes effect after `!fra restart`"
        )
        warning = ""
        if setting.path == "automation.dry_run" and parsed is False:
            warning = (
                "\n⚠️ **Dry-run is OFF — the bot will now perform REAL "
                "MissionChief actions** (trainings, buildings, missions)."
            )
        await ctx.send(
            f"✅ `{setting.path}` = **{rt.format_value(parsed)}** "
            f"(was {rt.format_value(old)}) — {when}.{warning}\n"
            f"_Undo with `!fra settings reset {setting.path}`._"
        )

    @fra.group(name="settings", aliases=["instellingen"], invoke_without_command=True)
    async def settings_group(self, ctx: commands.Context, group: str = "") -> None:
        """Show every runtime setting (optionally one group, e.g.
        `!fra settings automation`). `*` marks values overridden via
        `!fra set`; ⟳ marks settings that need a restart to apply."""
        from ..core import settings as rt

        group = group.strip().lower()
        groups: dict[str, list[str]] = {}
        for setting in rt.SETTINGS:
            if group and setting.group != group:
                continue
            override = await rt.get_override(self._state, setting)
            mark = " \\*" if override is not None else ""
            flag = "" if setting.live else " ⟳"
            value = rt.format_value(rt.current(self.bot.cfg, setting))
            short = setting.path.split(".", 1)[1]
            groups.setdefault(setting.group, []).append(
                f"`{short}` = **{value}**{mark}{flag}"
            )
        if not groups:
            names = ", ".join(sorted({s.group for s in rt.SETTINGS}))
            await ctx.send(f"Unknown group `{group}`. Groups: {names}")
            return
        # Split into several embeds/fields so nothing is truncated: Discord
        # caps a field value at 1024 chars and an embed at ~6000 / 25 fields,
        # and the automation group alone now blows past a single field.
        header = (
            "Change with `!fra set <key> <value>` · reset with "
            "`!fra settings reset <key>`\n\\* = overridden via command · "
            "⟳ = applies after restart"
        )
        fields: list[tuple[str, str]] = []
        for name, lines in groups.items():
            chunk: list[str] = []
            size = 0
            part = 1
            for line in lines:
                if chunk and size + len(line) + 1 > 1024:
                    label = name if part == 1 else f"{name} (cont. {part})"
                    fields.append((label, "\n".join(chunk)))
                    chunk, size, part = [], 0, part + 1
                chunk.append(line)
                size += len(line) + 1
            if chunk:
                label = name if part == 1 else f"{name} (cont. {part})"
                fields.append((label, "\n".join(chunk)))

        # Pack fields into embeds (≤10 fields / ≤5000 chars each) and send each.
        first = True
        pending: list[tuple[str, str]] = []
        pending_size = 0

        async def _flush() -> None:
            nonlocal first, pending, pending_size
            if not pending:
                return
            embed = discord.Embed(
                title="⚙️ Settings" if first else "⚙️ Settings (cont.)",
                colour=discord.Colour.blurple(),
                description=header if first else None,
            )
            for field_name, field_value in pending:
                embed.add_field(name=field_name, value=field_value, inline=False)
            await ctx.send(embed=embed)
            first = False
            pending = []
            pending_size = 0

        for field_name, field_value in fields:
            if pending and (len(pending) >= 10 or pending_size + len(field_value) > 5000):
                await _flush()
            pending.append((field_name, field_value))
            pending_size += len(field_value) + len(field_name)
        await _flush()

    @settings_group.command(name="reset")
    async def settings_reset(self, ctx: commands.Context, key: str = "") -> None:
        """Remove an override so the config.yaml value applies again.
        `!fra settings reset all` clears every override."""
        from ..config import ConfigError, load_config
        from ..core import settings as rt

        if not key:
            await ctx.send("Usage: `!fra settings reset <key|all>`")
            return

        try:
            fresh = load_config()
        except (ConfigError, Exception):  # noqa: BLE001 — fall back to restart
            fresh = None

        async def _reset_one(setting) -> str:
            removed = await rt.clear_override(self._state, setting)
            if not removed:
                return f"➖ `{setting.path}` had no override"
            if fresh is not None:
                file_value = rt.current(fresh, setting)
                rt.apply(self.bot.cfg, setting, file_value)
                try:
                    rt.post_apply(self.bot, setting)
                except Exception:  # noqa: BLE001
                    log.exception("post-apply for %s failed", setting.path)
                when = "" if setting.live else " (fully applies after restart)"
                return (
                    f"✅ `{setting.path}` back to **{rt.format_value(file_value)}** "
                    f"from config.yaml{when}"
                )
            return f"✅ `{setting.path}` override removed — file value applies after restart"

        if key.strip().lower() == "all":
            lines = [await _reset_one(s) for s in rt.SETTINGS]
            lines = [line for line in lines if not line.startswith("➖")]
            await ctx.send("\n".join(lines)[:1900] or "No overrides were set.")
            return
        try:
            setting = rt.resolve(key)
        except rt.SettingError as exc:
            await ctx.send(f"⚠️ {exc}")
            return
        await ctx.send(await _reset_one(setting))

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

    @fra.command(name="logroutes", aliases=["logroute"])
    async def log_routes_cmd(
        self, ctx: commands.Context, action: str = "list", *args: str
    ) -> None:
        """Duplicate alliance-log types into extra channels (the log still
        posts to the main log channel; a COPY also goes to each route):

        `!fra logroutes` — show the current routes
        `!fra logroutes keys` — list the log types and group aliases
        `!fra logroutes add <#channel> <type…>` — route these types to a
        channel (types: a group like `building`, an exact key like
        `building_constructed`, or `all`)
        `!fra logroutes remove <#channel> [type]` — drop one type, or the
        whole channel when no type is given
        `!fra logroutes reset` — clear every route
        """
        import re as _re

        from ..core import log_routes as lr
        from .display import ACTION_DISPLAY

        state = StateRepo(self.bot.db)
        action = (action or "list").lower()

        def _channel_id(token: str) -> int | None:
            m = _re.fullmatch(r"<#?(\d+)>|(\d+)", (token or "").strip())
            return int(m.group(1) or m.group(2)) if m else None

        if action in ("list", ""):
            routes = await lr.load(state)
            if not routes:
                await ctx.send(
                    "No log routes set. Add one with `!fra logroutes add "
                    "<#channel> <type>` — `!fra logroutes keys` lists the types."
                )
                return
            main = self.bot.channel_for("alliance_logs")
            lines = []
            for channel_id, targets in routes.items():
                lines.append(f"<#{channel_id}> ← {', '.join(f'`{t}`' for t in targets)}")
            note = ""
            if main is not None and main.id in routes:
                note = ("\n⚠️ A route points at the main log channel "
                        f"(<#{main.id}>); those copies are suppressed to avoid "
                        "double-posting.")
            await ctx.send(("🔀 **Log routes** (duplicated in addition to the "
                            "main log channel):\n" + "\n".join(lines) + note)[:1990])
            return

        if action == "keys":
            group_lines = [
                f"`{name}` → {len(members)} types"
                for name, members in lr.GROUPS.items()
            ]
            key_lines = []
            for key in sorted(ACTION_DISPLAY):
                title, _, emoji = ACTION_DISPLAY[key]
                key_lines.append(f"`{key}` — {emoji} {title}")
            body = (
                "**Group aliases**\n" + "\n".join(group_lines)
                + "\n\n**Special:** `all` (every type, incl. future/unknown), "
                "`unknown` (unclassified lines)\n\n"
                "**Exact keys**\n" + "\n".join(key_lines)
            )
            await ctx.send(body[:1990])
            return

        if action == "add":
            if len(args) < 2:
                await ctx.send(
                    "Usage: `!fra logroutes add <#channel> <type…>` "
                    "(e.g. `!fra logroutes add #construction building`)"
                )
                return
            channel_id = _channel_id(args[0])
            if channel_id is None:
                await ctx.send("❌ Give the channel as a #mention or its id first.")
                return
            if self.bot.get_channel(channel_id) is None:
                await ctx.send(f"❌ I can't see a channel with id `{channel_id}`.")
                return
            bad = [t for t in args[1:] if lr.normalize_target(t) is None]
            if bad:
                await ctx.send(
                    "❌ Unknown type(s): " + ", ".join(f"`{t}`" for t in bad)
                    + ". See `!fra logroutes keys`."
                )
                return
            targets = await lr.add(state, channel_id, list(args[1:]))
            main = self.bot.channel_for("alliance_logs")
            warn = ""
            if main is not None and channel_id == main.id:
                warn = ("\n⚠️ That IS the main log channel — routed copies there "
                        "are suppressed (it already gets every log).")
            await ctx.send(
                f"✅ <#{channel_id}> now receives: "
                + ", ".join(f"`{t}`" for t in targets) + warn
            )
            return

        if action == "remove":
            if not args:
                await ctx.send("Usage: `!fra logroutes remove <#channel> [type]`")
                return
            channel_id = _channel_id(args[0])
            if channel_id is None:
                await ctx.send("❌ Give the channel as a #mention or its id.")
                return
            target = args[1] if len(args) > 1 else None
            if await lr.remove(state, channel_id, target):
                what = f"`{target}` from " if target else "all routes from "
                await ctx.send(f"🗑️ Removed {what}<#{channel_id}>.")
            else:
                await ctx.send(
                    f"Nothing to remove for <#{channel_id}>"
                    + (f" / `{target}`" if target else "") + "."
                )
            return

        if action == "reset":
            existed = await lr.clear(state)
            await ctx.send(
                "↩️ All log routes cleared." if existed
                else "There were no log routes to clear."
            )
            return

        await ctx.send("Unknown action — use `list`, `keys`, `add`, `remove` or `reset`.")

    @fra.command(name="reports")
    async def scheduled_reports(
        self, ctx: commands.Context, action: str = "list", *args: str
    ) -> None:
        """Manage the SCHEDULED report posts at runtime (no YAML edit):

        `!fra reports` — list the effective schedule
        `!fra reports add <report> <period> <cadence> <#channel>` — add one
        (cadence: daily / weekly / monthly / yearly; weekly posts Monday,
        monthly on the 1st, yearly on Jan 1, all shortly after the daily
        reset)
        `!fra reports remove <nr>` — remove an entry from the list
        `!fra reports reset` — back to the config.yaml schedule
        """
        import re as _re

        from ..core import scheduled_reports as sched
        from ..config import ScheduledReport
        from ..db.repos import StateRepo

        state = StateRepo(self.bot.db)
        entries = list(self.bot.cfg.reports.scheduled)
        action = (action or "list").lower()

        if action in ("list", ""):
            if not entries:
                await ctx.send(
                    "No scheduled reports. Add one with `!fra reports add "
                    "<report> <period> <cadence> <#channel>` — see "
                    "`!fra report list` for the report names."
                )
                return
            override = await state.get(sched.STATE_KEY) is not None
            lines = [sched.describe(e, i + 1) for i, e in enumerate(entries)]
            source = ("runtime override (`!fra reports reset` returns to "
                      "config.yaml)") if override else "config.yaml"
            await ctx.send(
                "🗓️ **Scheduled reports** — " + source + "\n"
                + "\n".join(lines)
            )
            return

        if action == "add":
            if len(args) < 4:
                await ctx.send(
                    "Usage: `!fra reports add <report> <period> <cadence> "
                    "<#channel>` (e.g. `!fra reports add treasury yesterday "
                    "daily #treasury`)"
                )
                return
            name, period, cadence, channel_raw = args[0], args[1], args[2], args[3]
            report = self.bot.reports.get(name)
            if report is None:
                await ctx.send(
                    f"❌ Unknown report `{name}` — `!fra report list` shows "
                    "the available names."
                )
                return
            period = period.lower()
            if period not in report.periods:
                await ctx.send(
                    f"❌ Report `{report.name}` doesn't support period "
                    f"`{period}` — options: {', '.join(report.periods)}."
                )
                return
            cadence = cadence.lower()
            if cadence not in sched.VALID_CADENCES:
                await ctx.send(
                    "❌ Cadence must be one of: "
                    + ", ".join(sched.VALID_CADENCES) + "."
                )
                return
            mention = _re.fullmatch(r"<#?(\d+)>|(\d+)", channel_raw.strip())
            if not mention:
                await ctx.send("❌ Give the channel as a #mention or its id.")
                return
            channel_id = int(mention.group(1) or mention.group(2))
            if self.bot.get_channel(channel_id) is None:
                await ctx.send(f"❌ I can't see a channel with id `{channel_id}`.")
                return
            entries.append(ScheduledReport(
                report=report.name, period=period, cadence=cadence,
                channel_id=channel_id,
            ))
            await sched.store_entries(state, tuple(entries))
            sched.apply(self.bot.cfg, tuple(entries))
            await ctx.send(
                f"✅ Added: {sched.describe(entries[-1], len(entries))}\n"
                "Posts land shortly after the daily reset (~00:10 New York "
                "time)."
            )
            return

        if action == "remove":
            if not args or not args[0].isdigit():
                await ctx.send("Usage: `!fra reports remove <nr>` (see the list).")
                return
            index = int(args[0])
            if not 1 <= index <= len(entries):
                await ctx.send(f"❌ There is no entry {index} — the list has "
                               f"{len(entries)}.")
                return
            removed = entries.pop(index - 1)
            await sched.store_entries(state, tuple(entries))
            sched.apply(self.bot.cfg, tuple(entries))
            await ctx.send(f"🗑️ Removed: {sched.describe(removed, index)}")
            return

        if action == "reset":
            existed = await sched.clear_override(state)
            yaml_entries = getattr(self.bot, "yaml_scheduled_reports", ())
            sched.apply(self.bot.cfg, tuple(yaml_entries))
            await ctx.send(
                "↩️ Back to the config.yaml schedule "
                f"({len(yaml_entries)} entries)."
                if existed else "There was no runtime override to reset."
            )
            return

        await ctx.send("Unknown action — use `list`, `add`, `remove` or `reset`.")

    # -- custom "Own mission" scheduling --------------------------------

    @fra.command(name="dmpanel", aliases=["messagepanel"])
    async def dm_panel(self, ctx: commands.Context) -> None:
        """(Re)post the message panel (configured channel, else this one).
        The keeper maintains it automatically afterwards."""
        keeper = self.bot.get_cog("PanelKeeperCog")
        if keeper is None:
            await ctx.send("Panel keeper not loaded.")
            return
        channel_id = getattr(self.bot.cfg.discord.channels, "dm_panel", 0)
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            channel = ctx.channel
        outcome = await keeper.ensure("dms", channel=channel, force=True)
        await ctx.send(f"✅ Message panel {outcome} in <#{channel.id}>.")

    @fra.command(name="requestpanel", aliases=["requestspanel"])
    async def request_panel(self, ctx: commands.Context) -> None:
        """(Re)post the training/building request panel (configured channel,
        else this one). The keeper maintains it automatically afterwards."""
        keeper = self.bot.get_cog("PanelKeeperCog")
        if keeper is None:
            await ctx.send("Panel keeper not loaded.")
            return
        channel_id = getattr(self.bot.cfg.discord.channels, "request_panel", 0)
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            channel = ctx.channel
        outcome = await keeper.ensure("requests", channel=channel, force=True)
        await ctx.send(f"✅ Request panel {outcome} in <#{channel.id}>.")

    @fra.command(name="academypanel", aliases=["academiespanel"])
    async def academy_panel(self, ctx: commands.Context) -> None:
        """(Re)post the academy-build panel (configured channel, else this
        one). The keeper maintains it automatically afterwards."""
        keeper = self.bot.get_cog("PanelKeeperCog")
        if keeper is None:
            await ctx.send("Panel keeper not loaded.")
            return
        channel_id = getattr(self.bot.cfg.discord.channels, "academy_panel", 0)
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            channel = ctx.channel
        outcome = await keeper.ensure("academy", channel=channel, force=True)
        await ctx.send(f"✅ Academy panel {outcome} in <#{channel.id}>.")

    @fra.command(name="classpanel", aliases=["classespanel"])
    async def class_panel(self, ctx: commands.Context) -> None:
        """(Re)post the class-availability panel (configured channel, else
        this one) with fresh numbers. The keeper and the hourly job maintain
        it automatically afterwards."""
        keeper = self.bot.get_cog("PanelKeeperCog")
        cog = self.bot.get_cog("ClassesPanelCog")
        if keeper is None or cog is None:
            await ctx.send("Panel keeper / classes panel not loaded.")
            return
        async with ctx.typing():
            await self.bot.trainings.refresh_availability()
            await cog.reload_snapshot()
            channel_id = getattr(self.bot.cfg.discord.channels, "class_panel", 0)
            channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
            if channel is None:
                channel = ctx.channel
            outcome = await keeper.ensure("classes", channel=channel, force=True)
        await ctx.send(f"✅ Class-availability panel {outcome} in <#{channel.id}>.")

    @fra.command(name="missionpanel")
    async def mission_panel(self, ctx: commands.Context) -> None:
        """(Re)post the mission-request panel to the configured channel. The
        keeper maintains it automatically afterwards."""
        keeper = self.bot.get_cog("PanelKeeperCog")
        if keeper is None:
            await ctx.send("Panel keeper not loaded.")
            return
        channel_id = self.bot.cfg.automation.mission.panel_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            await ctx.send(
                f"Configured panel channel `{channel_id}` not found; "
                "post from the target channel or fix `automation.mission.panel_channel_id`."
            )
            return
        outcome = await keeper.ensure("mission", channel=channel, force=True)
        await ctx.send(f"✅ Mission panel {outcome} in <#{channel.id}>.")

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

    @fra.command(name="savedmissions")
    async def saved_missions(self, ctx: commands.Context) -> None:
        """List the game's Saved Missions exactly as the bot sees them on
        the mission form (also refreshes the Discord chooser's cache)."""
        async with ctx.typing():
            names = await self.bot.missions_service.refresh_saved_missions()
        if names:
            lines = "\n".join(f"`{i}.` {name}" for i, name in enumerate(names, 1))
            await ctx.send(f"💾 **Saved missions on the form ({len(names)}):**\n{lines}"[:1900])
            return
        await ctx.send(
            "❌ No saved missions visible on the mission form. Either the "
            "alliance has none saved, or the game draws the block with "
            "JavaScript and Playwright isn't installed. "
            "`!fra dump /missionAllianceNew rendered` uploads the page for "
            "inspection."
        )

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
        started = dt.datetime.now(dt.timezone.utc)
        message = await ctx.send("⏳ Syncing board guides…")
        lines: list[str] = []
        current = {"label": "starting"}

        async def _heartbeat() -> None:
            # A live status line so the command NEVER looks silently stuck:
            # elapsed time + the shared MC request backlog (congestion gauge).
            while True:
                await asyncio.sleep(20)
                elapsed = int(
                    (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
                )
                backlog = self.bot.mc.pacer_backlog
                hint = (
                    " — backlog high: lower `missionchief.max_delay` (default 9.0)"
                    if backlog > 5 else ""
                )
                status = (
                    f"⏳ {current['label']}… {elapsed // 60}m{elapsed % 60:02d}s "
                    f"elapsed · {backlog} MC request(s) queued{hint}"
                )
                try:
                    await message.edit(
                        content="\n".join([*lines, status])[:1990]
                    )
                except discord.HTTPException:
                    return

        async def _run(label: str, job: str, coro_factory) -> None:
            current["label"] = (
                f"{label} (waiting for the running `{job}` poll)"
                if self.bot.job_lock(job).locked() else label
            )
            try:
                async with asyncio.timeout(15 * 60):
                    async with self.bot.job_lock(job):
                        current["label"] = label
                        result = await coro_factory()
                        if isinstance(result, list):
                            lines.extend(result)
                        else:
                            lines.append(result)
            except TimeoutError:
                lines.append(
                    f"⏱️ {label}: gave up after 15 min — the MissionChief "
                    "request queue is congested (see `!fra status` backlog; "
                    "check `missionchief.max_delay`, default 9.0)."
                )

        heartbeat = asyncio.create_task(_heartbeat())
        try:
            if auto.training.enabled:
                await _run(
                    "trainings guide", "board-trainings",
                    lambda: self.bot.trainings.force_guide(repost=repost),
                )
            if auto.building.enabled:
                await _run(
                    "building guide", "board-buildings",
                    lambda: self.bot.buildings.force_guide(repost=repost),
                )
            boards = self.bot.missions_service._request_boards()
            if boards:
                async def _mission_guides() -> list[str]:
                    return [
                        await self.bot.missions_service.force_guide(
                            thread_id, kind, repost=repost
                        )
                        for thread_id, kind in boards
                    ]
                await _run("mission/event guides", "missions", _mission_guides)
        except Exception as exc:  # noqa: BLE001 — surface it to the admin
            log.exception("guide sync failed")
            lines.append(f"❌ guide sync aborted: {exc}")
        finally:
            heartbeat.cancel()
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

    @fra.command(name="settax")
    async def set_tax(
        self, ctx: commands.Context, building_id: int, percent: int | None = None
    ) -> None:
        """Set an alliance building's tax, e.g. `!fra settax 5561931 20`.

        Without a percentage the configured default
        (`automation.building.set_tax_percent`) is used. The result is
        verified against the building page.
        """
        from ..mc.building_tax import set_building_tax

        target = percent if percent is not None else (
            self.bot.cfg.automation.building.set_tax_percent
        )
        if target not in (0, 10, 20, 30, 40, 50):
            await ctx.send("⚠️ Tax must be one of 0/10/20/30/40/50.")
            return
        message = await ctx.send(
            f"⏳ Setting tax of building {building_id} to {target}%…"
        )
        ok, detail = await set_building_tax(self.bot.mc, building_id, target)
        await message.edit(
            content=f"{'✅' if ok else '❌'} Building {building_id}: {detail}"
        )

    @fra.command(name="taxwarnings", aliases=["taxwarning", "taxwarn"])
    async def tax_warnings(
        self, ctx: commands.Context, action: str = "", *, target: str = ""
    ) -> None:
        """Member 5%-donation warnings. `!fra taxwarnings` lists everyone
        below the minimum with their warning count; `!fra taxwarnings scan`
        runs a warning pass now (works with the schedule off; dry-run only
        reports); `!fra taxwarnings reset <all|username>` wipes recorded
        warning trails (e.g. after warnings were logged but never
        delivered)."""
        svc = self.bot.tax_warnings
        if action.lower() == "reset":
            from ..db.repos import TaxWarningsRepo

            repo = TaxWarningsRepo(self.bot.db)
            who = target.strip()
            if not who:
                await ctx.send(
                    "⚠️ Say who: `!fra taxwarnings reset all` or "
                    "`!fra taxwarnings reset <username>`."
                )
                return
            if who.lower() == "all":
                removed = await repo.reset_all()
                await ctx.send(
                    f"🧹 Cleared {removed} warning trail(s). Everyone starts "
                    "fresh at warning 1."
                )
                return
            removed = await repo.reset_by_username(who)
            if removed:
                await ctx.send(f"🧹 Warning trail for **{who}** cleared.")
            else:
                await ctx.send(f"Nothing recorded for `{who}` — nothing to clear.")
            return
        if action.lower() == "scan":
            message = await ctx.send("⏳ Running a tax-warning scan…")
            lines = await svc.scan(force=True)
            body = "\n".join(lines) if lines else "Nothing to do — nobody is due."
            await message.edit(content=f"💰 Tax warnings:\n{body}"[:1900])
            return
        lines = await svc.overview()
        auto = self.bot.cfg.automation.tax_warnings
        head = (
            f"💰 Members below {auto.min_rate:g}% donation "
            f"(warnings: {'on' if auto.enabled else 'OFF'}, "
            f"auto-kick: {'on' if auto.auto_kick else 'off'}):"
        )
        body = "\n".join(lines) if lines else "Nobody — everyone meets the minimum. 🎉"
        await ctx.send(f"{head}\n{body}"[:1900])

    @fra.group(
        name="missionsforum",
        aliases=["missionforum", "mforum"],
        invoke_without_command=True,
    )
    async def missions_forum_group(self, ctx: commands.Context) -> None:
        """Missions-database forum status. Subcommands: `sync [limit|force]`
        runs a sync now; `adopt` rebuilds the mapping from existing posts."""
        lines = await self.bot.missions_forum.status_lines()
        await ctx.send("📚 **Missions forum**\n" + "\n".join(lines)[:1900])

    @missions_forum_group.command(name="sync")
    async def missions_forum_sync(
        self, ctx: commands.Context, arg: str = ""
    ) -> None:
        """Run a missions-forum sync now. `!fra missionsforum sync 25` caps
        this run at 25 posts; `!fra missionsforum sync force` re-renders
        every post even when nothing changed."""
        lock = self.bot.job_lock("missions-forum")
        if lock.locked():
            await ctx.send("⏳ A missions-forum sync is already running.")
            return
        force = arg.strip().lower() == "force"
        limit = None
        if not force and arg.strip():
            try:
                limit = max(1, int(arg))
            except ValueError:
                await ctx.send("⚠️ Use a number (post cap) or `force`.")
                return
        message = await ctx.send("⏳ Syncing the missions forum…")
        async with lock:
            try:
                summary = await asyncio.wait_for(
                    self.bot.missions_forum.sync(limit=limit, force=force),
                    timeout=45 * 60,
                )
            except asyncio.TimeoutError:
                await message.edit(
                    content="⏱️ Sync timed out after 45 min — aborted; run it "
                            "again to continue where it left off."
                )
                return
            except Exception as exc:  # surfaced to the operator, not a crash
                log.exception("Manual missions-forum sync failed")
                await message.edit(content=f"❌ Sync failed: {exc}")
                return
        icon = "❌" if summary.get("error") else "✅"
        await message.edit(
            content=f"{icon} Missions forum:\n" + "\n".join(summary["lines"])[:1800]
        )

    @missions_forum_group.command(name="stop")
    async def missions_forum_stop(self, ctx: commands.Context) -> None:
        """Stop the running missions-forum sync (or wipe) after the post
        it is currently working on."""
        if not self.bot.job_lock("missions-forum").locked():
            await ctx.send("There is no missions-forum sync running.")
            return
        self.bot.missions_forum.request_stop()
        await ctx.send("🛑 Stopping the missions-forum run after the current post…")

    @missions_forum_group.command(name="wipe")
    async def missions_forum_wipe(
        self, ctx: commands.Context, confirm: str = ""
    ) -> None:
        """Delete ALL mission posts from the forum and forget the mapping.
        Destructive — requires the literal word CONFIRM:
        `!fra missionsforum wipe CONFIRM`."""
        if confirm != "CONFIRM":
            count = 0
            try:
                from ..db.repos import MissionsForumRepo

                count = await MissionsForumRepo(self.bot.db).count()
            except Exception:  # count is cosmetic only
                pass
            await ctx.send(
                f"⚠️ This deletes **every** mission post ({count} tracked) "
                "from the forum. If you are sure: `!fra missionsforum wipe CONFIRM`"
            )
            return
        lock = self.bot.job_lock("missions-forum")
        if lock.locked():
            await ctx.send(
                "⏳ A missions-forum run is busy — `!fra missionsforum stop` "
                "it first."
            )
            return
        message = await ctx.send("🧹 Wiping the missions forum… (this is paced)")
        async with lock:
            summary = await self.bot.missions_forum.wipe()
        icon = "❌" if summary.get("error") else "✅"
        await message.edit(
            content=f"{icon} Missions forum wipe:\n"
                    + "\n".join(summary["lines"])[:1800]
        )

    @missions_forum_group.command(name="adopt")
    async def missions_forum_adopt(self, ctx: commands.Context) -> None:
        """Rebuild the mission→post mapping from the forum's thread titles
        (recovery after a database loss — prevents duplicate posts)."""
        forum = self.bot.missions_forum.forum()
        if forum is None:
            await ctx.send(
                "⚠️ No missions forum configured — `!fra set missions_forum <id>`."
            )
            return
        message = await ctx.send("⏳ Scanning forum threads…")
        adopted = await self.bot.missions_forum.adopt(forum)
        await message.edit(
            content=f"✅ Adopted {adopted} post(s). Content refreshes on the next sync."
        )

    @fra.group(
        name="vehiclesforum",
        aliases=["vehicleforum", "vforum"],
        invoke_without_command=True,
    )
    async def vehicles_forum_group(self, ctx: commands.Context) -> None:
        """Vehicles-database forum status. Subcommands: `sync [limit|force]`
        runs a sync now; `adopt` rebuilds the mapping from existing posts."""
        lines = await self.bot.vehicles_forum.status_lines()
        await ctx.send("🚒 **Vehicles forum**\n" + "\n".join(lines)[:1900])

    @vehicles_forum_group.command(name="sync")
    async def vehicles_forum_sync(
        self, ctx: commands.Context, arg: str = ""
    ) -> None:
        """Run a vehicles-forum sync now. `!fra vehiclesforum sync 25` caps
        this run at 25 posts; `!fra vehiclesforum sync force` re-renders every
        post even when nothing changed."""
        lock = self.bot.job_lock("vehicles-forum")
        if lock.locked():
            await ctx.send("⏳ A vehicles-forum sync is already running.")
            return
        force = arg.strip().lower() == "force"
        limit = None
        if not force and arg.strip():
            try:
                limit = max(1, int(arg))
            except ValueError:
                await ctx.send("⚠️ Use a number (post cap) or `force`.")
                return
        message = await ctx.send("⏳ Syncing the vehicles forum…")
        async with lock:
            try:
                summary = await asyncio.wait_for(
                    self.bot.vehicles_forum.sync(limit=limit, force=force),
                    timeout=45 * 60,
                )
            except asyncio.TimeoutError:
                await message.edit(
                    content="⏱️ Sync timed out after 45 min — aborted; run it "
                            "again to continue where it left off."
                )
                return
            except Exception as exc:  # surfaced to the operator, not a crash
                log.exception("Manual vehicles-forum sync failed")
                await message.edit(content=f"❌ Sync failed: {exc}")
                return
        icon = "❌" if summary.get("error") else "✅"
        await message.edit(
            content=f"{icon} Vehicles forum:\n" + "\n".join(summary["lines"])[:1800]
        )

    @vehicles_forum_group.command(name="stop")
    async def vehicles_forum_stop(self, ctx: commands.Context) -> None:
        """Stop the running vehicles-forum sync (or wipe) after the post it
        is currently working on."""
        if not self.bot.job_lock("vehicles-forum").locked():
            await ctx.send("There is no vehicles-forum sync running.")
            return
        self.bot.vehicles_forum.request_stop()
        await ctx.send("🛑 Stopping the vehicles-forum run after the current post…")

    @vehicles_forum_group.command(name="wipe")
    async def vehicles_forum_wipe(
        self, ctx: commands.Context, confirm: str = ""
    ) -> None:
        """Delete ALL vehicle posts from the forum and forget the mapping.
        Destructive — requires the literal word CONFIRM:
        `!fra vehiclesforum wipe CONFIRM`."""
        if confirm != "CONFIRM":
            count = 0
            try:
                from ..db.repos import VehiclesForumRepo

                count = await VehiclesForumRepo(self.bot.db).count()
            except Exception:  # count is cosmetic only
                pass
            await ctx.send(
                f"⚠️ This deletes **every** vehicle post ({count} tracked) "
                "from the forum. If you are sure: `!fra vehiclesforum wipe CONFIRM`"
            )
            return
        lock = self.bot.job_lock("vehicles-forum")
        if lock.locked():
            await ctx.send(
                "⏳ A vehicles-forum run is busy — `!fra vehiclesforum stop` "
                "it first."
            )
            return
        message = await ctx.send("🧹 Wiping the vehicles forum… (this is paced)")
        async with lock:
            summary = await self.bot.vehicles_forum.wipe()
        icon = "❌" if summary.get("error") else "✅"
        await message.edit(
            content=f"{icon} Vehicles forum wipe:\n"
                    + "\n".join(summary["lines"])[:1800]
        )

    @vehicles_forum_group.command(name="adopt")
    async def vehicles_forum_adopt(self, ctx: commands.Context) -> None:
        """Rebuild the vehicle→post mapping from the forum's thread titles
        (recovery after a database loss — prevents duplicate posts)."""
        forum = self.bot.vehicles_forum.forum()
        if forum is None:
            await ctx.send(
                "⚠️ No vehicles forum configured — `!fra set vehicles_forum <id>`."
            )
            return
        message = await ctx.send("⏳ Scanning forum threads…")
        adopted = await self.bot.vehicles_forum.adopt(forum)
        await message.edit(
            content=f"✅ Adopted {adopted} post(s). Content refreshes on the next sync."
        )

    @fra.group(name="dmmirror", aliases=["dms", "dmirror"], invoke_without_command=True)
    async def dm_mirror_group(self, ctx: commands.Context) -> None:
        """In-game DM mirror status. `!fra dmmirror scan` runs an inbox
        scan now (works with the schedule off)."""
        lines = await self.bot.dm_mirror.status_lines()
        await ctx.send("📬 **DM mirror**\n" + "\n".join(lines)[:1900])

    @dm_mirror_group.command(name="scan")
    async def dm_mirror_scan(self, ctx: commands.Context) -> None:
        """Scan the in-game PM inbox now and mirror conversations to the
        forum."""
        lock = self.bot.job_lock("dm-mirror")
        if lock.locked():
            await ctx.send("⏳ A DM-mirror scan is already running.")
            return
        message = await ctx.send("⏳ Scanning the in-game inbox…")
        async with lock:
            try:
                summary = await self.bot.dm_mirror.scan()
            except Exception as exc:  # surfaced to the operator, not a crash
                log.exception("Manual DM-mirror scan failed")
                await message.edit(content=f"❌ Scan failed: {exc}")
                return
        icon = "❌" if summary.get("error") else "✅"
        await message.edit(
            content=f"{icon} DM mirror:\n" + "\n".join(summary["lines"])[:1800]
        )

    @fra.command(name="dm")
    async def dm_send(self, ctx: commands.Context, *, spec: str = "") -> None:
        """Start a new in-game PM from Discord (the old bot's Send Message):
        `!fra dm <username> | <subject> | <body>`. The username is matched
        case-insensitively against the alliance roster; the conversation is
        mirrored into the DM forum right away so you can continue there."""
        parts = [p.strip() for p in spec.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            await ctx.send(
                "⚠️ Use: `!fra dm <username> | <subject> | <body>` "
                "(three parts separated by `|`)."
            )
            return
        recipient, subject, body = parts
        message = await ctx.send(f"⏳ Sending PM to **{recipient}**…")
        result = await self.bot.dm_mirror.send_new(recipient, subject, body)
        if not result["ok"]:
            await message.edit(content=f"❌ Not sent: {result['detail']}"[:1900])
            return
        thread = result.get("thread")
        where = f" — continue in {thread.mention}" if thread is not None else ""
        await message.edit(content=f"✅ PM sent to **{recipient}**{where}")

    @fra.command(name="rankroles", aliases=["rankrole", "ranks"])
    async def rank_roles(self, ctx: commands.Context, action: str = "") -> None:
        """Credit rank roles. `!fra rankroles sync` runs a sync now;
        `!fra rankroles dryrun` previews without changing anything."""
        action = action.strip().lower()
        if action in ("sync", "dryrun"):
            lock = self.bot.job_lock("rank-roles")
            if lock.locked():
                await ctx.send("⏳ A rank-role sync is already running.")
                return
            message = await ctx.send(
                "⏳ Previewing rank roles…" if action == "dryrun"
                else "⏳ Syncing rank roles…"
            )
            async with lock:
                try:
                    summary = await self.bot.rank_roles.sync(
                        dry_run=(action == "dryrun")
                    )
                except Exception as exc:
                    log.exception("Manual rank-role sync failed")
                    await message.edit(content=f"❌ Sync failed: {exc}")
                    return
            icon = "❌" if summary.get("error") else "✅"
            await message.edit(
                content=f"{icon} Rank roles:\n" + "\n".join(summary["lines"])[:1800]
            )
            return
        auto = self.bot.cfg.automation.rank_roles
        await ctx.send(
            "🎖️ **Rank roles**\n"
            f"schedule: {'every ' + str(auto.interval) + ' min' if auto.enabled else 'OFF'}\n"
            f"promotions → <#{auto.promotion_channel_id}>\n"
            "`!fra rankroles dryrun` previews, `!fra rankroles sync` runs now."
        )

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

    @fra.command(name="dailybuild")
    async def daily_build_now(self, ctx: commands.Context) -> None:
        """Run the daily worldwide auto-build now (hospital + prison at a
        random real location). Works even when the schedule is off; in
        dry-run it only reports what it would build. A real build consumes
        today's slot so the scheduled run won't build again."""
        lock = self.bot.job_lock("daily-build")
        if lock.locked():
            await ctx.send("⏳ The daily build is already running — skipped.")
            return
        message = await ctx.send(
            "⏳ Running the daily worldwide auto-build… "
            "(geocode + OpenStreetMap lookups can take a minute)"
        )
        async with lock:
            self.bot.presence.mark_running("daily-build")
            try:
                summary = await self.bot.buildings.daily_build(force=True)
            except Exception as exc:  # noqa: BLE001 - surfaced to the admin
                log.exception("Manual daily build failed")
                await message.edit(content=f"❌ Daily build failed: {exc}")
                return
            finally:
                self.bot.presence.mark_done("daily-build")
        if not summary:
            await message.edit(content="Daily build produced no actions.")
            return
        await message.edit(
            content="\n".join(["🏗️ Daily worldwide auto-build:"] + summary)[:1900]
        )

    @fra.command(name="diag", aliases=["diagnose", "doctor"])
    async def diag(self, ctx: commands.Context) -> None:
        """One-shot health check of every automation path.

        Fetches the real MissionChief pages and reports exactly where a
        flow breaks: session, alliance funds parse, academy list, education
        form, and whether Playwright (needed for building) is installed.
        """
        await ctx.send("⏳ Running diagnostics… (a few MissionChief fetches)")
        try:
            lines = await self._run_diagnostics()
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not crash
            log.exception("diagnostics failed")
            await ctx.send(f"❌ Diagnostics errored: {exc}")
            return
        await ctx.send("```\n" + "\n".join(lines)[:1900] + "\n```")

    async def _run_diagnostics(self) -> list[str]:
        import html as html_lib
        import re

        from ..mc.browser_builder import BrowserBuilder
        from ..mc.errors import MissionChiefError
        from ..mc.parsers.academy import (
            parse_academy_page,
            parse_alliance_buildings_page,
        )
        from ..mc.parsers.treasury import parse_total_funds

        cfg = self.bot.cfg
        lines = [
            f"dry_run: {'ON' if cfg.automation.dry_run else 'OFF'}",
            "playwright: " + (
                "installed"
                if BrowserBuilder.available()
                else "NOT INSTALLED — building can never run without it "
                     "(see README: pip install playwright)"
            ),
        ]

        # Alliance funds: fetch + parse, and show what the page actually
        # says when the parse finds nothing.
        try:
            kasse = await self.bot.mc.fetch_page("/verband/kasse")
        except MissionChiefError as exc:
            kasse = None
            lines.append(f"kasse fetch: FAILED — {exc}")
        if kasse is not None:
            funds = parse_total_funds(kasse)
            if funds is not None:
                lines.append(f"alliance funds: {funds:,} (plain HTML parse OK)")
            else:
                text = re.sub(
                    r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", kasse))
                )
                pos = text.lower().find("credits")
                around = (
                    "…" + text[max(0, pos - 90): pos + 30].strip() + "…"
                    if pos >= 0 else "no 'Credits' text on the page at all"
                )
                lines.append(
                    f"alliance funds: NOT FOUND in plain HTML ({len(kasse):,} chars)"
                )
                lines.append(f"  around first 'Credits': {around}")
                # The figure is drawn by JavaScript on this page — test the
                # rendered fallback the build flow uses, so the diag proves
                # end-to-end whether funds can be read at all.
                if BrowserBuilder.available():
                    from ..mc.browser_builder import cookies_for, render_page

                    try:
                        base = cfg.missionchief.base_url
                        rendered = await render_page(
                            base,
                            cookies_for(base, self.bot.mc.session.cookie_jar),
                            "/verband/kasse",
                        )
                        rendered_funds = parse_total_funds(rendered)
                        if rendered_funds is not None:
                            lines.append(
                                f"alliance funds (rendered): {rendered_funds:,} "
                                "— the build flow will use this fallback"
                            )
                        else:
                            lines.append(
                                "alliance funds (rendered): STILL NOT FOUND "
                                f"({len(rendered):,} chars) — layout change?"
                            )
                    except Exception as exc:  # noqa: BLE001 - diagnostic only
                        lines.append(f"alliance funds (rendered): FAILED — {exc}")

        # Academy list: what the trainings flow can actually see.
        try:
            listing_html = await self.bot.mc.fetch_page("/verband/gebauede")
        except MissionChiefError as exc:
            listing_html = None
            lines.append(f"academy list fetch: FAILED — {exc}")
        first = None
        if listing_html is not None:
            listings = parse_alliance_buildings_page(listing_html)
            per: dict[str, list[int]] = {}
            for a in listings:
                stats = per.setdefault(a.discipline or "other buildings", [0, 0])
                stats[0] += 1
                if a.has_start_button:
                    stats[1] += 1
            if listings:
                lines.append(
                    "academies (page 1): " + ", ".join(
                        f"{d}: {n} ({s} startable)"
                        for d, (n, s) in sorted(per.items())
                    )
                )
                first = next((a for a in listings if a.has_start_button), None)
            else:
                hint = (
                    "start-course text IS on the page — parser mismatch"
                    if "start a new training course" in listing_html.lower()
                    else "no start-course text — wrong page or no permissions?"
                )
                lines.append(
                    f"academies (page 1): NONE PARSED "
                    f"({len(listing_html):,} chars; {hint})"
                )

        # Education form on the first startable academy.
        if first is not None:
            try:
                page = parse_academy_page(
                    await self.bot.mc.fetch_page(f"/buildings/{first.building_id}")
                )
                free_ok = not page.costs or 0 in page.costs
                lines.append(
                    f"academy {first.building_id} ({first.name or first.discipline}): "
                    f"form={'yes' if page.action else 'NO'}, "
                    f"token={'yes' if page.authenticity_token else 'NO'}, "
                    f"free rooms={page.available_rooms}, "
                    f"courses={len(page.courses)}, "
                    f"free class={'yes' if free_ok else 'NO'}"
                )
            except MissionChiefError as exc:
                lines.append(f"academy {first.building_id}: fetch FAILED — {exc}")

        return lines

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
