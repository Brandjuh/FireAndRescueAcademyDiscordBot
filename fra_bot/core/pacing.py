"""Human-like request pacing towards MissionChief.

Every outgoing request to missionchief.com goes through one shared
:class:`HumanPacer`. It enforces:

* a randomized delay between consecutive requests (uniform jitter),
* a hard cap on requests per minute (sliding window),
* a circuit breaker that pauses all traffic after repeated failures.

The goal is that our traffic looks like a person browsing, never like a
crawler hammering the site.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import time
from collections import deque
from contextlib import contextmanager

log = logging.getLogger(__name__)

# Task-local flag marking the current work as LOW-priority bulk traffic.
# Set via bulk_traffic(); read by HumanPacer.wait_turn, so no call site
# between the job and the client needs to thread a priority argument.
_BULK_TRAFFIC = contextvars.ContextVar("fra_bulk_traffic", default=False)


@contextmanager
def bulk_traffic():
    """Mark every MissionChief request made inside as bulk (low priority).

    Wrap the body of history backfills and full sweeps::

        with bulk_traffic():
            await self.client.fetch_page(...)   # yields to board work
    """
    token = _BULK_TRAFFIC.set(True)
    try:
        yield
    finally:
        _BULK_TRAFFIC.reset(token)


class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open and traffic is paused."""

    def __init__(self, retry_at: float):
        self.retry_at = retry_at
        super().__init__(
            "MissionChief circuit breaker open until "
            f"{time.strftime('%H:%M:%S', time.localtime(retry_at))}"
        )


class HumanPacer:
    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        max_per_minute: int,
        failure_threshold: int = 5,
        cooldown_seconds: float = 900.0,
        failure_window_seconds: float = 600.0,
    ) -> None:
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._max_per_minute = max(1, max_per_minute)
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._failure_window = failure_window_seconds

        self._lock = asyncio.Lock()
        self._recent: deque[float] = deque()
        self._next_allowed = 0.0
        self._failures: deque[float] = deque()
        self._circuit_open_until = 0.0
        self._waiting = 0
        self._bulk_waiting = 0
        self._priority_waiters = 0
        # Set whenever NO priority (interactive) request is waiting — bulk
        # traffic parks on this event so board work always goes first.
        self._no_priority = asyncio.Event()
        self._no_priority.set()

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    @property
    def backlog(self) -> int:
        """How many requests are currently queued for their turn. A number
        that keeps growing means demand exceeds the configured pacing
        (min_delay/max_delay) — the bot is choking on its own politeness."""
        return self._waiting

    @property
    def backlog_bulk(self) -> int:
        """How many of the queued requests are LOW-priority bulk traffic
        (backfills, full member sweeps)."""
        return self._bulk_waiting

    def reconfigure(
        self,
        *,
        min_delay: float | None = None,
        max_delay: float | None = None,
        max_per_minute: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Apply new pacing LIVE (used by the runtime settings commands)."""
        if min_delay is not None:
            self._min_delay = float(min_delay)
        if max_delay is not None:
            self._max_delay = float(max_delay)
        if max_per_minute is not None:
            self._max_per_minute = max(1, int(max_per_minute))
        if cooldown_seconds is not None:
            self._cooldown_seconds = float(cooldown_seconds)
        # Don't keep serving a delay armed under the OLD pacing: cap the next
        # slot to the new maximum so e.g. lowering max_delay from 60 to 9
        # takes effect on the very next request.
        self._next_allowed = min(
            self._next_allowed, time.monotonic() + self._max_delay
        )

    async def wait_turn(self) -> None:
        """Block until it's polite to send the next request.

        Two priority classes share the pacing budget: interactive work
        (board polls, guides, request execution, admin commands) and BULK
        work (history backfills, full member sweeps — anything running
        inside :func:`bulk_traffic`). Bulk holds back whenever interactive
        requests are waiting, so a member's request never sits behind a
        stack of backfill pages."""
        bulk = _BULK_TRAFFIC.get()
        self._waiting += 1
        if bulk:
            self._bulk_waiting += 1
        try:
            if bulk:
                while self._priority_waiters:
                    await self._no_priority.wait()
                await self._wait_turn_inner()
            else:
                self._priority_waiters += 1
                self._no_priority.clear()
                try:
                    await self._wait_turn_inner()
                finally:
                    self._priority_waiters -= 1
                    if self._priority_waiters == 0:
                        self._no_priority.set()
        finally:
            self._waiting -= 1
            if bulk:
                self._bulk_waiting -= 1

    async def _wait_turn_inner(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._circuit_open_until:
                raise CircuitOpenError(time.time() + (self._circuit_open_until - now))

            # Sliding one-minute window cap.
            while self._recent and self._recent[0] < now - 60.0:
                self._recent.popleft()
            wait_for_window = 0.0
            if len(self._recent) >= self._max_per_minute:
                wait_for_window = self._recent[0] + 60.0 - now

            wait_for_jitter = max(0.0, self._next_allowed - now)
            delay = max(wait_for_window, wait_for_jitter)
            if delay > 0:
                await asyncio.sleep(delay)

            sent_at = time.monotonic()
            self._recent.append(sent_at)
            self._next_allowed = sent_at + random.uniform(self._min_delay, self._max_delay)

    def record_success(self) -> None:
        # Windowed breaker: a single success no longer wipes the failure
        # history, so intermittent failures (fail, ok, fail, ok, …) can
        # still trip it. Old failures simply age out of the window.
        pass

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and self._failures[0] < now - self._failure_window:
            self._failures.popleft()
        if len(self._failures) >= self._failure_threshold:
            self._circuit_open_until = now + self._cooldown_seconds
            self._failures.clear()
            log.warning(
                "Circuit breaker opened: pausing MissionChief traffic for %.0f minutes",
                self._cooldown_seconds / 60,
            )
