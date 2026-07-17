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
        from .services.chat_sync import ChatSyncService

        self.chat_sync = ChatSyncService(self.mc, self.db)
        self.logs_sync = LogsSyncService(cfg, self.mc, self.db)
        self.treasury_sync = TreasurySyncService(cfg, self.mc, self.db)

        # Phase 2 board automation.
        self.trainings = TrainingsService(cfg, self.mc, self.db)
        self.buildings = BuildingsService(cfg, self.mc, self.db, self.geocoder)
        # On-command alliance hospital/prison level + extension upgrades, also
        # used to finish a freshly-built academy by buying its extensions.
        self.building_upgrade = BuildingUpgradeService(cfg, self.mc, self.db)
        # Academy build panel: fixed-address academy builds, reusing the
        # building service's browser builder + live-funds read + geocoder, and
        # the upgrade service to buy the new academy's extensions.
        from .services.academy import AcademyService

        self.academy = AcademyService(
            cfg, self.db, self.buildings, upgrader=self.building_upgrade
        )
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
        # Member tax (5% donation) warnings — the old bot's system, ported.
        from .services.tax_warnings import TaxWarningService

        self.tax_warnings = TaxWarningService(cfg, self.mc, self.db)
        # The missions-database forum: every einsaetze.json mission as a
        # tagged forum post, synced daily.
        from .services.missions_forum import MissionsForumService

        self.missions_forum = MissionsForumService(cfg, self.mc, self.db, self)
        # The vehicles-database forum: every LSSM vehicle as a tagged forum
        # post, synced daily (fetched from GitHub, so no MissionChief traffic).
        from .services.vehicles_forum import VehiclesForumService

        self.vehicles_forum = VehiclesForumService(cfg, self.db, self)
        # In-game DM mirror: every PM conversation ↔ one forum thread,
        # with staff replies routed back into the game.
        from .services.dm_mirror import DmMirrorService

        self.dm_mirror = DmMirrorService(cfg, self.mc, self.db, self)
        # Sent tax warnings mirror into the DM forum at send time (like the
        # reference bot): outgoing-only conversations live in the game's
        # SENT box and may never show on the inbox page the scan reads.
        self.tax_warnings.mirror = self.dm_mirror.mirror_now
        # Credit rank roles (the old bot's RoleBasedCredits, ported).
        from .services.rank_roles import RankRolesService

        self.rank_roles = RankRolesService(cfg, self.db, self)

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

        # Scheduled reports: keep the YAML list as the reset point, then
        # apply the operator's `!fra reports` override on top.
        from .core import scheduled_reports as sched_reports

        self.yaml_scheduled_reports = self.cfg.reports.scheduled
        if await sched_reports.apply_stored(self.cfg, StateRepo(self.db)):
            log.info(
                "settings: reports.scheduled overridden (%d entries via "
                "!fra reports)", len(self.cfg.reports.scheduled),
            )

        await self.mc.start()
        await self.geocoder.start()

        from .cogs.academy import AcademyCog, AcademyPanelView
        from .cogs.admin import AdminCog
        from .cogs.automation import AutomationCog
        from .cogs.chat_bridge import ChatBridgeCog
        from .cogs.sanctions import SanctionsCog
        from .cogs.faq import FaqCog
        from .cogs.profile import ProfileCog
        from .cogs.timeline import TimelineCog
        from .cogs.dossier import DossierCog, DossierPanelView
        from .cogs.eventpinger import EventPingerCog
        from .cogs.membersync import MemberSyncCog
        from .cogs.missions import MissionPanelView, MissionsCog
        from .cogs.notifications import NotificationsCog
        from .cogs.classes_panel import ClassesPanelCog
        from .cogs.dm_mirror import DmMirrorCog
        from .cogs.panels import PanelKeeperCog
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
        await self.add_cog(DossierCog(self))
        await self.add_cog(EventPingerCog(self))
        await self.add_cog(PanelKeeperCog(self))
        await self.add_cog(DmMirrorCog(self))
        await self.add_cog(ClassesPanelCog(self))
        await self.add_cog(AcademyCog(self))
        await self.add_cog(ChatBridgeCog(self))
        await self.add_cog(SanctionsCog(self))
        await self.add_cog(TimelineCog(self))
        await self.add_cog(FaqCog(self))
        await self.add_cog(ProfileCog(self))

        # Persistent panels survive restarts; register their views.
        missions_cog = self.get_cog("MissionsCog")
        if missions_cog is not None:
            self.add_view(MissionPanelView(missions_cog))
        dossier_cog = self.get_cog("DossierCog")
        if dossier_cog is not None:
            self.add_view(DossierPanelView(dossier_cog))
        requests_cog = self.get_cog("RequestsCog")
        if requests_cog is not None:
            self.add_view(RequestPanelView(requests_cog))
        academy_cog = self.get_cog("AcademyCog")
        if academy_cog is not None:
            self.add_view(AcademyPanelView(academy_cog))
        dm_cog = self.get_cog("DmMirrorCog")
        if dm_cog is not None:
            from .cogs.dm_mirror import DmPanelView

            self.add_view(DmPanelView(dm_cog))

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
            self._guarded(self.buildings.finish_pending, "building-finisher"),
            minutes=30,
            name="building-finisher",
            initial_delay_seconds=420.0,
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

        # Phase 2 board automation pollers. ALWAYS registered; each poll
        # checks its own switch LIVE (services' poll_enabled) and returns
        # instantly when off. Gating registration on the switch instead
        # made `!fra set training.enabled on` a trap: the flag flipped in
        # memory (intake accepted requests, the one-shot kick ran) but the
        # retry poll didn't exist until restart, stranding any request
        # whose first attempt hit a transient 'busy'.
        automation = self.cfg.automation
        sched.add_interval_job(
            self._guarded(self.trainings.poll, "board-trainings"),
            minutes=automation.training.interval,
            name="board-trainings",
            initial_delay_seconds=150.0,
        )
        sched.add_interval_job(
            self._guarded(self.buildings.poll, "board-buildings"),
            minutes=automation.building.interval,
            name="board-buildings",
            initial_delay_seconds=210.0,
        )
        # Academy panel builds queued because funds were low: drain + retry.
        # The buttons themselves work regardless of this switch (they build on
        # click); this only auto-resumes builds that had to wait for funds.
        # Run the drain whenever the panel is live (channel set), even if the
        # `enabled` flag is off, otherwise a low-funds click is orphaned: the
        # panel promises auto-retry but nothing would ever drain the queue.
        # (Draining only ever retries builds a member/admin/autoscale
        # explicitly queued — it starts nothing on its own.) Always
        # registered, switches read live: autoscale/enabled can be flipped
        # on at runtime, and a queued build must never sit undrained just
        # because every switch was off at startup.
        async def _drain_academy_queue_if_on() -> None:
            enabled = self.cfg.automation.academy.enabled
            autoscale = self.cfg.automation.academy.autoscale
            panel = int(getattr(self.cfg.discord.channels, "academy_panel", 0) or 0)
            if not (enabled or panel or autoscale):
                return
            await self.academy.process_queue()

        sched.add_interval_job(
            self._guarded(_drain_academy_queue_if_on, "academy-builds"),
            minutes=automation.academy.interval,
            name="academy-builds",
            initial_delay_seconds=200.0,
        )

        # The jobs below spend alliance funds AUTONOMOUSLY (no member click
        # behind them), so each requires its own explicit switch — a live
        # panel channel must not be enough. Always registered, switch read
        # live each pass, so `!fra set` applies without a restart.
        async def _sweep_extensions_if_enabled() -> None:
            # Academy extensions unlock one at a time (~7 days each); a slow
            # sweep buys the next available one on each of our academies so
            # they max out over the following weeks without hammering.
            if not self.cfg.automation.academy.enabled:
                # The sweep used to ride on the panel channel alone; after
                # the switch became required it must not stop SILENTLY on a
                # deployment that relied on that — say so, every pass (4x/day).
                if int(getattr(self.cfg.discord.channels, "academy_panel", 0) or 0):
                    log.info(
                        "academy extension sweep is OFF (automation.academy."
                        "enabled=false) — run `!fra set academy.enabled on` "
                        "to resume buying extensions"
                    )
                return
            await self.academy.sweep_extensions()

        sched.add_interval_job(
            self._guarded(_sweep_extensions_if_enabled, "academy-extensions"),
            minutes=360,
            name="academy-extensions",
            initial_delay_seconds=900.0,
        )

        # Auto-scale: build a new academy when a discipline runs out of free
        # classrooms (own switch, off by default — it spends alliance funds).
        # Hourly, matching the training availability refresh, with a debounce
        # + 24h cooldown so one transient reading can't spawn a fleet.
        async def _autoscale_if_enabled() -> None:
            if not self.cfg.automation.academy.autoscale:
                return
            await self.academy.autoscale()

        sched.add_interval_job(
            self._guarded(_autoscale_if_enabled, "academy-autoscale"),
            minutes=60,
            name="academy-autoscale",
            initial_delay_seconds=1200.0,
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
        # the Discord queue and the rotation. Always registered; the poll
        # reads the three switches live and no-ops when all are off.
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
        # Daily missions-forum sync: post new einsaetze.json missions,
        # refresh changed ones. Discord-only writes; the JSON fetch is a
        # paced read, so this is safe regardless of dry_run.
        if automation.missions_forum.enabled:
            hour, minute = _parse_hhmm(
                automation.missions_forum.sync_time, default=(4, 0)
            )
            sched.add_daily_job(
                self._guarded(self._missions_forum_pass, "missions-forum"),
                at=dt.time(hour, minute),
                timezone=self.cfg.reports.timezone,
                name="missions-forum",
            )
            # Hourly catch-up while the initial backfill is incomplete: an
            # interrupted run (restart, crash) otherwise stalls the forum
            # until the next day's sync. A DB-only no-op once complete.
            sched.add_interval_job(
                self._guarded(self._missions_forum_catchup, "missions-forum"),
                minutes=60,
                name="missions-forum-catchup",
                initial_delay_seconds=480.0,
            )
        # The vehicles-database forum: same shape as the missions forum, but
        # the catalog is fetched from GitHub, so it never touches the game.
        if automation.vehicles_forum.enabled:
            hour, minute = _parse_hhmm(
                automation.vehicles_forum.sync_time, default=(4, 30)
            )
            sched.add_daily_job(
                self._guarded(self._vehicles_forum_pass, "vehicles-forum"),
                at=dt.time(hour, minute),
                timezone=self.cfg.reports.timezone,
                name="vehicles-forum",
            )
            sched.add_interval_job(
                self._guarded(self._vehicles_forum_catchup, "vehicles-forum"),
                minutes=60,
                name="vehicles-forum-catchup",
                initial_delay_seconds=540.0,
            )
        # Credit rank roles: hourly sync of Discord rank roles against
        # MissionChief earned credits (Discord-only writes; role changes
        # mirror the roster, like the verified role).
        if automation.rank_roles.enabled:
            sched.add_interval_job(
                self._guarded(self._rank_roles_pass, "rank-roles"),
                minutes=automation.rank_roles.interval,
                name="rank-roles",
                initial_delay_seconds=540.0,
            )
        # In-game DM mirror: scan the PM inbox and mirror conversations to
        # the forum. Read-only on the game side, so it runs in dry-run too
        # (thread replies into the game DO honour dry_run).
        if automation.dm_mirror.enabled:
            sched.add_interval_job(
                self._guarded(self._dm_mirror_pass, "dm-mirror"),
                minutes=automation.dm_mirror.interval,
                name="dm-mirror",
                initial_delay_seconds=240.0,
            )
        # Member tax (5% donation) warnings: escalating in-game PMs, reset
        # the moment a member fixes their donation. Every action lands in
        # the admin channel.
        if automation.tax_warnings.enabled:
            sched.add_interval_job(
                self._guarded(self._tax_warning_pass, "tax-warnings"),
                minutes=max(1, automation.tax_warnings.interval_hours) * 60,
                name="tax-warnings",
                initial_delay_seconds=900.0,
            )
        # Class-availability panel: hourly free-classroom counts. The walk
        # reuses the trainings guide's cache when that already ran this
        # hour, so the two consumers never double the game traffic.
        if int(getattr(self.cfg.discord.channels, "class_panel", 0) or 0):
            sched.add_interval_job(
                self._guarded(self._class_availability_pass, "class-availability"),
                minutes=60,
                name="class-availability",
                initial_delay_seconds=180.0,
            )
        # Saved-missions list for the Discord mission chooser: one form
        # fetch per pass (also refreshed opportunistically on every large
        # mission start).
        sched.add_interval_job(
            self._guarded(self.missions_service.refresh_saved_missions,
                          "saved-missions"),
            minutes=360,
            name="saved-missions",
            initial_delay_seconds=600.0,
        )
        log.info(
            "Background jobs scheduled (automation: dry_run=%s, training=%s, "
            "building=%s, events=%s, mission=%s)",
            automation.dry_run, automation.training.enabled,
            automation.building.enabled, automation.events.enabled,
            automation.mission.enabled,
        )

    async def _missions_forum_pass(self) -> None:
        """One missions-forum sync, with anything noteworthy (new posts,
        edits, new tags, failures) mirrored to the admin channel. The
        watchdog timeout guarantees a hung run can never hold the job
        lock forever (which would silently stop all future syncs)."""
        try:
            summary = await asyncio.wait_for(
                self.missions_forum.sync(), timeout=45 * 60
            )
        except asyncio.TimeoutError:
            log.error("Missions-forum sync timed out after 45 min")
            await self.notify_admin(
                "📚 **Missions forum**\n⏱️ sync timed out after 45 min — "
                "aborted; the next run continues where it left off"
            )
            return
        if summary.get("error") or summary.get("changed"):
            await self.notify_admin(
                "📚 **Missions forum**\n" + "\n".join(summary["lines"])[:1800]
            )

    async def _missions_forum_catchup(self) -> None:
        """Continue the missions-forum backfill between daily syncs. Free
        (one DB read, no MissionChief traffic) once the backfill is done."""
        from .db.repos import StateRepo
        from .services.missions_forum import STATE_BACKFILL_DONE

        if await StateRepo(self.db).get(STATE_BACKFILL_DONE) is not None:
            return
        await self._missions_forum_pass()

    async def _vehicles_forum_pass(self) -> None:
        """One vehicles-forum sync, with anything noteworthy mirrored to the
        admin channel. The watchdog guarantees a hung run can never hold the
        job lock forever."""
        try:
            summary = await asyncio.wait_for(
                self.vehicles_forum.sync(), timeout=45 * 60
            )
        except asyncio.TimeoutError:
            log.error("Vehicles-forum sync timed out after 45 min")
            await self.notify_admin(
                "🚒 **Vehicles forum**\n⏱️ sync timed out after 45 min — "
                "aborted; the next run continues where it left off"
            )
            return
        if summary.get("error") or summary.get("changed"):
            await self.notify_admin(
                "🚒 **Vehicles forum**\n" + "\n".join(summary["lines"])[:1800]
            )

    async def _vehicles_forum_catchup(self) -> None:
        """Continue the vehicles-forum backfill between daily syncs. Free (one
        DB read, no traffic) once the backfill is done."""
        from .db.repos import StateRepo
        from .services.vehicles_forum import STATE_BACKFILL_DONE

        if await StateRepo(self.db).get(STATE_BACKFILL_DONE) is not None:
            return
        await self._vehicles_forum_pass()

    async def _rank_roles_pass(self) -> None:
        """One rank-role sync; noteworthy outcomes go to the admin channel."""
        summary = await self.rank_roles.sync()
        if summary.get("error") or summary.get("changed"):
            await self.notify_admin(
                "🎖️ **Rank roles**\n" + "\n".join(summary["lines"])[:1800]
            )

    async def _class_availability_pass(self) -> None:
        """Hourly: refresh the free-classroom cache (reusing a fresh guide
        walk when there is one) and re-render the class-availability panel."""
        await self.trainings.refresh_availability()
        cog = self.get_cog("ClassesPanelCog")
        if cog is not None:
            await cog.reload_snapshot()
        keeper = self.get_cog("PanelKeeperCog")
        if keeper is not None:
            await keeper.ensure("classes")

    async def _dm_mirror_pass(self) -> None:
        """One DM inbox scan; only errors are surfaced to the admin channel
        (new mirrored messages are visible in the forum itself)."""
        summary = await self.dm_mirror.scan()
        if summary.get("error") or summary.get("failed"):
            await self.notify_admin(
                "📬 **DM mirror**\n" + "\n".join(summary["lines"])[:1800]
            )

    async def _tax_warning_pass(self) -> None:
        """One warning scan, with every action mirrored to the admin
        channel so warnings/resets/kick flags are visible in Discord."""
        lines = await self.tax_warnings.scan()
        if lines:
            await self.notify_admin(
                "💰 **Tax warnings**\n" + "\n".join(lines)[:1800]
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

    async def log_member_action(
        self, *, action: str, detail: str | None = None,
        discord_user_id: int | None = None, mc_user_id: int | None = None,
        actor_name: str | None = None,
    ) -> None:
        """Record a member's bot-side action in the central member-action
        log (per-member history + the admin feed). MUST never break the
        action it accompanies — failures are logged and swallowed."""
        from .db.repos import MemberActionsRepo

        try:
            await MemberActionsRepo(self.db).log(
                discord_user_id=discord_user_id, mc_user_id=mc_user_id,
                actor_name=actor_name, action=action, detail=detail,
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not break actions
            log.exception("member action log failed (%s)", action)

    async def close(self) -> None:
        log.info("Shutting down…")
        await self.presence.stop()
        await self.scheduler.stop()
        await self.geocoder.close()
        await self.mc.close()
        await self.db.close()
        await super().close()
