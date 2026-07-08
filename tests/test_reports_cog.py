"""Tests for the income-report partial-data warning."""

import datetime as dt
from zoneinfo import ZoneInfo

from fra_bot.cogs.reports import _partial_note

NY = ZoneInfo("America/New_York")


def _reset_july8():
    # Midnight NY on 2026-07-08 = the reset after the July 7 game day.
    return dt.datetime(2026, 7, 8, 0, 0, tzinfo=NY)


def test_no_note_for_a_fresh_pre_reset_capture():
    # 23:55 NY July 7 = 03:55 UTC July 8 — five minutes before the reset.
    taken = dt.datetime(2026, 7, 8, 3, 55, tzinfo=dt.timezone.utc)
    assert _partial_note(_reset_july8(), taken.isoformat(), NY) is None


def test_note_for_a_stale_capture():
    # 16:00 NY July 7 — hours before the reset (bot was offline after).
    taken = dt.datetime(2026, 7, 7, 20, 0, tzinfo=dt.timezone.utc)
    note = _partial_note(_reset_july8(), taken.isoformat(), NY)
    assert note is not None
    assert "incomplete" in note
    assert "16:00" in note


def test_no_note_without_a_timestamp():
    assert _partial_note(_reset_july8(), None, NY) is None
