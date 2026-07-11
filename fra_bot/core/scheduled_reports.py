"""Runtime-managed scheduled reports.

``reports.scheduled`` in config.yaml is the documented default. The
``!fra reports`` commands store an OVERRIDE list in the state table that
REPLACES the YAML list — applied live on every change and re-applied on
startup (right after the ``!fra set`` overrides). ``!fra reports reset``
drops the override and the YAML entries stand again.

The reporting cog reads ``cfg.reports.scheduled`` on every daily tick, so
an in-place apply is all a change needs — no restart, no rescheduling.
"""

from __future__ import annotations

import json

from ..config import ScheduledReport

#: State key holding the override: a JSON list of entry dicts.
STATE_KEY = "scheduled_reports_override"

VALID_CADENCES = ("daily", "weekly", "monthly", "yearly")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")


def entry_to_dict(entry: ScheduledReport) -> dict:
    return {
        "report": entry.report,
        "period": entry.period,
        "cadence": entry.cadence,
        "channel_id": entry.channel_id,
        "weekday": entry.weekday,
        "day": entry.day,
        "month": entry.month,
    }


def dict_to_entry(data: dict) -> ScheduledReport:
    return ScheduledReport(
        report=str(data["report"]),
        period=str(data.get("period", "today")),
        cadence=str(data.get("cadence", "daily")).lower(),
        channel_id=int(data.get("channel_id", 0)),
        weekday=int(data.get("weekday", 0)),
        day=int(data.get("day", 1)),
        month=int(data.get("month", 1)),
    )


def apply(cfg, entries: tuple[ScheduledReport, ...]) -> None:
    """Swap the live schedule in place (frozen dataclass, same sanctioned
    mutation pattern as the runtime settings)."""
    object.__setattr__(cfg.reports, "scheduled", tuple(entries))


async def store_entries(state, entries: tuple[ScheduledReport, ...]) -> None:
    await state.set(STATE_KEY, json.dumps([entry_to_dict(e) for e in entries]))


async def clear_override(state) -> bool:
    existed = await state.get(STATE_KEY) is not None
    await state.delete(STATE_KEY)
    return existed


async def load_override(state) -> tuple[ScheduledReport, ...] | None:
    """The stored override, or None when the YAML list is in charge."""
    raw = await state.get(STATE_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return tuple(dict_to_entry(item) for item in data)
    except (ValueError, TypeError, KeyError):
        return None  # unreadable override: fall back to YAML, don't crash


async def apply_stored(cfg, state) -> bool:
    """Re-apply the stored override on startup. Returns True when one was
    applied (the caller logs it)."""
    entries = await load_override(state)
    if entries is None:
        return False
    apply(cfg, entries)
    return True


def describe(entry: ScheduledReport, index: int) -> str:
    """One human-readable list line for ``!fra reports``."""
    when = {
        "daily": "daily",
        "weekly": f"weekly ({WEEKDAYS[entry.weekday % 7]})",
        "monthly": f"monthly (day {entry.day})",
        "yearly": f"yearly ({entry.day:02d}-{entry.month:02d})",
    }.get(entry.cadence, entry.cadence)
    return (
        f"`{index}.` **{entry.report}** · {entry.period} · {when} → "
        f"<#{entry.channel_id}>"
    )
