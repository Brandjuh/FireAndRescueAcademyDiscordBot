"""The composite overview reports — the old bot's daily/monthly member
and admin reports, rebuilt on this bot's registry.

Two registered reports render the same gathered data two ways:

* ``overview-member`` — the public digest: roster, credits, treasury,
  game activity, activity score, fun facts and the outlook;
* ``overview-admin`` — all of that plus sanctions, verifications, admin
  activity and an action-items list.

Both run on any bounded period (``yesterday`` is the finished game day,
``prev-month`` the finished month), so the scheduled daily/monthly posts
and an ad-hoc ``!fra report overview-member week`` share one code path.
"""

from __future__ import annotations

from ..db.database import Database
from .analytics import OverviewData, gather_overview
from .registry import Report, ReportRegistry, ReportResult
from .period import Period

_KIND_ICONS = {"training": "🎓", "building": "🏗️", "event": "🎉"}


def _int(value: int | None) -> str:
    return "?" if value is None else f"{value:,}"


def _members_section(result: ReportResult, data: OverviewData) -> None:
    lines = [f"Active members: **{data.active_members:,}**"]
    if data.joined.value or data.left.value:
        lines.append(
            f"Joined: {data.joined.value}{data.joined.trend()} · "
            f"Left: {data.left.value}{data.left.trend()}"
        )
        lines.append(f"Net: {data.joined.value - data.left.value:+}")
    else:
        lines.append("No joins or leaves recorded.")
    result.add("👥 Members", "\n".join(lines))


def _credits_section(result: ReportResult, data: OverviewData) -> None:
    if not data.credits_earners:
        return
    lines = [
        f"**{data.credits_total:,}** credits earned by "
        f"{data.credits_earners} member(s)."
    ]
    medals = ("🥇", "🥈", "🥉")
    for medal, (name, amount) in zip(medals, data.top_earners):
        lines.append(f"{medal} {name} — {amount:,}")
    result.add("📈 Credits earned", "\n".join(lines))


def _treasury_section(result: ReportResult, data: OverviewData) -> None:
    lines = []
    if data.balance is not None:
        line = f"Funds: **{_int(data.balance)}** credits"
        if data.balance_change is not None:
            line += f" ({data.balance_change:+,} this period)"
        lines.append(line)
    if data.donations_total is not None:
        donors = data.donations_contributors or 0
        lines.append(
            f"Donated: {data.donations_total:,} credits by {donors} member(s)"
        )
    if data.spent_total:
        line = f"Spent: {data.spent_total:,} credits ({data.spent_rows} expense(s))"
        if data.top_spender:
            line += f" — most by {data.top_spender[0]}"
        lines.append(line)
    if lines:
        result.add("💰 Treasury", "\n".join(lines))


def _activity_section(result: ReportResult, data: OverviewData) -> None:
    lines = [
        "Courses started: "
        f"{data.courses_started.value}{data.courses_started.trend()}",
        "Courses completed: "
        f"{data.courses_completed.value}{data.courses_completed.trend()}",
        "Large missions started: "
        f"{data.missions_started.value}{data.missions_started.trend()}",
        "Events started: "
        f"{data.events_started.value}{data.events_started.trend()}",
    ]
    if data.buildings_constructed.value or data.expansions_finished.value:
        lines.append(
            f"Buildings constructed: {data.buildings_constructed.value} · "
            f"Expansions finished: {data.expansions_finished.value}"
        )
    result.add("🎯 Game activity", "\n".join(lines))


def _pipeline_section(result: ReportResult, data: OverviewData) -> None:
    lines = []
    for kind, statuses in sorted(data.requests_by_kind.items()):
        icon = _KIND_ICONS.get(kind, "🤖")
        parts = ", ".join(f"{s}: {n}" for s, n in sorted(statuses.items()))
        lines.append(f"{icon} {kind.capitalize()}: {parts}")
    if data.missions_by_status:
        parts = ", ".join(
            f"{s}: {n}" for s, n in sorted(data.missions_by_status.items())
        )
        lines.append(f"🚨 Mission requests: {parts}")
    if lines:
        result.add("🤖 Bot requests", "\n".join(lines))


def _score_section(result: ReportResult, data: OverviewData) -> None:
    line = f"**{data.activity_score}/100**"
    if data.activity_score_previous is not None:
        delta = data.activity_score - data.activity_score_previous
        if delta:
            line += f" ({'↑' if delta > 0 else '↓'} {delta:+} vs previous)"
        else:
            line += " (= previous)"
    result.add("⚡ Activity score", line, inline=True)


def _outlook_section(result: ReportResult, data: OverviewData) -> None:
    if data.outlook:
        result.add("🔮 Outlook", "\n".join(f"• {line}" for line in data.outlook))


def _fun_facts_section(result: ReportResult, data: OverviewData) -> None:
    if data.fun_facts:
        result.add("✨ Fun facts", "\n".join(data.fun_facts))


def _sanctions_section(result: ReportResult, data: OverviewData) -> None:
    lines = [
        f"Issued: {data.sanctions_issued.value}{data.sanctions_issued.trend()}"
    ]
    if data.sanctions_by_type:
        lines += [
            f"• {stype}: {n}"
            for stype, n in sorted(
                data.sanctions_by_type.items(), key=lambda kv: -kv[1]
            )[:6]
        ]
    if data.tax_warnings_issued:
        lines.append(f"Of which tax warnings: {data.tax_warnings_issued}")
    register = data.sanctions_register
    if register:
        lines.append(
            f"Register: {register.get('active', 0)} active · "
            f"{register.get('unverified', 0)} awaiting review"
        )
    result.add("⚖️ Sanctions", "\n".join(lines))


def _admin_activity_section(result: ReportResult, data: OverviewData) -> None:
    lines = [
        "Verifications approved: "
        f"{data.verifications_approved.value}"
        f"{data.verifications_approved.trend()}"
    ]
    if data.most_active_admins:
        busiest = ", ".join(
            f"{name} ({n})" for name, n in data.most_active_admins
        )
        lines.append(f"Most active admins: {busiest}")
    result.add("🧑‍💼 Admin activity", "\n".join(lines))


def _action_items_section(result: ReportResult, data: OverviewData) -> None:
    items = []
    if data.open_applications:
        items.append(f"• {data.open_applications} open application(s)")
    if data.sanctions_unverified:
        items.append(
            f"• {data.sanctions_unverified} sanction(s) awaiting review"
        )
    if data.open_requests:
        items.append(f"• {data.open_requests} open board/panel request(s)")
    if data.open_missions:
        items.append(f"• {data.open_missions} queued mission request(s)")
    if data.low_contributors:
        items.append(
            f"• {data.low_contributors} member(s) below the 5% donation "
            "minimum"
        )
    result.add(
        "📋 Action items", "\n".join(items) if items else "Nothing pending. 🎉"
    )


def register_overview_reports(registry: ReportRegistry, db: Database) -> None:
    async def overview_member(period: Period) -> ReportResult:
        result = ReportResult(
            title=f"📊 Alliance overview — {period.label}", colour=0x3498DB
        )
        if period.start is None:
            result.description = "Pick a bounded period (yesterday/week/month)."
            return result
        data = await gather_overview(db, period, admin=False)
        _members_section(result, data)
        _credits_section(result, data)
        _treasury_section(result, data)
        _activity_section(result, data)
        _score_section(result, data)
        _fun_facts_section(result, data)
        _outlook_section(result, data)
        return result

    async def overview_admin(period: Period) -> ReportResult:
        result = ReportResult(
            title=f"🛡️ Admin overview — {period.label}", colour=0x2C3E50
        )
        if period.start is None:
            result.description = "Pick a bounded period (yesterday/week/month)."
            return result
        data = await gather_overview(db, period, admin=True)
        _members_section(result, data)
        _credits_section(result, data)
        _treasury_section(result, data)
        _activity_section(result, data)
        _pipeline_section(result, data)
        _sanctions_section(result, data)
        _admin_activity_section(result, data)
        _score_section(result, data)
        _outlook_section(result, data)
        _action_items_section(result, data)
        return result

    registry.register(Report(
        "overview-member",
        "Full member digest: roster, credits, treasury, activity, outlook",
        overview_member,
        periods=("yesterday", "today", "week", "month", "prev-month"),
    ))
    registry.register(Report(
        "overview-admin",
        "Full admin digest: everything + sanctions, admin activity, action items",
        overview_admin,
        periods=("yesterday", "today", "week", "month", "prev-month"),
    ))
