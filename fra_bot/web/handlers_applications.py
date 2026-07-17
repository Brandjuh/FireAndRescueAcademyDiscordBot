"""Web console: alliance membership applications.

Read view over the ``applications`` table the sync job maintains, plus
Accept/Deny actions that call ``bot.applications_sync`` — the exact
service the Discord announcement buttons use (paced client with retries
and re-login, then ``mark_resolved``). That service is gate-free by
design, so the ``automation.dry_run`` gate is enforced here the same
way ``NotificationsCog.handle_application_action`` enforces it before
calling the service.

The applications table records WHEN an application closed but not HOW
(a row simply vanishes from /verband/bewerbungen), so history outcomes
come from our own member-action log where we decided ourselves, from
the members table when the applicant is now an active member, and fall
back to an honest "expired / decided in-game" label otherwise.
"""

from __future__ import annotations

import logging
import re

from aiohttp import web

from ..db.repos import ApplicationsRepo
from ..mc.errors import MissionChiefError
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page

log = logging.getLogger(__name__)

_HISTORY_LIMIT = 50

# earned_credits only exists for applicants who are (or were) members —
# the applications page itself lists no credits, so this is best-effort.
_SELECT = (
    "SELECT a.application_id, a.applicant_name, a.mc_user_id, "
    "a.first_seen_at, a.last_seen_at, a.resolved_at, a.posted_at, "
    "m.earned_credits, m.is_active AS member_active "
    "FROM applications a LEFT JOIN members m ON m.mc_user_id = a.mc_user_id "
)


async def _rows(bot, *, resolved: bool) -> list:
    if resolved:
        sql = _SELECT + (
            "WHERE a.resolved_at IS NOT NULL "
            "ORDER BY a.resolved_at DESC LIMIT ?"
        )
        params: tuple = (_HISTORY_LIMIT,)
    else:
        sql = _SELECT + (
            "WHERE a.resolved_at IS NULL ORDER BY a.first_seen_at ASC"
        )
        params = ()
    async with bot.db.conn.execute(sql, params) as cur:
        return list(await cur.fetchall())


async def _console_outcomes(bot) -> dict[int, str]:
    """Application id → 'accepted'/'denied' for decisions made through
    THIS console (parsed back out of the member-action log — the only
    place a decision is recorded; the game never reports how a vanished
    application row was decided)."""
    outcomes: dict[int, str] = {}
    async with bot.db.conn.execute(
        "SELECT action, detail FROM member_actions "
        "WHERE action IN ('application_accepted', 'application_denied')"
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        match = re.search(r"application #(\d+)", row["detail"] or "")
        if match:
            outcomes[int(match.group(1))] = (
                "accepted" if row["action"] == "application_accepted"
                else "denied"
            )
    return outcomes


def _name_cell(row) -> str:
    name = esc(row["applicant_name"])
    if row["mc_user_id"]:
        return (
            f"<a href='https://www.missionchief.com/profile/"
            f"{int(row['mc_user_id'])}' target='_blank'>{name}</a>"
        )
    return name


def _credits(row) -> str:
    if row["earned_credits"] is None:
        return "—"
    return f"{row['earned_credits']:,}"


def _outcome_badge(row, outcomes: dict[int, str]) -> str:
    outcome = outcomes.get(row["application_id"])
    if outcome == "accepted":
        return badge("accepted (console)", "ok")
    if outcome == "denied":
        return badge("denied (console)", "off")
    if row["member_active"]:
        return badge("accepted — now a member", "ok")
    return badge("expired / decided in-game")


async def applications_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    pending = await _rows(bot, resolved=False)
    history = await _rows(bot, resolved=True)
    outcomes = await _console_outcomes(bot)

    auto = bool(bot.cfg.automation.applications.auto_accept)
    dry = bool(bot.cfg.automation.dry_run)
    status_bits = [
        badge("auto-accept on", "ok") if auto else badge("auto-accept off"),
    ]
    if dry:
        status_bits.append(badge("dry-run — game actions are simulated", "off"))

    pending_lines = "".join(
        "<tr>"
        f"<td>{_name_cell(row)}</td>"
        f"<td>{_credits(row)}</td>"
        f"<td>{esc(str(row['first_seen_at'])[:16])}</td>"
        f"<td>{esc(str(row['last_seen_at'])[:16])}</td>"
        f"<td>{badge('announced') if row['posted_at'] else badge('announce queued', 'off')}</td>"
        "<td>"
        f"<form class='inline' method='post' "
        f"action='/applications/{int(row['application_id'])}/accept'>"
        "<button class='small'>Accept</button></form> "
        f"<form class='inline' method='post' "
        f"action='/applications/{int(row['application_id'])}/deny'>"
        "<button class='small ghost'>Deny</button></form>"
        "</td></tr>"
        for row in pending
    ) or "<tr><td colspan='6' class='muted'>No pending applications.</td></tr>"

    history_lines = "".join(
        "<tr>"
        f"<td>{_name_cell(row)}</td>"
        f"<td>{_credits(row)}</td>"
        f"<td>{esc(str(row['first_seen_at'])[:16])}</td>"
        f"<td>{esc(str(row['resolved_at'])[:16])}</td>"
        f"<td>{_outcome_badge(row, outcomes)}</td>"
        "</tr>"
        for row in history
    ) or "<tr><td colspan='5' class='muted'>No handled applications yet.</td></tr>"

    body = (
        f"<p>{' '.join(status_bits)} · {len(pending)} pending</p>"
        "<div class='panel'><h2>Pending applications</h2>"
        "<table><tr><th>Applicant</th><th>Credits</th><th>First seen</th>"
        "<th>Last seen</th><th>Status</th><th>Decision</th></tr>"
        f"{pending_lines}</table>"
        "<p class='muted'>Credits are only known for applicants who are or "
        "were alliance members. Accept/Deny performs the same in-game "
        "action as the Discord buttons.</p></div>"
        f"<div class='panel'><h2>Recent history (last {_HISTORY_LIMIT})</h2>"
        "<table><tr><th>Applicant</th><th>Credits</th><th>First seen</th>"
        "<th>Resolved</th><th>Outcome</th></tr>"
        f"{history_lines}</table></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Applications", body, active="/applications", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


#: Serializes decisions: two rapid clicks (or accept vs deny) must not
#: both pass the pending check and fire the game action twice.
_decide_lock: "asyncio.Lock | None" = None


async def _decide(request: web.Request, action: str) -> web.Response:
    import asyncio

    global _decide_lock
    if _decide_lock is None:
        _decide_lock = asyncio.Lock()
    bot = _bot(request)
    application_id = int(request.match_info["app_id"])
    async with _decide_lock:
        # Check-then-act stays inside the lock: the row is re-read here so
        # the second of two racing POSTs sees resolved_at and bails.
        row = await ApplicationsRepo(bot.db).get(application_id)
        if row is None:
            _redirect("/applications",
                      err=f"Application #{application_id} is unknown.")
        if row["resolved_at"]:
            _redirect("/applications",
                      err=f"Application #{application_id} was already handled.")
        name = row["applicant_name"]
        # Same gate as NotificationsCog.handle_application_action: the
        # service itself never checks dry_run, so the caller must —
        # dry-run reports the would-be action and touches nothing.
        if bot.cfg.automation.dry_run:
            _redirect(
                "/applications",
                ok=f"[dry-run] would {action} {name} — no game action taken.",
            )
        service = getattr(bot, "applications_sync", None)
        if service is None:
            _redirect("/applications",
                      err="Applications service is unavailable.")
        try:
            if action == "accept":
                await service.accept(application_id)
            else:
                await service.deny(application_id)
        except MissionChiefError as exc:
            _redirect("/applications", err=f"Could not {action} {name}: {exc}")
    verb = "accepted" if action == "accept" else "denied"
    await bot.log_member_action(
        action=f"application_{verb}",
        detail=f"application #{application_id}: {name} (via {WEB_ACTOR})",
        mc_user_id=row["mc_user_id"], actor_name=name,
    )
    # The Discord button flow announces who decided in the admin log —
    # console decisions must be equally visible there.
    notify = getattr(bot, "notify_admin", None)
    if notify is not None:
        try:
            await notify(
                f"📋 Application #{application_id} ({name}) {verb} "
                f"via {WEB_ACTOR}."
            )
        except Exception:  # noqa: BLE001 — announcements are best-effort
            log.warning("applications: admin notice failed", exc_info=True)
    _redirect("/applications", ok=f"{verb.capitalize()} {name}.")


async def post_accept(request: web.Request) -> web.Response:
    return await _decide(request, "accept")


async def post_deny(request: web.Request) -> web.Response:
    return await _decide(request, "deny")


ROUTES = [
    web.get("/applications", applications_page),
    web.post("/applications/{app_id:\\d+}/accept", post_accept),
    web.post("/applications/{app_id:\\d+}/deny", post_deny),
]
NAV_ENTRY = ("/applications", "Applications")
