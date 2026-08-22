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
        self.files = []

    async def send(self, embed=None, file=None, **kwargs):
        self.embeds.append(embed)
        self.files.append(file)


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
    assert "Daily overview" in channels["reports"].embeds[0].title
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


# --------------------------------------------------------------------------
# The rendered daily card
# --------------------------------------------------------------------------

async def test_member_post_is_one_message_with_a_card(db, registry):
    await _member(db, 1, "BrandjuhNL")
    await _log(db, "created_course", 0.5)
    cog, channels = _reporting_cog(db, registry)
    await cog._run_scheduled(dt.datetime(2026, 7, 7, 0, 12))
    # ONE message on the member side: the card image plus its embed.
    assert len(channels["reports"].embeds) == 1
    assert channels["reports"].files[0] is not None
    embed = channels["reports"].embeds[0]
    assert embed.image.url == "attachment://daily-overview.png"


async def test_card_maps_the_overview_without_arrow_glyphs(db):
    from fra_bot.services.report_card import card_from_overview, render_daily_card

    await _member(db, 1, "BrandjuhNL")
    await _member(db, 2, "Bob")
    await _snapshot(db, 2.0, {1: 1000, 2: 5000})
    await _snapshot(db, 0.6, {1: 4000, 2: 5500})
    await _log(db, "created_course", 0.5)
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    card = card_from_overview(data, "06 Jul 2026")

    assert card.top_earners and card.top_earners[0][1] == 3000
    assert any(label == "Members" for label, _, _ in card.tiles)
    assert card.activity                      # a course was started
    # The bundled PIL font has no arrow/emoji glyphs — they would render
    # as tofu boxes on the card.
    text = " ".join(
        part for tile in card.tiles for part in tile
    ) + " " + card.footer
    assert text.isascii(), text

    png = render_daily_card(card)
    assert png is None or png.startswith(b"\x89PNG")


async def test_all_zero_game_chips_are_culled_but_donations_survive(db):
    """A quiet day has nothing to chart, so the row of zeros is padding —
    but a real donation figure is still worth stating."""
    from fra_bot.services.report_card import card_from_overview

    await _member(db, 1, "Alice")
    await TreasuryRepo(db).store_income_snapshot(
        "daily", "2026-07-06", [{"username": "Alice", "amount": 500}],
    )
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    card = card_from_overview(data, "06 Jul 2026")
    assert [(l, v) for l, v, _ in card.activity] == [("Donated", "500")]


async def test_a_missing_snapshot_says_no_data_instead_of_zero(db):
    """The live complaint: a night the capture was lost reported
    "Donated: 0 credits by 0 member(s)" as fact, next to Funds and Spent
    tiles showing real movement."""
    from fra_bot.services.report_card import card_from_overview

    await _member(db, 1, "Alice")
    period = resolve_period("yesterday", now=NOW)
    data = await gather_overview(db, period, admin=False, now=NOW)
    assert data.donations_total is None
    assert data.donations_missing is True
    card = card_from_overview(data, "06 Jul 2026")
    assert [(l, v) for l, v, _ in card.activity] == [("Donated", "no data")]


async def test_member_names_are_not_recapitalised_on_the_card():
    # _bar_panel title-cases building types; member names must survive
    # exactly as the player spells them.
    from fra_bot.services.infographic import _bar_panel

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    draw = ImageDraw.Draw(Image.new("RGB", (1080, 400)))
    # Smoke test: both modes render without raising, and cap=False is the
    # documented way to keep "BrandjuhNL" intact.
    assert _bar_panel(draw, 0, "T", [("BrandjuhNL", 5)], cap=False) > 0
    assert _bar_panel(draw, 0, "T", [("fire station", 5)]) > 0


def test_ascii_fold_keeps_names_readable():
    from fra_bot.services.report_card import ascii_only

    # Accents decompose instead of being dropped — a member keeps a
    # recognisable name on the card.
    assert ascii_only("Café Ångström") == "Cafe Angstrom"
    assert ascii_only("Jürgen Müller") == "Jurgen Muller"
    # Typographic punctuation from the embed texts folds to its twin.
    assert ascii_only("last 14 days — at that pace") == "last 14 days - at that pace"
    assert ascii_only("a…b") == "a...b"


async def test_report_card_preview_renders_into_the_given_channel(db, registry):
    """`!fra reportcard` must preview into the channel it was typed in,
    never into the members' reports channel."""
    await _member(db, 1, "BrandjuhNL")
    await _log(db, "created_course", 0.5)
    cog, channels = _reporting_cog(db, registry)
    here = _Channel()
    await cog._post_member_card("yesterday", channel=here)
    assert len(here.embeds) == 1
    assert channels["reports"].embeds == []      # members saw nothing


# --------------------------------------------------------------------------
# The compact two-column card
# --------------------------------------------------------------------------

def _filled_overview():
    """An OverviewData with every card section populated."""
    from types import SimpleNamespace

    from fra_bot.reporting.analytics import Metric, OverviewData

    data = OverviewData(period=SimpleNamespace(name="yesterday"))
    data.active_members = 947
    data.joined, data.left = Metric(5, 2), Metric(2, 4)
    data.credits_total, data.credits_earners = 18_442_900, 214
    data.top_earners = [("BrandjuhNL", 812_400), ("MedicOne", 512_800)]
    data.donations_total, data.donations_contributors = 5_431_200, 10
    data.top_donors = [("BrandjuhNL", 1_204_000), ("MedicOne", 890_000)]
    data.balance, data.balance_change = 41_882_400, 1_205_000
    data.courses_started, data.courses_completed = Metric(12, 9), Metric(9, 11)
    data.missions_started, data.events_started = Metric(4, 4), Metric(2, 1)
    data.activity_score, data.activity_score_previous = 62, 58
    return data


def test_alliance_funds_survive_alongside_donations():
    """The two used to share one tile through an elif, so on any day with
    donations the balance vanished from the card entirely."""
    from fra_bot.services.report_card import card_from_overview

    card = card_from_overview(_filled_overview(), "19 Aug 2026")
    labels = [label for label, _, _ in card.tiles]
    assert "Alliance funds" in labels
    funds = next(t for t in card.tiles if t[0] == "Alliance funds")
    assert funds[1] == "41,882,400" and "+1,205,000" in funds[2]
    # Donations moved to the compact row rather than displacing it.
    assert any(label == "Donated" for label, _, _ in card.activity)


def test_activity_chips_carry_their_own_comparison():
    from fra_bot.services.report_card import card_from_overview

    card = card_from_overview(_filled_overview(), "19 Aug 2026")
    chips = {label: (value, sub) for label, value, sub in card.activity}
    assert chips["Courses started"] == ("12", "+3 vs prev")
    assert chips["Courses done"] == ("9", "-2 vs prev")
    assert chips["Large missions"] == ("4", "same vs prev")


def test_the_card_stays_ascii_and_renders():
    """Every string reaching the renderer must be drawable: the bundled
    PIL font has no arrow or emoji glyphs."""
    from fra_bot.services.report_card import card_from_overview, render_daily_card

    card = card_from_overview(_filled_overview(), "19 Aug 2026")
    text = " ".join(
        part
        for group in (card.tiles, card.activity)
        for row in group
        for part in row
    )
    assert text.isascii(), text
    png = render_daily_card(card)
    assert png is None or png.startswith(b"\x89PNG")


def test_two_columns_halve_the_card_height():
    """Top earners and donors are read together; stacked full-width they
    doubled the height and pushed the reader past one to reach the other."""
    from fra_bot.services.report_card import card_from_overview, render_daily_card

    png = render_daily_card(card_from_overview(_filled_overview(), "19 Aug 2026"))
    if png is None:
        return                                  # Pillow absent
    import io

    from PIL import Image

    height = Image.open(io.BytesIO(png)).height
    assert height < 1100, f"card is {height}px tall — the columns regressed"


def test_bar_panel_still_spans_the_card_by_default():
    """The width arguments are opt-in: the infographic and fleet cards
    share this helper and must be untouched."""
    from PIL import Image, ImageDraw

    from fra_bot.services.infographic import _PAD, _WIDTH, _bar_panel

    image = Image.new("RGB", (_WIDTH, 400))
    draw = ImageDraw.Draw(image)
    _bar_panel(draw, 0, "TOP", [("engine", 410), ("ladder", 150)])
    # The panel's right edge is painted at _WIDTH - _PAD when unbounded.
    assert image.getpixel((_WIDTH - _PAD - 4, 60)) != (0, 0, 0)
    assert image.getpixel((_WIDTH - _PAD // 2, 60)) == (0, 0, 0)


# --------------------------------------------------------------------------
# A finished period's standings must not be rewritten downward
# --------------------------------------------------------------------------

async def test_a_smaller_snapshot_cannot_overwrite_the_day(db):
    """Within one period the game's list is cumulative, so it can only
    grow. A smaller total means a partial page, a truncated table, or a
    table read just after the reset while we still held the old key —
    and because latest_snapshot reads MAX(taken_at), accepting it left
    the good batch in the table but unreachable."""
    treasury = TreasuryRepo(db)
    full = [{"username": f"M{i}", "amount": 100_000} for i in range(25)]
    assert await treasury.store_income_snapshot("daily", "2026-08-20", full) is True
    assert await treasury.snapshot_total("daily", "2026-08-20") == 2_500_000

    # A partially rendered page arrives later.
    accepted = await treasury.store_income_snapshot(
        "daily", "2026-08-20", [{"username": "EarlyBird", "amount": 500}],
    )
    assert accepted is False
    assert await treasury.snapshot_total("daily", "2026-08-20") == 2_500_000
    assert len(await treasury.latest_snapshot("daily", "2026-08-20")) == 25
    # The refusal carries the numbers, so the admin notice can say which
    # of the two readings is the odd one.
    detail = treasury.last_snapshot_refusal
    assert detail["incoming"] == 500 and detail["stored"] == 2_500_000
    assert detail["incoming_rows"] == 1 and detail["stored_rows"] == 25


async def test_a_growing_snapshot_still_replaces_the_day(db):
    treasury = TreasuryRepo(db)
    await treasury.store_income_snapshot(
        "daily", "2026-08-20", [{"username": "A", "amount": 100}],
    )
    assert await treasury.store_income_snapshot(
        "daily", "2026-08-20",
        [{"username": "A", "amount": 300}, {"username": "B", "amount": 50}],
    ) is True
    assert await treasury.snapshot_total("daily", "2026-08-20") == 350


async def test_a_new_period_key_starts_from_scratch(db):
    """The guard is per key: the next day legitimately starts near zero."""
    treasury = TreasuryRepo(db)
    await treasury.store_income_snapshot(
        "daily", "2026-08-20", [{"username": "A", "amount": 2_000_000}],
    )
    assert await treasury.store_income_snapshot(
        "daily", "2026-08-21", [{"username": "A", "amount": 10}],
    ) is True
    assert await treasury.snapshot_total("daily", "2026-08-21") == 10


async def test_an_ordinary_wobble_is_still_accepted(db):
    """The guard is a guess about a page this code cannot see. If the
    game's daily list turns out to be a rolling window rather than a
    cumulative one, a strict never-shrink rule would reject the truth all
    day — so only a COLLAPSE counts."""
    treasury = TreasuryRepo(db)
    await treasury.store_income_snapshot(
        "daily", "2026-08-20", [{"username": "A", "amount": 1_000_000}],
    )
    assert await treasury.store_income_snapshot(
        "daily", "2026-08-20", [{"username": "A", "amount": 800_000}],
    ) is True
    assert await treasury.snapshot_total("daily", "2026-08-20") == 800_000


async def test_a_stale_stored_batch_stops_blocking_the_truth(db):
    """A wrong stored value must not become permanent: if the game keeps
    reporting a smaller number, the game is the better witness."""
    from fra_bot.db.repos import SNAPSHOT_TRUST_MINUTES

    treasury = TreasuryRepo(db)
    await treasury.store_income_snapshot(
        "daily", "2026-08-21", [{"username": "A", "amount": 2_000_000}],
    )
    # Age the stored batch past the trust window.
    stale = (
        dt.datetime.now(UTC)
        - dt.timedelta(minutes=SNAPSHOT_TRUST_MINUTES + 5)
    ).isoformat()
    await db.execute(
        "UPDATE income_snapshots SET taken_at = ? WHERE period_key = ?",
        (stale, "2026-08-21"),
    )
    assert await treasury.store_income_snapshot(
        "daily", "2026-08-21", [{"username": "A", "amount": 1_000}],
    ) is True
    assert await treasury.snapshot_total("daily", "2026-08-21") == 1_000
