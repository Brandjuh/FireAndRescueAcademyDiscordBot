"""Tests for the income-report partial-data warning."""

import datetime as dt
from fra_bot.cogs.reports import _partial_note
from fra_bot.reporting.period import GAME_TZ


def _reset_july8():
    # Midnight UTC on 2026-07-08 = the reset after the July 7 game day.
    return dt.datetime(2026, 7, 8, 0, 0, tzinfo=GAME_TZ)


def test_no_note_for_a_fresh_pre_reset_capture():
    # 23:55 UTC July 7 — five minutes before the reset.
    taken = dt.datetime(2026, 7, 7, 23, 55, tzinfo=dt.timezone.utc)
    assert _partial_note(_reset_july8(), taken.isoformat()) is None


def test_note_for_a_stale_capture():
    # 16:00 UTC July 7 — hours before the reset (bot was offline after).
    taken = dt.datetime(2026, 7, 7, 16, 0, tzinfo=dt.timezone.utc)
    note = _partial_note(_reset_july8(), taken.isoformat())
    assert note is not None
    assert "incomplete" in note
    assert "16:00" in note


def test_no_note_without_a_timestamp():
    assert _partial_note(_reset_july8(), None) is None
