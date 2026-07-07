"""Discord bot wiring: services, scheduler and cogs."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands

from .config import Config
from .core.pacing import HumanPacer
from .core.scheduler import Scheduler
from .db.database import Database
from .mc.client import MissionChiefClient
from .services.applications_sync import ApplicationsSyncService
from .services.logs_sync import LogsSyncService
from .services.members_sync import MembersSyncService
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

        self.members_sync = MembersSyncService(cfg, self.mc, self.db)
        self.applications_sync = ApplicationsSyncService(self.mc, self.db)
        self.logs_sync = LogsSyncService(self.mc, self.db)
        self.treasury_sync = TreasurySyncService(cfg, self.mc, self.db)

        self._jobs_started = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.mc.start()

        from .cogs.admin import AdminCog
        from .cogs.notifications import NotificationsCog
        from .cogs.reports import ReportsCog

        await self.add_cog(AdminCog(self))
        await self.add_cog(NotificationsCog(self))
        await self.add_cog(ReportsCog(self))

    async def on_ready(self) -> None:
        log.info("Logged in to Discord as %s (%s)", self.user, self.user.id)
        if not self._jobs_started:
            self._jobs_started = True
            self._start_jobs()

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
        log.info("Background jobs scheduled")

    def _guarded(self, func, name: str):
        """Wrap a sync job so scheduler jobs log-and-continue on errors,
        and pause quietly while the circuit breaker is open."""

        async def runner() -> None:
            from .core.pacing import CircuitOpenError
            from .mc.errors import MissionChiefError

            try:
                await func()
            except CircuitOpenError as exc:
                log.warning("Job %s skipped: %s", name, exc)
            except MissionChiefError as exc:
                log.error("Job %s failed: %s", name, exc)
                await self.notify_admin(f"⚠️ Sync job **{name}** failed: {exc}")

        return runner

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
        await self.scheduler.stop()
        await self.mc.close()
        await self.db.close()
        await super().close()
