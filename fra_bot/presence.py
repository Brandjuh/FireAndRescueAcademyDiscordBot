"""Bot presence that reflects what the bot is currently doing.

A background loop reconciles the Discord presence with the set of
running jobs every few seconds. Jobs only mutate an in-memory set (no
awaits, no Discord calls), so the presence is fully decoupled from job
frequency and naturally throttled well under Discord's gateway
presence-update rate limit (~5 per 20 s).

Displayed status:
* a job is running   → "🔄 syncing members…"
* several running    → "🔄 running 3 tasks…"
* circuit breaker    → "⚠️ paused (MissionChief cooldown)" (dnd)
* idle               → "👀 47 members · 3 applications"
"""

from __future__ import annotations

import asyncio
import logging

import discord

from .db.repos import ApplicationsRepo, AutomationRepo, MembersRepo, StateRepo, TreasuryRepo

log = logging.getLogger(__name__)

_TICK_SECONDS = 10.0

# Internal job name → friendly present-tense action.
_JOB_LABELS = {
    "members": "syncing members",
    "applications": "checking applications",
    "logs": "reading alliance logs",
    "treasury": "reading the treasury",
    "expenses": "syncing expenses",
    "expenses-backfill": "backfilling expenses",
    "pre-reset": "capturing daily standings",
    "board-trainings": "processing training requests",
    "board-buildings": "processing building requests",
    "board-events": "processing event requests",
}


class PresenceManager:
    def __init__(self, bot) -> None:
        self._bot = bot
        self._running: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._current_text: str | None = None

        self._members = MembersRepo(bot.db)
        self._apps = ApplicationsRepo(bot.db)
        self._treasury = TreasuryRepo(bot.db)
        self._automation = AutomationRepo(bot.db)
        self._state = StateRepo(bot.db)

    # -- called (sync, cheap) from the job wrapper ----------------------

    def mark_running(self, name: str) -> None:
        self._running.add(name)

    def mark_done(self, name: str) -> None:
        self._running.discard(name)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="presence")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        await self._bot.wait_until_ready()
        while not self._stopping.is_set():
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Presence update failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _reconcile(self) -> None:
        text, status = await self._desired()
        if text == self._current_text:
            return  # nothing changed; don't spend a presence update
        activity = discord.CustomActivity(name=text[:128])
        await self._bot.change_presence(activity=activity, status=status)
        self._current_text = text

    async def _desired(self) -> tuple[str, discord.Status]:
        if self._bot.pacer.circuit_open:
            return "⚠️ paused (MissionChief cooldown)", discord.Status.dnd

        running = self._running
        if running:
            if len(running) == 1:
                label = _JOB_LABELS.get(next(iter(running)), "working")
            else:
                label = f"running {len(running)} tasks"
            return f"🔄 {label}…", discord.Status.online

        return await self._idle_summary(), discord.Status.online

    async def _idle_summary(self) -> str:
        try:
            members = await self._members.active_count()
            open_apps = await self._apps.open_count()
        except Exception:
            return "👀 watching MissionChief"

        parts = [f"{members} members"]
        if open_apps:
            parts.append(f"{open_apps} application{'s' if open_apps != 1 else ''}")

        # Surface expenses backfill progress while it's still running.
        try:
            from .services.treasury_sync import STATE_BACKFILL_DONE, STATE_BACKFILL_NEXT_PAGE

            if await self._state.get(STATE_BACKFILL_DONE) != "1":
                next_page = await self._state.get(STATE_BACKFILL_NEXT_PAGE)
                if next_page:
                    parts.append(f"backfill p{next_page}")
        except Exception:
            pass

        try:
            open_requests = await self._automation.open_count()
            if open_requests:
                parts.append(f"{open_requests} queued")
        except Exception:
            pass

        return "👀 " + " · ".join(parts)
