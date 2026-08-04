"""Is missionchief.com actually reachable right now?

Every request the client makes reports here, so this is the one place
that knows whether the GAME is down as opposed to the bot being idle,
rate-limited or refused.

The distinction matters, because "MissionChief is down" is a claim we
make to members:

* a 5xx answer, a refused connection or a timeout is the site failing —
  those are outage signals;
* a 4xx (including 429 rate limiting) and a redirect to the sign-in page
  are NOT: the server answered, so it is up. Those count as proof of
  reachability, which is exactly what keeps a permissions problem or our
  own throttling from being announced as an outage.

An idle bot must never look like an outage either, hence the rule below:
the outage clock only runs once something has actually failed in a
site-is-down way.
"""

from __future__ import annotations

import time


class MissionChiefHealth:
    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        #: Process start — the floor for a bot that boots INTO an outage
        #: (it has no successful request to measure from).
        self.started_at = clock()
        self.last_reachable_at: float | None = None
        self.last_unreachable_at: float | None = None
        #: Consecutive site-is-down failures; any proof of reachability
        #: clears it.
        self.unreachable_count = 0
        self.last_reason: str | None = None

    # -- fed by the client ------------------------------------------------

    def note_reachable(self) -> None:
        """The server answered — 2xx, but also 4xx/429/sign-in redirect."""
        self.last_reachable_at = self._clock()
        self.unreachable_count = 0
        self.last_reason = None

    def note_unreachable(self, reason: str) -> None:
        """The site failed to answer: 5xx, connection error, timeout."""
        self.last_unreachable_at = self._clock()
        self.unreachable_count += 1
        self.last_reason = reason

    # -- read by the status service ---------------------------------------

    def outage_seconds(self) -> float:
        """How long the site has looked down; 0.0 when it does not.

        Measured from the last proof of reachability (or from process
        start when there has never been one), and only once a real
        down-signal arrived — a quiet bot makes no claims."""
        if self.unreachable_count <= 0:
            return 0.0
        since = self.last_reachable_at or self.started_at
        return max(0.0, self._clock() - since)

    def down_since(self) -> float | None:
        """Epoch seconds the outage started, or None when up."""
        if self.unreachable_count <= 0:
            return None
        return self.last_reachable_at or self.started_at
