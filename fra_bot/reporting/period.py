"""Report time periods, bounded by the MissionChief game day.

MissionChief resets its daily and monthly income lists at midnight UTC
(observed: the lists collapse between 00:00 and 00:30 UTC every night, all
summer — i.e. 20:00 New York, not New York midnight as long assumed), so
report windows are computed on that clock. Every stored timestamp is UTC ISO-8601, so the returned bounds
are directly comparable with them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

UTC = dt.timezone.utc
#: The game's reset clock: game day / month edges are midnights on it.
GAME_TZ = UTC

# name -> human label
PERIODS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "week": "Last 7 days",
    "month": "This month",
    "prev-month": "Last month",
    "year": "This year",
    "prev-year": "Last year",
    "all": "All time",
}


@dataclass(frozen=True)
class Period:
    name: str
    label: str
    start: dt.datetime  # aware UTC (or None for 'all')
    end: dt.datetime    # aware UTC

    @property
    def start_iso(self) -> str | None:
        return None if self.start is None else self.start.isoformat(timespec="seconds")

    @property
    def end_iso(self) -> str:
        return self.end.isoformat(timespec="seconds")


def resolve_period(name: str, *, now: dt.datetime | None = None) -> Period:
    """Compute UTC bounds for a named period using game-day (UTC) edges."""
    name = (name or "today").lower()
    if name not in PERIODS:
        raise ValueError(f"Unknown period '{name}'. Options: {', '.join(PERIODS)}")

    now = now or dt.datetime.now(UTC)
    game_now = now.astimezone(GAME_TZ)
    day_start = game_now.replace(hour=0, minute=0, second=0, microsecond=0)

    if name == "today":
        start, end = day_start, now
    elif name == "yesterday":
        start = day_start - dt.timedelta(days=1)
        end = day_start
    elif name == "week":
        start, end = now - dt.timedelta(days=7), now
    elif name == "month":
        start = day_start.replace(day=1)
        end = now
    elif name == "prev-month":
        first_this = day_start.replace(day=1)
        end = first_this
        start = (first_this - dt.timedelta(days=1)).replace(day=1)
    elif name == "year":
        start = day_start.replace(month=1, day=1)
        end = now
    elif name == "prev-year":
        first_this_year = day_start.replace(month=1, day=1)
        end = first_this_year
        start = first_this_year.replace(year=first_this_year.year - 1)
    else:  # all
        return Period(name, PERIODS[name], None, now)

    return Period(
        name,
        PERIODS[name],
        start.astimezone(UTC),
        end.astimezone(UTC),
    )
