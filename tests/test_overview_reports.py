"""The composite overview reports (the old bot's daily/monthly member and
admin reports, rebuilt): analytics, trends, forecasts, and the two
registered builders against a seeded database."""

import datetime as dt

import pytest_asyncio

from fra_bot.db.database import Database, utcnow_iso
from fra_bot.db.repos import LogsRepo, SanctionsRepo, TreasuryRepo
from fra_bot.reporting.analytics import (
    Metric,
    gather_overview,
    previous_period,
)
from fra_bot.reporting.period import UTC, resolve_period
from fra_bot.reporting.registry import ReportRegistry
from fra_bot.reporting.reports import register_builtin_reports

# 2026-07-07 15:30 UTC — mid game day, mid month.
NOW = dt.datetime(2026, 7, 7, 15, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "overview.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def registry(db):
    reg = ReportRegistry()
    register_builtin_reports(reg, db)
    return reg


def _iso(days_ago: float) -> str:
    return (NOW - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


async def _member(db, mc_id, name, rate=10.0, credits=1000):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, "
        "earned_credits, is_active, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (mc_id, name, rate, credits, _iso(30), _iso(0)),
    )


async def _event(db, event_type, days_ago, name="Someone"):
    await db.execute(
        "INSERT INTO member_events (mc_user_id, name, event_type, "
        "occurred_at) VALUES (1, ?, ?, ?)",
        (name, event_type, _iso(days_ago)),
    )


async def _log(db, action_key, days_ago, executed="AdminAnna"):
    await db.execute(
        "INSERT INTO alliance_logs (signature, raw_timestamp, event_at, "
        "action_key, executed_name, description, scraped_at, posted_at) "
        "VALUES (?, 'x', ?, ?, ?, 'd', ?, ?)",
        (f"{action_key}-{days_ago}-{executed}", _iso(days_ago), action_key,
         executed, _iso(days_ago), utcnow_iso()),
    )


async def _snapshot(db, days_ago, credits_by_member):
    run_id = await db.execute_returning_id(
        "INSERT INTO scrape_runs (scraper, status, started_at) "
        "VALUES ('members', 'success', ?)",
        (_iso(days_ago),),
    )
    for mc_id, credits in credits_by_member.items():
        await db.execute(
            "INSERT INTO member_snapshots (run_id, mc_user_id, name, "
            "earned_credits, taken_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, mc_id, f"M{mc_id}", credits, _iso(days_ago)),
        )


# --------------------------------------------------------------------------
# Analytics building blocks
# --------------------------------------------------------------------------

def test_metric_trend_formats():
    assert Metric(5, 3).trend() == " (↑ +2 vs previous)"
    assert Metric(3, 5).trend() == " (↓ -2 vs previous)"
    assert Metric(4, 4).trend() == " (= previous)"
    assert Metric(4, None).trend() == ""


def test_previous_period_is_equal_length():
    period = resolve_period("yesterday", now=NOW)
    prev = previous_period(period)
    assert prev.end == period.start
    assert (prev.end - prev.start) == (period.end - period.start)
    assert previous_period(resolve_period("all", now=NOW)) is None


async def test_gather_counts_and_trends(db):
    await _member(db, 1, "Alice")
    await _member(db, 2, "Bob")
    # Yesterday: 2 courses; the day before: 1 -> upward trend.
    await _log(db, "created_course", 0.5)
    await _log(db, "created_course", 0.7)
    await _log(db, "created_course", 1.5)
    await _event(db, "joined", 0.5, "Newbie")
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    assert data.active_members == 2
    assert data.joined.value == 1
    assert data.courses_started.value == 2
    assert data.courses_started.previous == 1
    assert "↑" in data.courses_started.trend()


async def test_credit_deltas_feed_top_earners(db):
    # "Yesterday" runs July 6 04:00 UTC -> July 7 04:00 UTC; the baseline
    # snapshot sits before the window, the final one inside it.
    await _member(db, 1, "Alice")
    await _member(db, 2, "Bob")
    await _snapshot(db, 2.0, {1: 1000, 2: 5000})
    await _snapshot(db, 0.6, {1: 4000, 2: 5500})
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    assert data.credits_total == 3500
    assert data.top_earners[0][0] == "M1"


async def test_treasury_balance_change_and_outlook(db):
    treasury = TreasuryRepo(db)
    await db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) VALUES (?, ?)",
        (1_000_000, _iso(15)),
    )
    await db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) VALUES (?, ?)",
        (1_140_000, _iso(0.6)),
    )
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    assert data.balance == 1_140_000
    # Outlook projects the 14-day drift forward.
    assert any("Treasury" in line and "30 days" in line for line in data.outlook)


async def test_outlook_burn_rate_names_the_runway(db):
    await db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) VALUES (?, ?)",
        (2_000_000, _iso(15)),
    )
    await db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) VALUES (?, ?)",
        (1_300_000, _iso(0.05)),
    )
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    assert any("funds last" in line for line in data.outlook)


async def test_donation_pace_projects_the_month(db):
    treasury = TreasuryRepo(db)
    # NOW is July 7 NY -> monthly key 2026-07, 7 days gone of 31.
    await treasury.store_income_snapshot(
        "monthly", "2026-07",
        [{"username": "Alice", "amount": 700_000},
         {"username": "Bob", "amount": 70_000}],
    )
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    pace = [line for line in data.outlook if "on pace" in line]
    assert pace and "770,000" in pace[0]


async def test_member_outlook_projects_the_roster(db):
    await _member(db, 1, "Alice")
    for day in range(1, 8):
        await _event(db, "joined", day)
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    grow = [line for line in data.outlook if "Members" in line]
    assert grow and "grow" in grow[0]


async def test_admin_extras_and_action_items(db):
    # Sanctions stamp created_at with the real clock, so this test runs
    # on a real "today" period instead of the frozen NOW.
    await _member(db, 1, "Alice", rate=2.0)   # below the 5% minimum
    await _member(db, 2, "Bob", rate=10.0)
    await SanctionsRepo(db).add(
        mc_user_id=1, mc_username="Alice", discord_user_id=None,
        admin_discord_id=9, admin_name="Boss",
        sanction_type="Warning - Official 1st warning",
        reason="4.1 tax", status="active", source="tax",
    )
    await db.execute(
        "INSERT INTO alliance_logs (signature, raw_timestamp, event_at, "
        "action_key, executed_name, description, scraped_at, posted_at) "
        "VALUES ('adm-1', 'x', ?, 'added_to_alliance', 'AdminAnna', 'd', ?, ?)",
        ((dt.datetime.now(UTC) - dt.timedelta(hours=1))
         .isoformat(timespec="seconds"),
         utcnow_iso(), utcnow_iso()),
    )
    # The period resolves AFTER seeding: created_at stamps land strictly
    # inside the half-open [start, end) window.
    real_now = dt.datetime.now(UTC) + dt.timedelta(seconds=1)
    period = resolve_period("today", now=real_now)
    data = await gather_overview(db, period, admin=True, now=real_now)
    assert data.sanctions_issued.value == 1
    assert data.tax_warnings_issued == 1
    assert data.low_contributors == 1
    assert data.most_active_admins[0][0] == "AdminAnna"


# --------------------------------------------------------------------------
# The registered builders
# --------------------------------------------------------------------------

async def test_overview_reports_register(registry):
    names = registry.names()
    assert "overview-member" in names and "overview-admin" in names
    assert registry.get("overview-member").default_period == "yesterday"


async def test_member_overview_renders_sections(db, registry):
    await _member(db, 1, "Alice")
    await _log(db, "created_course", 0.5)
    report = registry.get("overview-member")
    result = await report.builder(resolve_period("yesterday", now=NOW))
    names = [f.name for f in result.fields]
    assert any("Members" in n for n in names)
    assert any("Game activity" in n for n in names)
    assert any("Activity score" in n for n in names)
    # Member report never carries admin sections.
    assert not any("Action items" in n for n in names)
    assert not any("Sanctions" in n for n in names)


async def test_admin_overview_renders_action_items(db, registry):
    await _member(db, 1, "Alice", rate=2.0)
    report = registry.get("overview-admin")
    result = await report.builder(resolve_period("yesterday", now=NOW))
    names = [f.name for f in result.fields]
    assert any("Action items" in n for n in names)
    items = next(f.value for f in result.fields if "Action items" in f.name)
    assert "below the 5%" in items


async def test_overview_field_sizes_fit_discord(db, registry):
    # Discord: max 25 fields, 1024 chars per field value.
    for mc_id in range(1, 40):
        await _member(db, mc_id, f"Member{mc_id}")
    for day in (0.2, 0.4, 0.6, 0.8):
        await _log(db, "created_course", day)
        await _log(db, "large_mission_started", day)
    report = registry.get("overview-admin")
    result = await report.builder(resolve_period("yesterday", now=NOW))
    assert len(result.fields) <= 25
    assert all(len(f.value) <= 1024 for f in result.fields)


async def test_unbounded_period_is_refused(db, registry):
    report = registry.get("overview-member")
    result = await report.builder(resolve_period("all", now=NOW))
    assert "bounded" in result.description


# --------------------------------------------------------------------------
# The built-in daily posting (member -> reports, admin -> admin log)
# --------------------------------------------------------------------------

class _Channel:
    def __init__(self):
        self.embeds = []

    async def send(self, embed=None, **kwargs):
        self.embeds.append(embed)


def _reporting_cog(db, registry, *, overviews=True):
    import asyncio
    from types import SimpleNamespace

    from fra_bot.cogs.reporting import ReportingCog

    channels = {"reports": _Channel(), "admin_log": _Channel()}
    bot = SimpleNamespace(
        db=db,
        reports=registry,
        cfg=SimpleNamespace(
            reports=SimpleNamespace(
                daily_delay_minutes=10, scheduled=(), overviews=overviews,
            ),
        ),
        channel_for=lambda key: channels.get(key),
        get_channel=lambda cid: None,
    )
    cog = ReportingCog.__new__(ReportingCog)
    cog.bot = bot
    cog.registry = registry
    cog._task = None
    return cog, channels


async def test_daily_tick_posts_both_overviews(db, registry):
    cog, channels = _reporting_cog(db, registry)
    await cog._run_scheduled(dt.datetime(2026, 7, 7, 0, 12))
    assert len(channels["reports"].embeds) == 1
    assert len(channels["admin_log"].embeds) == 1
    assert "Alliance overview" in channels["reports"].embeds[0].title
    assert "Admin overview" in channels["admin_log"].embeds[0].title


async def test_first_of_the_month_adds_the_monthly_digest(db, registry):
    cog, channels = _reporting_cog(db, registry)
    await cog._run_scheduled(dt.datetime(2026, 7, 1, 0, 12))
    assert len(channels["reports"].embeds) == 2   # yesterday + prev-month
    assert len(channels["admin_log"].embeds) == 2


async def test_overviews_setting_turns_the_posts_off(db, registry):
    cog, channels = _reporting_cog(db, registry, overviews=False)
    await cog._run_scheduled(dt.datetime(2026, 7, 7, 0, 12))
    assert channels["reports"].embeds == []
    assert channels["admin_log"].embeds == []
