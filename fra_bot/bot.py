"""Discord bot wiring: services, scheduler and cogs."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord.ext import commands

from .config import Config
from .core.pacing import HumanPacer
from .core.scheduler import Scheduler
from .db.database import Database
from .db.repos import StateRepo
from .geo.geocoder import Geocoder
from .presence import PresenceManager
from .mc.client import MissionChiefClient
from .services.applications_sync import ApplicationsSyncService
from .services.buildings import BuildingsService
from .services.events import EventsService
from .services.logs_sync import LogsSyncService
from .services.members_sync import MembersSyncService
from .services.missions import MissionScheduler
from .services.trainings import TrainingsService
from .services.treasury_sync import TreasurySyncService

log = logging.getLogger(__name__)


class FRABot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        self.cfg = cfg
        self.db = Database(cfg.database.path)
        self.pacer = HumanPacer(
            min_delay=cfg.missionchief.min_delay,
            max_delay=cfg.missionchief.max_delay,
            max_per_minute=cfg.missionchief.max_requests_per_minute,
            cooldown_seconds=cfg.missionchief.circuit_breaker_cooldown_minutes * 60.0,
        )
        self.mc = MissionChiefClient(cfg.missionchief, self.pacer)
        self.scheduler = Scheduler()
        self.geocoder = Geocoder(
            StateRepo(self.db),
            base_url=cfg.geocoding.base_url,
            api_key=cfg.geocoding.api_key,
            api_key_param=cfg.geocoding.api_key_param,
            contact_email=cfg.geocoding.contact_email,
            min_interval=cfg.geocoding.min_interval,
        )
        self.presence = PresenceManager(self)

        from .reporting import ReportRegistry
        from .reporting.reports import register_builtin_reports

        self.reports = ReportRegistry()
        register_builtin_reports(self.reports, self.db)
        # Per-job locks so a manual !fra sync can't run a second copy of
        # a job that's already running on the scheduler (which would
        # duplicate ledger rows and repeat real MissionChief actions).
        self._job_locks: dict[str, asyncio.Lock] = {}

        self.members_sync = MembersSyncService(cfg, self.mc, self.db)
        self.applications_sync = ApplicationsSyncService(self.mc, self.db)
        self.logs_sync = LogsSyncService(self.mc, self.db)
        self.treasury_sync = TreasurySyncService(cfg, self.mc, self.db)

        # Phase 2 board automation.
        self.trainings = TrainingsService(cfg, self.mc, self.db)
        self.buildings = BuildingsService(cfg, self.mc, self.db, self.geocoder)
        # Events and custom missions both start large alliance missions on the
        # same alliance-wide free cooldown; a shared lock serializes their
        # check-then-start so one free window is never double-spent.
        self._large_mission_lock = asyncio.Lock()
        self.events = EventsService(
            cfg, self.mc, self.db, self.geocoder, start_lock=self._large_mission_lock
        )
        # Custom "Own mission" scheduling (Discord panel/slash + board).
        self.missions_service = MissionScheduler(
            cfg, self.mc, self.db, self.geocoder, start_lock=self._large_mission_lock
        )

        self._jobs_started = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        from .db.repos import RunsRepo

        orphans = await RunsRepo(self.db).close_orphans()
        if orphans:
            log.info("Marked %d interrupted scrape run(s) as failed", orphans)
        from .db.repos import AutomationRepo, MissionsRepo

        stranded = await AutomationRepo(self.db).sweep_processing()
        if stranded:
            log.warning(
                "Flagged %d board request(s) interrupted mid-action for review",
                stranded,
            )
        stranded_missions = await MissionsRepo(self.db).sweep_processing()
        if stranded_missions:
            log.warning(
                "Flagged %d scheduled mission(s) interrupted mid-start for review",
                stranded_missions,
            )
        await self.mc.start()
        await self.geocoder.start()

        from .cogs.admin import AdminCog
        from .cogs.automation import AutomationCog
        from .cogs.missions import MissionPanelView, MissionsCog
        from .cogs.notifications import NotificationsCog
        from .cogs.reporting import ReportingCog
        from .cogs.reports import ReportsCog

        await self.add_cog(AdminCog(self))
        await self.add_cog(NotificationsCog(self))
        await self.add_cog(ReportsCog(self))
        await self.add_cog(AutomationCog(self))
        await self.add_cog(ReportingCog(self))
        await self.add_cog(MissionsCog(self))

        # Persistent mission panel survives restarts; register its view.
        missions_cog = self.get_cog("MissionsCog")
        if missions_cog is not None:
            self.add_view(MissionPanelView(missions_cog))

        # Register the /mission slash command with the guild for fast
        # propagation (global sync can take up to an hour).
        if self.cfg.discord.guild_id:
            guild = discord.Object(id=self.cfg.discord.guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                log.warning("Slash-command sync failed: %s", exc)

    async def on_command_error(self, ctx, error) -> None:
        from discord.ext import commands as _commands

        if isinstance(error, _commands.CommandNotFound):
            return
        if isinstance(error, _commands.CheckFailure):
            try:
                await ctx.send("⛔ You don't have permission to use that command.")
            except discord.HTTPException:
                pass
            return
        if isinstance(error, _commands.UserInputError):
            try:
                await ctx.send(f"⚠️ {error}")
            except discord.HTTPException:
                pass
            return
        log.exception("Command error in %s", getattr(ctx, "command", None), exc_info=error)
        try:
            await ctx.send("❌ Something went wrong running that command.")
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        log.info("Logged in to Discord as %s (%s)", self.user, self.user.id)
        if not self._jobs_started:
            self._jobs_started = True
            self.presence.start()
            self._start_jobs()
            await self._announce_restart_if_updated()

    async def _announce_restart_if_updated(self) -> None:
        """If we just restarted from !fra update / !fra restart, confirm."""
        from .selfupdate import read_and_clear_restart_marker

        marker = read_and_clear_restart_marker(self.cfg.database.path)
        if not marker:
            return
        channel = self.get_channel(int(marker.get("channel_id", 0)))
        if channel is None:
            return
        old_rev, new_rev = marker.get("old_rev", "?"), marker.get("new_rev", "?")
        reason = marker.get("reason", "update")
        if reason == "restart":
            description = f"Restarted (config reloaded) — running `{new_rev}`."
        elif old_rev != new_rev:
            description = f"Update applied — now running `{new_rev}` (was `{old_rev}`)."
        else:
            description = f"Restarted — running `{new_rev}`."
        embed = discord.Embed(
            title="✅ Bot restarted",
            colour=discord.Colour.green(),
            description=description,
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Could not post restart confirmation: %s", exc)

    def _start_jobs(self) -> None:
        sync = self.cfg.sync
        sched = self.scheduler

        sched.add_interval_job(
            self._guarded(self.applications_sync.run, "applications"),
            minutes=sync.applications_interval,
            name="applications",
            initial_delay_seconds=20.0,
        )
        sched.add_interval_job(
            self._guarded(self.members_sync.run, "members"),
            minutes=sync.members_interval,
            name="members",
            initial_delay_seconds=90.0,
        )
        sched.add_interval_job(
            self._guarded(self.logs_sync.run, "logs"),
            minutes=sync.logs_interval,
            name="logs",
            initial_delay_seconds=45.0,
        )
        sched.add_interval_job(
            self._guarded(self.treasury_sync.sync_balance_and_income, "treasury"),
            minutes=sync.treasury_interval,
            name="treasury",
            initial_delay_seconds=300.0,
        )
        sched.add_interval_job(
            self._guarded(self.treasury_sync.backfill_step, "expenses-backfill"),
            minutes=sync.expenses_backfill_interval,
            name="expenses-backfill",
            initial_delay_seconds=600.0,
        )
        sched.add_interval_job(
            self._guarded(
                self.treasury_sync.sync_expenses_incremental, "expenses"
            ),
            minutes=sync.expenses_interval,
            name="expenses",
            initial_delay_seconds=420.0,
        )
        # Final pre-reset capture of the daily/monthly income standings.
        sched.add_daily_job(
            self._guarded(self.treasury_sync.sync_balance_and_income, "pre-reset"),
            at=dt.time(23, 52),
            timezone=self.cfg.reports.timezone,
            name="treasury-pre-reset",
        )

        # Phase 2 board automation pollers (each gated by its own switch).
        automation = self.cfg.automation
        if automation.training.enabled:
            sched.add_interval_job(
                self._guarded(self.trainings.poll, "board-trainings"),
                minutes=automation.training.interval,
                name="board-trainings",
                initial_delay_seconds=150.0,
            )
        if automation.building.enabled:
            sched.add_interval_job(
                self._guarded(self.buildings.poll, "board-buildings"),
                minutes=automation.building.interval,
                name="board-buildings",
                initial_delay_seconds=210.0,
            )
        if automation.events.enabled:
            sched.add_interval_job(
                self._guarded(self.events.poll, "board-events"),
                minutes=automation.events.interval,
                name="board-events",
                initial_delay_seconds=270.0,
            )
        if automation.mission.enabled:
            sched.add_interval_job(
                self._guarded(self.missions_service.poll, "missions"),
                minutes=automation.mission.interval,
                name="missions",
                initial_delay_seconds=330.0,
            )
        log.info(
            "Background jobs scheduled (automation: dry_run=%s, training=%s, "
            "building=%s, events=%s, mission=%s)",
            automation.dry_run, automation.training.enabled,
            automation.building.enabled, automation.events.enabled,
            automation.mission.enabled,
        )

    def _guarded(self, func, name: str):
        """Wrap a sync job so scheduler jobs log-and-continue on errors,
        and pause quietly while the circuit breaker is open."""

        async def runner() -> None:
            from .core.pacing import CircuitOpenError
            from .mc.errors import MissionChiefError

            lock = self.job_lock(name)
            if lock.locked():
                log.info("Job %s already running; skipping this tick", name)
                return
            async with lock:
                self.presence.mark_running(name)
                try:
                    await func()
                except CircuitOpenError as exc:
                    log.warning("Job %s skipped: %s", name, exc)
                except MissionChiefError as exc:
                    log.error("Job %s failed: %s", name, exc)
                    await self.notify_admin(f"⚠️ Sync job **{name}** failed: {exc}")
                finally:
                    self.presence.mark_done(name)

        return runner

    def job_lock(self, name: str) -> asyncio.Lock:
        """The shared lock for a job, so scheduled and manual runs of the
        same job never overlap."""
        lock = self._job_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._job_locks[name] = lock
        return lock

    # ------------------------------------------------------------------
    # Discord helpers
    # ------------------------------------------------------------------

    def channel_for(self, key: str) -> discord.abc.Messageable | None:
        channel_id = getattr(self.cfg.discord.channels, key, 0)
        if not channel_id:
            return None
        return self.get_channel(channel_id)

    async def notify_admin(self, message: str) -> None:
        channel = self.channel_for("admin_log")
        if channel is None:
            return
        try:
            await channel.send(message[:1900])
        except discord.HTTPException as exc:
            log.warning("Could not post to admin channel: %s", exc)

    async def close(self) -> None:
        log.info("Shutting down…")
        await self.presence.stop()
        await self.scheduler.stop()
        await self.geocoder.close()
        await self.mc.close()
        await self.db.close()
        await super().close()
