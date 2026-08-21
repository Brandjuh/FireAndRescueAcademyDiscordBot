"""Built-in reports, registered from the bot at startup.

Each builder queries the local database only (no MissionChief calls) and
returns a :class:`ReportResult`. Adding a report is: write a builder,
register it. New modules should register their own here (or from their
own package) so everything stays reportable.
"""

from __future__ import annotations

import datetime as dt

from ..db.database import Database
from ..db.repos import (
    ApplicationsRepo,
    AutomationRepo,
    LogsRepo,
    MembersRepo,
    MissionsRepo,
    TreasuryRepo,
    ny_period_keys,
)
from .period import NY, Period
from .registry import Report, ReportRegistry, ReportResult


def _prev_ny_keys(now_utc: dt.datetime | None = None) -> tuple[str, str]:
    """(previous NY game day, previous NY month) as period keys."""
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    ny_today = now_utc.astimezone(NY).date()
    prev_day = ny_today - dt.timedelta(days=1)
    prev_month_last = ny_today.replace(day=1) - dt.timedelta(days=1)
    return prev_day.strftime("%Y-%m-%d"), prev_month_last.strftime("%Y-%m")

#: How long after the NY reset a bare "today"/"month" still means the
#: FINISHED period. These builders cannot see the schedule, and
#: ``reports.daily_delay_minutes`` is capped at 120 by its Setting — the
#: old ``hour == 0`` test therefore broke for any delay past 60 minutes,
#: rendering the new day's single early bird as the day's top contributors.
RESET_GRACE_MINUTES = 120


def _just_after_reset(ny_now: dt.datetime) -> bool:
    return ny_now.hour * 60 + ny_now.minute <= RESET_GRACE_MINUTES


_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _top10_lines(rows) -> str:
    lines = []
    for row in rows[:10]:
        medal = _MEDALS.get(row["rank"], f"`#{row['rank']:>2}`")
        lines.append(f"{medal} **{row['username']}** — {row['amount']:,} credits")
    return "\n".join(lines) if lines else "No contributions recorded."

_MEMBER_EVENT_LABELS = {
    "joined": "Joined",
    "left": "Left",
    "role_changed": "Role changes",
    "contribution_changed": "Contribution changes",
    "name_changed": "Name changes",
}


def register_builtin_reports(registry: ReportRegistry, db: Database) -> None:
    from .overview import register_overview_reports

    register_overview_reports(registry, db)
    members = MembersRepo(db)
    apps = ApplicationsRepo(db)
    logs = LogsRepo(db)
    treasury = TreasuryRepo(db)
    automation = AutomationRepo(db)
    missions = MissionsRepo(db)

    async def members_report(period: Period) -> ReportResult:
        result = ReportResult(title=f"👥 Members — {period.label}")
        result.add("Active members", f"{await members.active_count():,}", inline=True)
        result.add("Open applications", str(await apps.open_count()), inline=True)
        counts = await members.event_counts(period.start_iso, period.end_iso)
        lines = [
            f"• {_MEMBER_EVENT_LABELS.get(k, k)}: {counts[k]}"
            for k in _MEMBER_EVENT_LABELS
            if counts.get(k)
        ]
        result.add("Changes this period", "\n".join(lines) if lines else "None", inline=False)
        return result

    async def credits_report(period: Period) -> ReportResult:
        result = ReportResult(title=f"📈 Earned credits — {period.label}")
        if period.start_iso is None:
            result.description = "Pick a bounded period (today/week/month)."
            return result
        rows = await members.credit_deltas(period.start_iso, period.end_iso)
        if not rows:
            result.description = "No credit gains recorded in this period."
            return result
        top = "\n".join(
            f"`#{i:>2}` **{r['name']}** — {r['delta']:,}"
            for i, r in enumerate(rows[:10], start=1)
        )
        result.add("Top earners", top, inline=False)
        return result

    async def treasury_report(period: Period) -> ReportResult:
        result = ReportResult(title=f"💰 Treasury — {period.label}")
        balance = await treasury.latest_balance()
        if balance is not None:
            result.add(
                "Alliance funds", f"{balance['total_funds']:,} credits", inline=True
            )
        summary = await treasury.expense_summary(period.start_iso, period.end_iso)
        result.add("Expenses", f"{summary['count']:,} rows", inline=True)
        result.add("Total spent", f"{summary['total']:,} credits", inline=True)
        if summary["top"]:
            spenders = "\n".join(
                f"• {name}: {spent:,}" for name, spent in summary["top"]
            )
            result.add("Top spenders", spenders, inline=False)
        return result

    async def logs_report(period: Period) -> ReportResult:
        result = ReportResult(title=f"📜 Alliance activity — {period.label}")
        counts = await logs.action_counts(period.start_iso, period.end_iso)
        if not counts:
            result.description = "No alliance-log activity in this period."
            return result
        lines = [
            f"• {key.replace('_', ' ')}: {n}"
            for key, n in list(counts.items())[:15]
        ]
        result.add("By action", "\n".join(lines), inline=False)
        return result

    async def sanctions_report(period: Period) -> ReportResult:
        from ..db.repos import SanctionsRepo
        from ..services.sanction_rules import effective_status

        repo = SanctionsRepo(db)
        result = ReportResult(title=f"⚖️ Sanctions — {period.label}")
        rows = await repo.issued_between(period.start_iso, period.end_iso)
        summary = await repo.status_summary()
        standing = (
            f"Register: {summary.get('active', 0)} active · "
            f"{summary.get('unverified', 0)} awaiting review · "
            f"{sum(summary.values())} total"
        )
        if not rows:
            result.description = (
                f"No sanctions recorded in this period.\n{standing}"
            )
            return result
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_member: dict[str, int] = {}
        for row in rows:
            by_type[row["sanction_type"]] = by_type.get(row["sanction_type"], 0) + 1
            status = effective_status(row)
            by_status[status] = by_status.get(status, 0) + 1
            name = row["mc_username"] or "?"
            by_member[name] = by_member.get(name, 0) + 1
        result.description = f"**{len(rows)}** recorded.\n{standing}"
        result.add(
            "By type",
            "\n".join(
                f"• {stype}: {n}"
                for stype, n in sorted(by_type.items(), key=lambda kv: -kv[1])
            )[:1024],
            inline=False,
        )
        result.add(
            "Outcome",
            ", ".join(
                f"{status}: {n}" for status, n in sorted(by_status.items())
            )[:1024],
            inline=False,
        )
        top = sorted(by_member.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        if top:
            result.add(
                "Most sanctioned",
                "\n".join(f"• {name}: {n}" for name, n in top)[:1024],
                inline=False,
            )
        return result

    async def automation_report(period: Period) -> ReportResult:
        result = ReportResult(title=f"🤖 Board automation — {period.label}")
        rows = await automation.activity_counts(period.start_iso, period.end_iso)
        if not rows:
            result.description = "No board requests in this period."
            result.add("Open requests", str(await automation.open_count()), inline=True)
            return result
        by_kind: dict[str, list[str]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(f"{row['status']}: {row['n']}")
        for kind, parts in by_kind.items():
            result.add(kind.capitalize(), ", ".join(parts), inline=False)
        result.add("Currently open", str(await automation.open_count()), inline=True)
        return result

    async def missions_report(period: Period) -> ReportResult:
        result = ReportResult(title=f"🚨 Custom missions — {period.label}")
        counts = await missions.status_counts(period.start_iso, period.end_iso)
        if not counts:
            result.description = "No custom missions requested in this period."
            result.add("Currently open", str(await missions.open_count()), inline=True)
            return result
        lines = [f"• {status}: {n}" for status, n in counts.items()]
        result.add("By status", "\n".join(lines), inline=False)
        result.add("Currently open", str(await missions.open_count()), inline=True)
        return result

    async def income_daily(period: Period) -> ReportResult:
        # "yesterday" reports the FINISHED game day (the 23:55-NY pre-reset
        # capture); "today" reports the running day. The morning schedule
        # fires minutes after the NY midnight reset — in that first hour a
        # "today" list is empty or a single early bird, so the finished
        # day's FINAL standings are what the reader wants (an empty-only
        # fallback showed a one-contributor "top 10" when anyone donated
        # within minutes of the reset). Outside that hour the empty-list
        # fallback still covers a missing fresh snapshot.
        ny_now = dt.datetime.now(dt.timezone.utc).astimezone(NY)
        day_key, _ = ny_period_keys()
        if period.name == "yesterday" or _just_after_reset(ny_now):
            day_key, _ = _prev_ny_keys()
        rows = await treasury.latest_snapshot("daily", day_key)
        if not rows and period.name != "yesterday":
            prev_day, _ = _prev_ny_keys()
            rows = await treasury.latest_snapshot("daily", prev_day)
            if rows:
                day_key = prev_day
        return ReportResult(
            title=f"💰 Daily top contributors ({day_key})",
            description=_top10_lines(rows),
            colour=0xF1C40F,  # gold
        )

    async def income_monthly(period: Period) -> ReportResult:
        # Same shape as the daily report: "prev-month" is explicit, and the
        # first hour after the month's NY rollover shows the finished
        # month's final standings (the monthly schedule fires exactly
        # then); an empty fresh month still falls back afterwards.
        ny_now = dt.datetime.now(dt.timezone.utc).astimezone(NY)
        _, month_key = ny_period_keys()
        if period.name == "prev-month" or (
            ny_now.day == 1 and _just_after_reset(ny_now)
        ):
            _, month_key = _prev_ny_keys()
        rows = await treasury.latest_snapshot("monthly", month_key)
        if not rows and period.name != "prev-month":
            _, prev_month = _prev_ny_keys()
            rows = await treasury.latest_snapshot("monthly", prev_month)
            if rows:
                month_key = prev_month
        return ReportResult(
            title=f"🏆 Monthly top contributors ({month_key})",
            description=_top10_lines(rows),
            colour=0xF1C40F,  # gold
        )

    registry.register(Report(
        "income-daily", "Daily income top-10 (today, or yesterday's final)", income_daily,
        periods=("today", "yesterday"),
    ))
    registry.register(Report(
        "income-monthly", "Monthly income top-10 (this month, or last month's final)", income_monthly,
        periods=("month", "prev-month"),
    ))
    registry.register(Report(
        "members", "Roster size, applications and member changes", members_report,
        periods=("today", "week", "month", "prev-month"),
    ))
    registry.register(Report(
        "credits", "Top earned-credit gainers (top earners)", credits_report,
        periods=("today", "yesterday", "week", "month", "prev-month", "year", "prev-year"),
    ))
    registry.register(Report(
        "treasury", "Alliance funds and expenses", treasury_report,
        periods=("today", "yesterday", "week", "month", "prev-month",
                 "year", "prev-year", "all"),
    ))
    registry.register(Report(
        "logs", "Alliance-log activity by action", logs_report,
        periods=("today", "week", "month"),
    ))
    registry.register(Report(
        "sanctions", "Sanctions issued, outcomes and repeat offenders",
        sanctions_report,
        periods=("today", "week", "month", "prev-month"),
    ))
    registry.register(Report(
        "automation", "Board request activity and outcomes", automation_report,
        periods=("today", "week", "month", "all"),
    ))
    registry.register(Report(
        "missions", "Custom scheduled-mission requests and outcomes", missions_report,
        periods=("today", "week", "month", "all"),
    ))
