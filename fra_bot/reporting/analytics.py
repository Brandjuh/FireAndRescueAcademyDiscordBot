"""The number-crunching behind the overview reports.

The reference bot's alliance_reports cog shipped calculators for trends,
predictions, an activity score and fun facts — none of which ever got
wired into its templates. This module is that promise, kept, on this
bot's much richer data: every figure comes with a comparison against the
equal-length previous period, and the outlook section projects forward
from real stored history (hourly member snapshots, the balance ledger,
income snapshots) instead of a bounded guess.

Forecast honesty rules:

* a projection is only shown when there is enough history to project
  from (no "predicted: 0" placeholders);
* every projection names its basis ("at the last 14 days' pace"), so a
  reader can discount it when the pace was unusual;
* projections extrapolate linearly and are labelled as such — no fake
  confidence intervals.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..db.database import Database
from ..db.repos import (
    ApplicationsRepo,
    AutomationRepo,
    LinksRepo,
    LogsRepo,
    MembersRepo,
    MissionsRepo,
    SanctionsRepo,
    TreasuryRepo,
    ny_period_keys,
)
from .period import NY, Period

#: Log actions that are performed by alliance admins/staff — the basis of
#: the "most active admin" line. Bot-executed starts are filtered out by
#: name in the query result (the bot account shows up as executor too).
ADMIN_ACTIONS = (
    "added_to_alliance", "application_denied", "kicked_from_alliance",
    "set_admin", "removed_admin", "set_co_admin", "removed_co_admin",
    "chat_ban_set", "chat_ban_removed",
    "allowed_to_apply", "not_allowed_to_apply",
)

#: Below this donation percentage a member fails the request gates and
#: the tax sweep — the action-item threshold.
TAX_MINIMUM = 5.0

#: History window for the pace-based forecasts.
FORECAST_LOOKBACK_DAYS = 14


@dataclass
class Metric:
    """A number now and the same number over the previous period."""

    value: int = 0
    previous: int | None = None

    def trend(self) -> str:
        """Compact arrow + delta against the previous period, or ''."""
        if self.previous is None:
            return ""
        delta = self.value - self.previous
        if delta == 0:
            return " (= previous)"
        arrow = "↑" if delta > 0 else "↓"
        return f" ({arrow} {delta:+,} vs previous)"

    def trend_plain(self, unit: str = "") -> str:
        """ASCII-only trend for the rendered card. The bundled PIL font
        has no arrow glyphs — they come out as tofu boxes."""
        if self.previous is None:
            return ""
        delta = self.value - self.previous
        suffix = f" {unit}" if unit else ""
        if delta == 0:
            return f"same as previous{suffix}"
        return f"{delta:+,}{suffix} vs previous"


@dataclass
class OverviewData:
    period: Period

    # Membership
    active_members: int = 0
    joined: Metric = field(default_factory=Metric)
    left: Metric = field(default_factory=Metric)

    # Credits / income
    credits_total: int = 0
    credits_earners: int = 0
    top_earners: list[tuple[str, int]] = field(default_factory=list)
    donations_total: int | None = None
    donations_contributors: int | None = None
    top_donor: tuple[str, int] | None = None
    top_donors: list[tuple[str, int]] = field(default_factory=list)

    # Treasury
    balance: int | None = None
    balance_change: int | None = None
    spent_total: int = 0
    spent_rows: int = 0
    top_spender: tuple[str, int] | None = None

    # Game activity (alliance logs)
    courses_started: Metric = field(default_factory=Metric)
    courses_completed: Metric = field(default_factory=Metric)
    missions_started: Metric = field(default_factory=Metric)
    events_started: Metric = field(default_factory=Metric)
    buildings_constructed: Metric = field(default_factory=Metric)
    expansions_finished: Metric = field(default_factory=Metric)

    # Bot pipeline
    requests_by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    missions_by_status: dict[str, int] = field(default_factory=dict)

    # Admin-only extras
    sanctions_issued: Metric = field(default_factory=Metric)
    sanctions_by_type: dict[str, int] = field(default_factory=dict)
    tax_warnings_issued: int = 0
    sanctions_register: dict[str, int] = field(default_factory=dict)
    verifications_approved: Metric = field(default_factory=Metric)
    most_active_admins: list[tuple[str, int]] = field(default_factory=list)

    # Action items
    open_applications: int = 0
    sanctions_unverified: int = 0
    open_requests: int = 0
    open_missions: int = 0
    low_contributors: int = 0

    # Derived
    activity_score: int = 0
    activity_score_previous: int | None = None
    outlook: list[str] = field(default_factory=list)
    fun_facts: list[str] = field(default_factory=list)


def previous_period(period: Period) -> Period | None:
    """The equal-length window ending where ``period`` starts."""
    if period.start is None:
        return None
    length = period.end - period.start
    if length <= dt.timedelta(0):
        return None
    return Period(period.name, "previous", period.start - length, period.start)


async def gather_overview(
    db: Database, period: Period, *, admin: bool,
    now: dt.datetime | None = None,
) -> OverviewData:
    """All figures for one overview report, current + previous period."""
    now = now or dt.datetime.now(dt.timezone.utc)
    members = MembersRepo(db)
    logs = LogsRepo(db)
    treasury = TreasuryRepo(db)
    automation = AutomationRepo(db)
    missions = MissionsRepo(db)
    prev = previous_period(period)

    data = OverviewData(period=period)

    # -- membership ----------------------------------------------------
    data.active_members = await members.active_count()
    events = await members.event_counts(period.start_iso, period.end_iso)
    prev_events = (
        await members.event_counts(prev.start_iso, prev.end_iso) if prev else {}
    )
    data.joined = Metric(
        events.get("joined", 0),
        prev_events.get("joined") if prev else None,
    )
    data.left = Metric(
        events.get("left", 0), prev_events.get("left") if prev else None
    )

    # -- credits earned -------------------------------------------------
    if period.start_iso:
        deltas = await members.credit_deltas(period.start_iso, period.end_iso)
        data.credits_total = sum(r["delta"] for r in deltas)
        data.credits_earners = len(deltas)
        data.top_earners = [(r["name"], r["delta"]) for r in deltas[:3]]

    # -- donations (income snapshots; game day / month only) ------------
    rows = await _income_rows_for(treasury, period, now)
    if rows is not None:
        data.donations_total = sum(r["amount"] for r in rows)
        data.donations_contributors = len(rows)
        if rows:
            data.top_donor = (rows[0]["username"], rows[0]["amount"])
            data.top_donors = [
                (row["username"], row["amount"]) for row in rows[:5]
            ]

    # -- treasury -------------------------------------------------------
    balance_row = await treasury.balance_at(period.end_iso)
    if balance_row is not None:
        data.balance = balance_row["total_funds"]
        if period.start_iso:
            opening = await treasury.balance_at(period.start_iso)
            if opening is not None:
                data.balance_change = data.balance - opening["total_funds"]
    spend = await treasury.expense_summary(period.start_iso, period.end_iso)
    data.spent_total = spend["total"]
    data.spent_rows = spend["count"]
    if spend["top"]:
        data.top_spender = spend["top"][0]

    # -- game activity --------------------------------------------------
    counts = await logs.action_counts(period.start_iso, period.end_iso)
    prev_counts = (
        await logs.action_counts(prev.start_iso, prev.end_iso) if prev else {}
    )

    def metric(key: str) -> Metric:
        return Metric(
            counts.get(key, 0), prev_counts.get(key, 0) if prev else None
        )

    data.courses_started = metric("created_course")
    data.courses_completed = metric("course_completed")
    data.missions_started = metric("large_mission_started")
    data.events_started = metric("alliance_event_started")
    data.buildings_constructed = metric("building_constructed")
    data.expansions_finished = metric("expansion_finished")

    # -- bot pipeline ---------------------------------------------------
    for row in await automation.activity_counts(period.start_iso, period.end_iso):
        data.requests_by_kind.setdefault(row["kind"], {})[row["status"]] = row["n"]
    data.missions_by_status = await missions.status_counts(
        period.start_iso, period.end_iso
    )

    # -- derived --------------------------------------------------------
    data.activity_score = _activity_score(data)
    if prev:
        prev_data = OverviewData(period=prev)
        prev_data.joined = Metric(prev_events.get("joined", 0))
        prev_data.left = Metric(prev_events.get("left", 0))
        prev_data.courses_started = Metric(prev_counts.get("created_course", 0))
        prev_data.courses_completed = Metric(prev_counts.get("course_completed", 0))
        prev_data.missions_started = Metric(
            prev_counts.get("large_mission_started", 0)
        )
        prev_data.events_started = Metric(
            prev_counts.get("alliance_event_started", 0)
        )
        prev_deltas = await members.credit_deltas(prev.start_iso, prev.end_iso)
        prev_data.credits_total = sum(r["delta"] for r in prev_deltas)
        prev_data.credits_earners = len(prev_deltas)
        data.activity_score_previous = _activity_score(prev_data)

    data.outlook = await _outlook(db, data, now)
    data.fun_facts = _fun_facts(data, now)

    # -- admin extras ---------------------------------------------------
    if admin:
        await _gather_admin(db, data, period, prev)
    return data


async def _gather_admin(
    db: Database, data: OverviewData, period: Period, prev: Period | None
) -> None:
    sanctions = SanctionsRepo(db)
    links = LinksRepo(db)
    apps = ApplicationsRepo(db)
    members = MembersRepo(db)
    logs = LogsRepo(db)
    automation = AutomationRepo(db)
    missions = MissionsRepo(db)

    rows = await sanctions.issued_between(period.start_iso, period.end_iso)
    prev_rows = (
        await sanctions.issued_between(prev.start_iso, prev.end_iso)
        if prev else None
    )
    data.sanctions_issued = Metric(
        len(rows), len(prev_rows) if prev_rows is not None else None
    )
    for row in rows:
        key = row["sanction_type"]
        data.sanctions_by_type[key] = data.sanctions_by_type.get(key, 0) + 1
    data.tax_warnings_issued = sum(1 for r in rows if r["source"] == "tax")
    data.sanctions_register = await sanctions.status_summary()

    data.verifications_approved = Metric(
        await links.approved_count_between(period.start_iso, period.end_iso),
        (
            await links.approved_count_between(prev.start_iso, prev.end_iso)
            if prev else None
        ),
    )
    data.most_active_admins = await logs.executor_counts(
        period.start_iso, period.end_iso, ADMIN_ACTIONS, limit=3
    )

    data.open_applications = await apps.open_count()
    data.sanctions_unverified = data.sanctions_register.get("unverified", 0)
    data.open_requests = await automation.open_count()
    data.open_missions = await missions.open_count()
    data.low_contributors = await members.low_contributor_count(TAX_MINIMUM)


# -- income snapshots ------------------------------------------------------

def _period_days(period: Period) -> float:
    if period.start is None:
        return 0.0
    return max(0.0, (period.end - period.start).total_seconds() / 86400)


async def _income_rows_for(treasury: TreasuryRepo, period: Period, now):
    """Snapshot rows covering the period, or None when no snapshot maps
    onto it (weeks/years have no income list in the game)."""
    ny_now = now.astimezone(NY)
    day_key, month_key = ny_period_keys(now)
    if period.name == "today":
        return await treasury.latest_snapshot("daily", day_key)
    if period.name == "yesterday":
        prev_day = (ny_now.date() - dt.timedelta(days=1)).isoformat()
        return await treasury.latest_snapshot("daily", prev_day)
    if period.name == "month":
        return await treasury.latest_snapshot("monthly", month_key)
    if period.name == "prev-month":
        prev_month = (ny_now.date().replace(day=1) - dt.timedelta(days=1))
        return await treasury.latest_snapshot(
            "monthly", prev_month.strftime("%Y-%m")
        )
    return None


# -- activity score --------------------------------------------------------

def _component(value: float, full_marks: float) -> float:
    """0..1, saturating at ``full_marks`` — simple and explainable."""
    if full_marks <= 0:
        return 0.0
    return min(1.0, max(0.0, value / full_marks))


def _activity_score(data: OverviewData) -> int:
    """0-100 across five equally weighted components, scaled to the
    period length so a day and a month judge against fair yardsticks."""
    days = max(1.0, _period_days(data.period))
    membership = _component(
        data.joined.value - data.left.value + 2 * days, 4 * days
    )
    training = _component(
        data.courses_started.value + data.courses_completed.value, 6 * days
    )
    operations = _component(
        data.missions_started.value + data.events_started.value, 4 * days
    )
    engagement = _component(data.credits_earners, 150)
    treasury = _component(
        (data.donations_total or 0) / 1_000_000
        + (1 if (data.balance_change or 0) >= 0 else 0),
        days + 1,
    )
    score = 100 * (
        membership + training + operations + engagement + treasury
    ) / 5
    return int(round(score))


# -- outlook (the prognoses) -----------------------------------------------

async def _outlook(db: Database, data: OverviewData, now) -> list[str]:
    """Forward-looking lines from real stored history. Only lines with a
    real basis are emitted."""
    lines: list[str] = []
    members = MembersRepo(db)
    treasury = TreasuryRepo(db)

    lookback_start = (
        now - dt.timedelta(days=FORECAST_LOOKBACK_DAYS)
    ).isoformat(timespec="seconds")
    now_iso = now.isoformat(timespec="seconds")

    # Member trajectory from the last two weeks of roster events.
    events = await members.event_counts(lookback_start, now_iso)
    joined = events.get("joined", 0)
    left = events.get("left", 0)
    if joined or left:
        net = joined - left
        per_month = round(net * 30 / FORECAST_LOOKBACK_DAYS)
        direction = "grow" if per_month >= 0 else "shrink"
        lines.append(
            f"Members: on the last {FORECAST_LOOKBACK_DAYS} days' pace "
            f"(+{joined}/−{left}) the roster would {direction} by "
            f"~{abs(per_month)} in the next 30 days "
            f"(→ ~{data.active_members + per_month})."
        )

    # Treasury trajectory from the balance ledger.
    balance_then = await treasury.balance_at(lookback_start)
    balance_now = await treasury.balance_at(now_iso)
    if balance_then is not None and balance_now is not None:
        drift = balance_now["total_funds"] - balance_then["total_funds"]
        if drift or balance_now["total_funds"]:
            per_day = drift / FORECAST_LOOKBACK_DAYS
            projected = int(balance_now["total_funds"] + per_day * 30)
            line = (
                f"Treasury: {drift:+,} credits over the last "
                f"{FORECAST_LOOKBACK_DAYS} days — at that pace "
                f"~{projected:,} in 30 days."
            )
            if per_day < 0 and balance_now["total_funds"] > 0:
                days_left = int(balance_now["total_funds"] / -per_day)
                line += f" At this burn the funds last ~{days_left} days."
            lines.append(line)

    # Month-to-date income pace (only meaningful mid-month).
    ny_now = now.astimezone(NY)
    _, month_key = ny_period_keys(now)
    month_rows = await treasury.latest_snapshot("monthly", month_key)
    days_gone = ny_now.day
    if month_rows and 2 <= days_gone < 28:
        total = sum(r["amount"] for r in month_rows)
        import calendar

        days_in_month = calendar.monthrange(ny_now.year, ny_now.month)[1]
        projected = int(total / days_gone * days_in_month)
        lines.append(
            f"Donations: {total:,} credits in the first {days_gone} days "
            f"of the month — on pace for ~{projected:,}."
        )

    return lines


# -- fun facts -------------------------------------------------------------

def _fun_facts(data: OverviewData, now) -> list[str]:
    """A rotating handful of true, engaging one-liners. Deterministic per
    day (seeded rotation), so a rebuild of the same report shows the same
    facts."""
    facts: list[str] = []
    if data.top_earners:
        name, amount = data.top_earners[0]
        facts.append(f"🏅 Top earner: **{name}** with {amount:,} credits.")
    if data.top_donor and data.donations_total:
        name, amount = data.top_donor
        share = 100 * amount / data.donations_total if data.donations_total else 0
        facts.append(
            f"💝 **{name}** donated {amount:,} credits"
            + (f" — {share:.0f}% of everything given." if share >= 10 else ".")
        )
    if data.courses_started.value and data.courses_completed.value:
        facts.append(
            f"🎓 {data.courses_completed.value} course(s) finished while "
            f"{data.courses_started.value} new one(s) started — the academy "
            "never sleeps."
        )
    total_ops = data.missions_started.value + data.events_started.value
    if total_ops:
        days = max(1.0, _period_days(data.period))
        if total_ops / days >= 1:
            facts.append(
                f"🚨 {total_ops} large mission(s)/event(s) started — about "
                f"{total_ops / days:.1f} per day."
            )
        else:
            facts.append(f"🚨 {total_ops} large mission(s)/event(s) started.")
    if data.credits_earners:
        facts.append(
            f"📈 {data.credits_earners} member(s) earned credits this "
            f"period — {data.credits_total:,} combined."
        )
    if data.top_spender and data.spent_total:
        name, spent = data.top_spender
        facts.append(f"🏗️ Biggest builder: **{name}** ({spent:,} credits).")
    if len(facts) <= 4:
        return facts
    # Deterministic rotation: same facts for the same report on the same
    # day, different selection day to day.
    offset = now.timetuple().tm_yday % len(facts)
    rotated = facts[offset:] + facts[:offset]
    return rotated[:4]
