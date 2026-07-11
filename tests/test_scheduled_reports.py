"""Runtime-managed scheduled reports: the state-backed override that
replaces config.yaml's reports.scheduled list."""

from types import SimpleNamespace

import pytest_asyncio

from fra_bot.config import ScheduledReport
from fra_bot.core import scheduled_reports as sched
from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "sched.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _cfg(*entries):
    return SimpleNamespace(reports=SimpleNamespace(scheduled=tuple(entries)))


def _entry(**overrides):
    fields = dict(report="treasury", period="yesterday", cadence="daily",
                  channel_id=544461383358480385, weekday=0, day=1, month=1)
    fields.update(overrides)
    return ScheduledReport(**fields)


async def test_store_apply_and_reload_round_trip(db):
    state = StateRepo(db)
    entries = (
        _entry(),
        _entry(period="week", cadence="weekly"),
        _entry(period="prev-month", cadence="monthly"),
        _entry(period="prev-year", cadence="yearly"),
    )
    await sched.store_entries(state, entries)

    cfg = _cfg()  # empty YAML schedule
    assert await sched.apply_stored(cfg, state) is True
    assert cfg.reports.scheduled == entries

    # A fresh load (restart) sees the same override.
    assert await sched.load_override(state) == entries


async def test_no_override_leaves_yaml_schedule_alone(db):
    yaml_entry = _entry(report="credits")
    cfg = _cfg(yaml_entry)
    assert await sched.apply_stored(cfg, StateRepo(db)) is False
    assert cfg.reports.scheduled == (yaml_entry,)


async def test_clear_override_reports_whether_one_existed(db):
    state = StateRepo(db)
    assert await sched.clear_override(state) is False
    await sched.store_entries(state, (_entry(),))
    assert await sched.clear_override(state) is True
    assert await sched.load_override(state) is None


async def test_unreadable_override_falls_back_to_yaml(db):
    state = StateRepo(db)
    await state.set(sched.STATE_KEY, "not json at all")
    cfg = _cfg(_entry(report="credits"))
    assert await sched.apply_stored(cfg, state) is False
    assert cfg.reports.scheduled[0].report == "credits"


def test_describe_lines_cover_every_cadence():
    assert "daily" in sched.describe(_entry(), 1)
    assert "(Monday)" in sched.describe(_entry(cadence="weekly"), 2)
    assert "(day 1)" in sched.describe(_entry(cadence="monthly"), 3)
    assert "01-01" in sched.describe(_entry(cadence="yearly"), 4)
    assert "<#544461383358480385>" in sched.describe(_entry(), 1)


def test_treasury_report_supports_all_four_cadence_periods():
    from fra_bot.reporting import ReportRegistry
    from fra_bot.reporting.reports import register_builtin_reports

    registry = ReportRegistry()
    register_builtin_reports(registry, db=None)  # builders aren't invoked
    treasury = registry.get("treasury")
    for period in ("yesterday", "week", "prev-month", "prev-year"):
        assert period in treasury.periods
