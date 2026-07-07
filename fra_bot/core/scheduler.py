"""Tiny asyncio job scheduler with jitter.

We deliberately avoid APScheduler: fewer dependencies on the Pi and full
control over jitter and error isolation. Two job kinds:

* interval jobs: run every N minutes with +/- jitter so runs never land
  on predictable clock ticks,
* daily jobs: run once per day at a given time in a given timezone
  (used for the pre-reset treasury snapshot).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[None]]


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    def add_interval_job(
        self,
        func: JobFunc,
        *,
        minutes: float,
        name: str,
        jitter_fraction: float = 0.15,
        run_immediately: bool = False,
        initial_delay_seconds: float | None = None,
    ) -> None:
        """Run *func* every *minutes*, with +/- jitter_fraction jitter.

        A small random initial delay spreads the first runs of all jobs
        after boot instead of firing everything at once.
        """
        task = asyncio.create_task(
            self._run_interval(
                func,
                minutes * 60.0,
                name,
                jitter_fraction,
                run_immediately,
                initial_delay_seconds,
            ),
            name=f"job:{name}",
        )
        self._tasks.append(task)

    def add_daily_job(
        self,
        func: JobFunc,
        *,
        at: dt.time,
        timezone: str,
        name: str,
    ) -> None:
        """Run *func* once per day at local time *at* in *timezone*."""
        task = asyncio.create_task(
            self._run_daily(func, at, ZoneInfo(timezone), name),
            name=f"job:{name}",
        )
        self._tasks.append(task)

    async def _run_interval(
        self,
        func: JobFunc,
        interval_seconds: float,
        name: str,
        jitter_fraction: float,
        run_immediately: bool,
        initial_delay_seconds: float | None,
    ) -> None:
        if initial_delay_seconds is None:
            initial_delay_seconds = random.uniform(5.0, min(120.0, interval_seconds / 2))
        if not run_immediately:
            if await self._sleep(initial_delay_seconds):
                return
        while not self._stopping.is_set():
            await self._invoke(func, name)
            jitter = interval_seconds * jitter_fraction
            delay = interval_seconds + random.uniform(-jitter, jitter)
            if await self._sleep(max(30.0, delay)):
                return

    async def _run_daily(
        self, func: JobFunc, at: dt.time, tz: ZoneInfo, name: str
    ) -> None:
        while not self._stopping.is_set():
            now = dt.datetime.now(tz)
            target = now.replace(
                hour=at.hour, minute=at.minute, second=at.second, microsecond=0
            )
            if target <= now:
                target += dt.timedelta(days=1)
            wait = (target - now).total_seconds()
            log.debug("Daily job %s sleeping %.0fs until %s", name, wait, target)
            if await self._sleep(wait):
                return
            await self._invoke(func, name)

    async def _invoke(self, func: JobFunc, name: str) -> None:
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except Exception:
            # One failing job must never kill the scheduler loop.
            log.exception("Job %s failed", name)

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, but wake early on stop. Returns True if stopping."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
