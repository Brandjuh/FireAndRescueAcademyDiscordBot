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
from .services.board_cleanup import BoardCleanupService
from .services.building_upgrade import BuildingUpgradeService
from .services.buildings import BuildingsService
from .services.events import EventsService
from .services.logs_sync import LogsSyncService
from .services.members_sync import MembersSyncService
from .services.missions import MissionScheduler
from .services.trainings import TrainingsService
from .services.treasury_sync import TreasurySyncService

log = logging.getLogger(__name__)

# Periodic safety net for requests stranded in 'processing'. A request older
# than STALE_PROCESSING_MINUTES is considered stuck (browser builds finish well
# under this), and the sweep runs every STALE_SWEEP_INTERVAL_MINUTES.
STALE_PROCESSING_MINUTES = 15
STALE_SWEEP_INTERVAL_MINUTES = 5
# The 12h board tidy-up checks for due deletions this often (live mode only).
BOARD_CLEANUP_INTERVAL_MINUTES = 10


def _parse_hhmm(value: str, *, default: tuple[int, int] = (3, 0)) -> tuple[int, int]:
    """Parse a ``"HH:MM"`` string into (hour, minute); fall back on garbage."""
    try:
        hour_s, minute_s = str(value).split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return default


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
        self.logs_sync = LogsSyncService(cfg, self.mc, self.db)
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
        # The 12h board tidy-up: removes handled request posts (live only).
        self.board_cleanup = BoardCleanupService(cfg, self.mc, self.db)
        # On-command alliance hospital/prison level + extension upgrades.
        self.building_upgrade = BuildingUpgradeService(cfg, self.mc, self.db)

        self._jobs_started = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        from .db.repos import RunsRepo

        orphans = await RunsRepo(self.db).close_orphans()
        if orphans:
            log.info("Marked %d interrupted scrape run(s) as failed", orphans)
        from .db.repos import AutomationRepo, MissionsRepo

        # In dry-run nothing real can have half-run, so re-queue an interrupted
        # request instead of failing it with a scary "verify on MissionChief".
        requeue = self.cfg.automation.dry_run
        stranded = await AutomationRepo(self.db).sweep_processing(requeue=requeue)
        if stranded:
            log.warning(
                "%s %d board request(s) interrupted mid-action",
                "Re-queued" if requeue else "Flagged", stranded,
            )
        stranded_missions = await MissionsRepo(self.db).sweep_processing(requeue=requeue)
        if stranded_missions:
            log.warning(
                "%s %d scheduled mission(s) interrupted mid-start",
                "Re-queued" if requeue else "Flagged", stranded_missions,
            )
        # Re-apply the operator's `!fra set` overrides on top of config.yaml
        # BEFORE anything schedules jobs or caches values. The pacer was
        # built pre-DB, so rewire it in case pacing was overridden.
        from .core.settings import apply_stored_overrides
        from .db.repos import StateRepo

        for line in await apply_stored_overrides(self.cfg, StateRepo(self.db)):
            log.info("settings: %s", line)
        self.mc.reconfigure_pacing(self.cfg.missionchief)

        await self.mc.start()
        await self.geocoder.start()

        from .cogs.admin import AdminCog
        from .cogs.automation import AutomationCog
        from .cogs.membersync import MemberSyncCog
        from .cogs.missions import MissionPanelView, MissionsCog
        from .cogs.notifications import NotificationsCog
        from .cogs.reporting import ReportingCog
        from .cogs.reports import ReportsCog
        from .cogs.requests_panel import RequestPanelView, RequestsCog

        await self.add_cog(AdminCog(self))
        await self.add_cog(NotificationsCog(self))
        await self.add_cog(ReportsCog(self))
        await self.add_cog(AutomationCog(self))
        await self.add_cog(ReportingCog(self))
        await self.add_cog(MissionsCog(self))
        await self.add_cog(RequestsCog(self))
        await self.add_cog(MemberSyncCog(self))

        # Persistent panels survive restarts; register their views.
        missions_cog = self.get_cog("MissionsCog")
        if missions_cog is not None:
            self.add_view(MissionPanelView(missions_cog))
        requests_cog = self.get_cog("RequestsCog")
        if requests_cog is not None:
            self.add_view(RequestPanelView(requests_cog))

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
        if isinstance(error, _commands.CommandOnCooldown):
            try:
                await ctx.send(
                    f"⏳ That command is on cooldown — try again in "
                    f"{error.retry_after:.0f}s."
                )
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
        # Full alliance-log history walk (no-ops once complete). Marks old
        # rows already-posted so history never floods the feed.
        sched.add_interval_job(
            self._guarded(self.logs_sync.backfill_step, "logs-backfill"),
            minutes=sync.logs_backfill_interval,
            name="logs-backfill",
            initial_delay_seconds=660.0,
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
        # Close to midnight NY (the reset) so the snapshot reflects the full
        # day, but with a safety margin for the fetch to finish first.
        sched.add_daily_job(
            self._guarded(self.treasury_sync.sync_balance_and_income, "pre-reset"),
            at=dt.time(23, 55),
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
        # Daily worldwide auto-build: one hospital + one prison at a real OSM
        # location. Scheduled even in dry-run (it reports what it would build);
        # the build itself honours dry_run and the funds floor.
        if automation.building.daily_build_enabled:
            hour, minute = _parse_hhmm(automation.building.daily_build_time)
            sched.add_daily_job(
                self._guarded(self.buildings.daily_build, "daily-build"),
                at=dt.time(hour, minute),
                timezone=self.cfg.reports.timezone,
                name="daily-build",
            )
        # The unified mission scheduler handles BOTH request boards — the
        # events board (kind=event) and the mission board (kind=large) — plus
        # the Discord queue and the rotation. Run it when any of those is on.
        if (
            automation.mission.enabled
            or automation.mission.board_enabled
            or automation.events.enabled
        ):
            sched.add_interval_job(
                self._guarded(self.missions_service.poll, "missions"),
                minutes=automation.mission.interval,
                name="missions",
                initial_delay_seconds=270.0,
            )
        # Safety net: release requests/missions stranded in 'processing' (an
        # action interrupted while the bot kept running). The startup sweep
        # only catches ones stranded across a restart; this catches the rest
        # without needing a restart. DB-only, so always on.
        sched.add_interval_job(
            self._sweep_stale_processing,
            minutes=STALE_SWEEP_INTERVAL_MINUTES,
            name="stale-sweep",
            initial_delay_seconds=120.0,
        )
        # The 12h board tidy-up: delete handled request posts. Destructive and
        # board-facing, so live mode only — in dry-run the other bot owns the
        # board and nothing is ever scheduled for deletion anyway.
        if not automation.dry_run:
            sched.add_interval_job(
                self._guarded(self.board_cleanup.sweep, "board-cleanup"),
                minutes=BOARD_CLEANUP_INTERVAL_MINUTES,
                name="board-cleanup",
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

    async def _sweep_stale_processing(self) -> None:
        """Release board requests / missions stuck in 'processing' for longer
        than STALE_PROCESSING_MINUTES, so a stranded one self-heals without a
        restart. DB-only; the scheduler already isolates failures."""
        from .db.repos import AutomationRepo, MissionsRepo

        cutoff = (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=STALE_PROCESSING_MINUTES)
        ).isoformat(timespec="seconds")
        # Dry-run: re-queue rather than fail (nothing real can have half-run).
        requeue = self.cfg.automation.dry_run
        requests = await AutomationRepo(self.db).sweep_stale_processing(cutoff, requeue=requeue)
        missions = await MissionsRepo(self.db).sweep_stale_processing(cutoff, requeue=requeue)
        if requests or missions:
            n = requests + missions
            if requeue:
                log.info("Stale-sweep re-queued %d dry-run request(s) stuck in processing", n)
            else:
                log.warning(
                    "Stale-sweep released %d request(s) stuck in processing (>%dm)",
                    n, STALE_PROCESSING_MINUTES,
                )
                await self.notify_admin(
                    f"🧹 Released {n} request(s) stuck in processing for over "
                    f"{STALE_PROCESSING_MINUTES} min — please verify nothing "
                    "half-ran on MissionChief."
                )

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
