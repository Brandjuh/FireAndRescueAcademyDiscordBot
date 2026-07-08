"""Tests for the reporting framework: period maths, the registry and a
couple of built-in report builders against a seeded database."""

import datetime as dt

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.reporting import (
    Period,
    Report,
    ReportRegistry,
    ReportResult,
    resolve_period,
)
from fra_bot.reporting.period import NY, UTC
from fra_bot.reporting.reports import register_builtin_reports

# asyncio_mode = auto (pytest.ini) runs the async tests without a mark.


# --------------------------------------------------------------------------
# Period maths (pure, no DB)
# --------------------------------------------------------------------------

# 2026-07-07 11:30 EDT (UTC-4) -> 15:30 UTC. NY midnight is 04:00 UTC.
NOW = dt.datetime(2026, 7, 7, 15, 30, tzinfo=UTC)
NY_MIDNIGHT = dt.datetime(2026, 7, 7, 0, 0, tzinfo=NY).astimezone(UTC)


def test_today_starts_at_ny_midnight():
    period = resolve_period("today", now=NOW)
    assert period.start == NY_MIDNIGHT
    assert period.end == NOW
    assert period.start_iso is not None


def test_yesterday_is_the_prior_ny_day():
    period = resolve_period("yesterday", now=NOW)
    assert period.end == NY_MIDNIGHT
    assert period.start == NY_MIDNIGHT - dt.timedelta(days=1)


def test_week_is_rolling_seven_days():
    period = resolve_period("week", now=NOW)
    assert period.start == NOW - dt.timedelta(days=7)
    assert period.end == NOW


def test_month_starts_on_the_first_ny_day():
    period = resolve_period("month", now=NOW)
    first = dt.datetime(2026, 7, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    assert period.start == first


def test_prev_month_spans_the_previous_month():
    period = resolve_period("prev-month", now=NOW)
    june_first = dt.datetime(2026, 6, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    july_first = dt.datetime(2026, 7, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    assert period.start == june_first
    assert period.end == july_first


def test_year_starts_on_jan_1():
    period = resolve_period("year", now=NOW)
    jan1 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    assert period.start == jan1
    assert period.end == NOW


def test_prev_year_spans_the_previous_year():
    period = resolve_period("prev-year", now=NOW)
    jan1_2025 = dt.datetime(2025, 1, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    jan1_2026 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    assert period.start == jan1_2025
    assert period.end == jan1_2026


def test_scheduled_report_yearly_cadence_is_due():
    from types import SimpleNamespace

    from fra_bot.cogs.reporting import ReportingCog

    sched = SimpleNamespace(cadence="yearly", month=1, day=1, weekday=0)
    assert ReportingCog._is_due(sched, dt.datetime(2027, 1, 1, 0, 5)) is True
    assert ReportingCog._is_due(sched, dt.datetime(2027, 2, 1, 0, 5)) is False
    assert ReportingCog._is_due(sched, dt.datetime(2027, 1, 2, 0, 5)) is False


def test_all_period_is_unbounded_below():
    period = resolve_period("all", now=NOW)
    assert period.start is None
    assert period.start_iso is None
    assert period.end == NOW


def test_unknown_period_raises():
    with pytest.raises(ValueError):
        resolve_period("fortnight", now=NOW)


def test_default_period_is_today():
    assert resolve_period("", now=NOW).name == "today"


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

async def _noop(period: Period) -> ReportResult:
    return ReportResult(title="x")


def test_registry_register_and_get():
    reg = ReportRegistry()
    reg.register(Report("demo", "A demo", _noop))
    assert reg.get("demo").description == "A demo"
    # Lookup is case-insensitive.
    assert reg.get("DEMO") is not None
    assert reg.get("missing") is None


def test_registry_rejects_duplicates():
    reg = ReportRegistry()
    reg.register(Report("demo", "A demo", _noop))
    with pytest.raises(ValueError):
        reg.register(Report("demo", "again", _noop))


def test_registry_all_is_sorted():
    reg = ReportRegistry()
    reg.register(Report("beta", "", _noop))
    reg.register(Report("alpha", "", _noop))
    assert [r.name for r in reg.all()] == ["alpha", "beta"]
    assert reg.names() == ["alpha", "beta"]


def test_report_default_period_is_first():
    report = Report("demo", "", _noop, periods=("week", "month"))
    assert report.default_period == "week"


# --------------------------------------------------------------------------
# Built-in report builders against a seeded database
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "reporting.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def registry(db):
    reg = ReportRegistry()
    register_builtin_reports(reg, db)
    return reg


def test_builtin_reports_all_register(registry):
    names = registry.names()
    for expected in (
        "income-daily",
        "income-monthly",
        "members",
        "credits",
        "treasury",
        "logs",
        "automation",
    ):
        assert expected in names


async def _run(registry, name, period_name):
    report = registry.get(name)
    period = resolve_period(period_name, now=NOW)
    return await report.builder(period)


async def test_members_report_counts_events(db, registry):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, is_active, first_seen_at, last_seen_at) "
        "VALUES (1, 'Alice', 1, ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    when = (NOW - dt.timedelta(hours=1)).isoformat()
    for etype in ("joined", "joined", "left"):
        await db.execute(
            "INSERT INTO member_events (name, event_type, occurred_at) VALUES (?, ?, ?)",
            ("Alice", etype, when),
        )
    result = await _run(registry, "members", "today")
    text = "\n".join(f.value for f in result.fields)
    assert "Joined: 2" in text
    assert "Left: 1" in text


async def test_treasury_report_summarises_expenses(db, registry):
    await db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) VALUES (?, ?)",
        (5_000_000, NOW.isoformat()),
    )
    when = (NOW - dt.timedelta(hours=2)).isoformat()
    for user, amount in (("Alice", 1000), ("Bob", 2500), ("Alice", 500)):
        await db.execute(
            "INSERT INTO expenses (signature, raw_date, event_at, username, amount, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"{user}{amount}", "x", when, user, amount, NOW.isoformat()),
        )
    result = await _run(registry, "treasury", "today")
    joined = " ".join(f"{f.name}={f.value}" for f in result.fields)
    assert "5,000,000" in joined         # balance
    assert "4,000 credits" in joined     # total spent
    assert "Bob: 2,500" in joined        # top spender


async def test_income_daily_uses_latest_snapshot(db, registry):
    from fra_bot.db.repos import TreasuryRepo, ny_period_keys

    day_key, _ = ny_period_keys()
    await TreasuryRepo(db).store_income_snapshot(
        "daily", day_key,
        [
            {"username": "Alice", "amount": 9000, "mc_user_id": 1},
            {"username": "Bob", "amount": 4000, "mc_user_id": 2},
        ],
    )
    report = registry.get("income-daily")
    result = await report.builder(resolve_period("today"))
    assert "Alice" in result.description
    assert "9,000" in result.description
    assert result.colour == 0xF1C40F


async def test_automation_report_groups_by_kind(db, registry):
    now_iso = (NOW - dt.timedelta(hours=1)).isoformat()
    rows = [
        ("training", "done"),
        ("training", "done"),
        ("event", "failed"),
    ]
    for kind, status in rows:
        await db.execute(
            "INSERT INTO automation_requests "
            "(kind, thread_id, post_id, status, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?, ?)",
            (kind, status, now_iso, now_iso),
        )
    result = await _run(registry, "automation", "today")
    joined = " ".join(f"{f.name}: {f.value}" for f in result.fields)
    assert "Training: done: 2" in joined
    assert "Event: failed: 1" in joined
