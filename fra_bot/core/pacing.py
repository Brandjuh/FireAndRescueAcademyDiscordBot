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
import logging
import random
import time
from collections import deque

log = logging.getLogger(__name__)


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
    ) -> None:
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._max_per_minute = max(1, max_per_minute)
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds

        self._lock = asyncio.Lock()
        self._recent: deque[float] = deque()
        self._next_allowed = 0.0
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    async def wait_turn(self) -> None:
        """Block until it's polite to send the next request."""
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
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._circuit_open_until = time.monotonic() + self._cooldown_seconds
            self._consecutive_failures = 0
            log.warning(
                "Circuit breaker opened: pausing MissionChief traffic for %.0f minutes",
                self._cooldown_seconds / 60,
            )
