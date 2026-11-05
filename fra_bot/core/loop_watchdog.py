"""Log what is holding the event loop when it stalls.

Discord drops a button click the bot can't acknowledge within 3 s, so a stall
of a few seconds is enough to break buttons, yet stays below discord.py's own
10 s "heartbeat blocked" warning. A thread is the only thing that can still
look while the loop is stuck, so it logs the loop thread's stack at that moment.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)


class LoopStallWatchdog:
    def __init__(self, *, threshold: float = 2.0, interval: float = 0.5) -> None:
        self._threshold = threshold
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_beat = 0.0
        self._stall_reported = False

    def start(self) -> None:
        """Call from the running event loop."""
        if self._thread is not None:
            return
        loop = asyncio.get_running_loop()
        loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch, args=(loop, loop_thread_id),
            name="loop-stall-watchdog", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _beat(self) -> None:
        now = time.monotonic()
        if self._stall_reported:
            self._stall_reported = False
            log.warning("event loop stall ended after %.1fs", now - self._last_beat)
        self._last_beat = now

    def _watch(self, loop: asyncio.AbstractEventLoop, loop_thread_id: int) -> None:
        while not self._stop.wait(self._interval):
            try:
                loop.call_soon_threadsafe(self._beat)
            except RuntimeError:  # loop closed
                return
            stalled_for = time.monotonic() - self._last_beat
            if stalled_for >= self._threshold and not self._stall_reported:
                self._stall_reported = True
                frame = sys._current_frames().get(loop_thread_id)
                stack = "".join(traceback.format_stack(frame)) if frame else "(unavailable)"
                log.warning(
                    "event loop stalled for %.1fs; loop thread is at:\n%s",
                    stalled_for, stack,
                )
