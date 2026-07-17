"""Web console: operational automation status + the academy build queue.

The read side mirrors ``!fra status`` / ``!fra automation`` (cogs/admin.py):
dry-run mode, circuit breaker, MC request backlog, per-automation switches,
board-reply failure records, recent job runs and the academy queue with its
funds gate. Everything renders from the bot's config/repos/state — this
module NEVER talks to MissionChief itself.

The single write action queues an academy build through the SAME path as
the Discord panel (``cogs/academy.py``): ``AcademyService.enqueue`` writes
the ``automation_requests`` row and the immediate kick runs under the
shared ``academy-builds`` job lock, so the service's own dry-run switch,
funds floor and pacing apply unchanged. On a console without the live
service wiring (offline tests) the kick is skipped and the committed row
simply waits for the scheduled queue poller.

Live MC gauges (``bot.pacer.circuit_open``, ``bot.mc.pacer_backlog``) are
read via ``getattr`` with a harmless fallback — the exact attributes the
admin cog reads — so the page still renders when they are absent.

Settings changes are deliberately NOT offered here: the /settings page
already exposes the full ``!fra set`` registry.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from ..db.repos import AutomationRepo, RunsRepo, StateRepo, TreasuryRepo
from ..services.academy import ACADEMIES, KIND as ACADEMY_KIND
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page, tile

log = logging.getLogger(__name__)

_QUEUE_LIMIT = 30
_RUNS_LIMIT = 10
#: The kinds the admin cog surfaces reply failures for (each board service
#: records under ``board_reply_last_failure:<kind>``).
_REPLY_KINDS = ("training", "building", "event")
_RUN_BADGE = {"success": "ok", "failed": "off", "partial": "dim"}
_STATUS_BADGE = {"done": "ok", "failed": "off"}


def _on_off(flag: bool) -> str:
    return badge("on", "ok") if flag else badge("off", "off")


def _kick_academy_build(bot, request_id: int) -> None:
    """The Discord panel's immediate execution: run the fresh request in the
    background under the shared ``academy-builds`` job lock so it can never
    overlap the retry poller. The service path applies dry-run, the funds
    floor and the pacer itself; without the live wiring the row is left for
    the scheduled poller."""
    service = getattr(bot, "academy", None)
    job_lock = getattr(bot, "job_lock", None)
    if service is None or job_lock is None:
        return

    async def _run() -> None:
        try:
            async with job_lock("academy-builds"):
                await service.run_one(request_id)
        except Exception:  # noqa: BLE001 — the queue poller retries the row
            log.exception("immediate academy build failed")

    asyncio.get_running_loop().create_task(_run())


# ---------------------------------------------------------------------------
# Status page
# ---------------------------------------------------------------------------

def _mode_banner(dry_run: bool) -> str:
    if dry_run:
        return (
            "<div class='flash' style='font-size:17px'>"
            "<strong>DRY-RUN is ON</strong> — MissionChief actions are "
            "simulated; nothing is started, built or spent.</div>"
        )
    return (
        "<div class='flash err' style='font-size:17px'>"
        "<strong>LIVE MODE — dry-run is OFF.</strong> The bot performs REAL "
        "MissionChief actions (trainings, buildings, missions, builds)."
        "</div>"
    )


def _switches_table(auto) -> str:
    rows = (
        ("Board replies", auto.reply_to_board,
         "posts request feedback on the alliance boards"),
        ("Trainings", auto.training.enabled,
         f"thread {auto.training.thread_id}"),
        ("Buildings", auto.building.enabled,
         f"thread {auto.building.thread_id}"),
        ("Events", auto.events.enabled,
         f"thread {auto.events.thread_id}"),
        ("Missions", auto.mission.enabled,
         "board " + ("on" if auto.mission.board_enabled else "off")
         + f" · thread {auto.mission.thread_id}"),
        ("Academy queue", auto.academy.enabled,
         "autoscale " + ("on" if auto.academy.autoscale else "off")
         + f" · funds floor {auto.academy.min_alliance_funds:,}"),
        ("Chat bridge", auto.chat.enabled,
         f"every {auto.chat.interval_seconds}s"),
        ("Application auto-accept", auto.applications.auto_accept,
         "accepts new alliance applications in-game"),
    )
    lines = "".join(
        f"<tr><td>{esc(name)}</td><td>{_on_off(flag)}</td>"
        f"<td class='muted'>{esc(detail)}</td></tr>"
        for name, flag, detail in rows
    )
    return (
        "<table><tr><th>Automation</th><th>State</th><th>Details</th></tr>"
        f"{lines}</table>"
        "<p class='muted'>Switches are changed on the "
        "<a href='/settings'>Settings</a> page (the !fra set registry).</p>"
    )


async def _reply_failures_html(state: StateRepo, reply_to_board: bool) -> str:
    lines = []
    for kind in _REPLY_KINDS:
        raw = await state.get(f"board_reply_last_failure:{kind}")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        detail = str(data.get("detail", "?"))
        at = str(data.get("at", ""))[:16]
        lines.append(
            f"<li>{badge(kind, 'off')} {esc(detail)} "
            f"<span class='muted'>{esc(at)}</span></li>"
        )
    out = ""
    if not reply_to_board:
        out += (
            "<p>" + badge("reply_to_board OFF", "off")
            + " the bot never posts board notices — members get no feedback "
            "on their board requests.</p>"
        )
    if lines:
        out += (
            "<p>Last failure per board (cleared by the next successful "
            f"reply):</p><ul class='timeline'>{''.join(lines)}</ul>"
        )
    elif reply_to_board:
        out += "<p class='muted'>No board reply failures recorded.</p>"
    return out


def _runs_table(runs) -> str:
    lines = "".join(
        "<tr>"
        f"<td><code>{esc(run['scraper'])}</code></td>"
        f"<td>{esc(str(run['started_at'])[:16])}</td>"
        f"<td>{badge(run['status'], _RUN_BADGE.get(run['status'], 'dim'))}</td>"
        f"<td>{run['rows_new']}</td>"
        f"<td class='muted'>{esc((run['message'] or '')[:80])}</td>"
        "</tr>"
        for run in runs
    ) or "<tr><td colspan='5' class='muted'>No runs recorded yet.</td></tr>"
    return (
        "<table><tr><th>Job</th><th>Started</th><th>Status</th>"
        f"<th>New</th><th>Message</th></tr>{lines}</table>"
    )


def _academy_rows(rows) -> str:
    lines = []
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            data = {}
        spec = ACADEMIES.get(str(data.get("academy") or ""))
        label = spec["label"] if spec else str(data.get("academy") or "?")
        status_html = badge(
            row["status"], _STATUS_BADGE.get(row["status"], "dim")
        )
        if row["status_detail"]:
            status_html += (
                f"<br><span class='muted'>{esc(str(row['status_detail'])[:140])}"
                "</span>"
            )
        attempts = str(row["attempts"] or 0)
        if row["next_attempt_at"]:
            attempts += (
                f"<br><span class='muted'>retry "
                f"{esc(str(row['next_attempt_at'])[:16])}</span>"
            )
        lines.append(
            "<tr>"
            f"<td>#{row['id']}</td><td>{esc(label)}</td>"
            f"<td>{esc(row['requester_name'] or '—')}</td>"
            f"<td>{status_html}</td><td>{attempts}</td>"
            f"<td>{esc(str(row['created_at'])[:16])}</td>"
            "</tr>"
        )
    return "".join(lines) or (
        "<tr><td colspan='6' class='muted'>No academy builds yet.</td></tr>"
    )


async def automation_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    auto = bot.cfg.automation
    state = StateRepo(bot.db)
    requests_repo = AutomationRepo(bot.db)

    # Live MC gauges — the same public attributes the admin cog reads.
    pacer = getattr(bot, "pacer", None)
    circuit_open = getattr(pacer, "circuit_open", None)
    mc = getattr(bot, "mc", None)
    backlog = getattr(mc, "pacer_backlog", None)
    backlog_bulk = getattr(mc, "pacer_backlog_bulk", 0) or 0

    if circuit_open is None:
        circuit = badge("unknown", "dim")
    elif circuit_open:
        circuit = badge("OPEN — MC traffic paused", "off")
    else:
        circuit = badge("closed", "ok")

    open_total = await requests_repo.open_count()
    academy_open = await requests_repo.open_count(ACADEMY_KIND)
    tiles = (
        tile("Open requests", open_total)
        + tile("Academy queue", academy_open)
        + tile("MC backlog", "—" if backlog is None else backlog)
    )
    backlog_note = ""
    if backlog:
        backlog_note = (
            f"<p class='muted'>{backlog} MC request(s) waiting for their "
            f"pacing turn ({backlog - backlog_bulk} interactive · "
            f"{backlog_bulk} bulk backfill). A steadily growing interactive "
            "count means demand exceeds missionchief.max_delay.</p>"
        )

    runs = await RunsRepo(bot.db).recent(limit=_RUNS_LIMIT)

    # Academy queue + funds gate (latest scraped balance vs the floor).
    async with bot.db.conn.execute(
        "SELECT * FROM automation_requests WHERE kind = ? "
        "ORDER BY id DESC LIMIT ?",
        (ACADEMY_KIND, _QUEUE_LIMIT),
    ) as cur:
        academy_requests = list(await cur.fetchall())
    balance = await TreasuryRepo(bot.db).latest_balance()
    floor = auto.academy.min_alliance_funds
    if balance is None:
        funds_html = (
            f"<p class='muted'>No alliance balance recorded yet — floor is "
            f"{floor:,} credits; a low-funds build waits in the queue.</p>"
        )
    else:
        funds = int(balance["total_funds"])
        gate = (
            badge("funds OK", "ok") if funds >= floor
            else badge("below floor — builds wait", "off")
        )
        funds_html = (
            f"<p>Alliance funds <strong>{funds:,}</strong> · floor "
            f"{floor:,} {gate} <span class='muted'>(as of "
            f"{esc(str(balance['scraped_at'])[:16])})</span></p>"
        )

    options = "".join(
        f"<option value='{esc(key)}'>{esc(spec['label'])}</option>"
        for key, spec in ACADEMIES.items()
    )
    academy_form = (
        "<form method='post' action='/automation/academy'>"
        f"<label>Academy type</label><select name='academy'>{options}</select>"
        "<p class='muted'>Queued through the same funds-gated queue as the "
        f"Discord panel; built at {esc(auto.academy.address)} as "
        "[AA] &lt;type&gt; #N.</p>"
        "<button>Queue academy build</button></form>"
    )

    body = (
        _mode_banner(auto.dry_run)
        + f"<div class='tiles'>{tiles}</div>"
        + backlog_note
        + "<div class='grid2'>"
        f"<div class='panel'><h2>Automation switches</h2>"
        f"{_switches_table(auto)}</div>"
        "<div>"
        f"<div class='panel'><h2>Circuit breaker</h2><p>{circuit}</p>"
        "<p class='muted'>Opens after repeated MissionChief failures and "
        "pauses all traffic for the configured cooldown.</p></div>"
        f"<div class='panel'><h2>Board reply health</h2>"
        f"{await _reply_failures_html(state, auto.reply_to_board)}</div>"
        "</div></div>"
        f"<div class='panel'><h2>Recent job runs</h2>{_runs_table(runs)}</div>"
        f"<div class='panel'><h2>Academy queue</h2>{funds_html}"
        "<table><tr><th>#</th><th>Type</th><th>Requester</th><th>Status</th>"
        f"<th>Attempts</th><th>Created</th></tr>{_academy_rows(academy_requests)}"
        "</table></div>"
        f"<div class='panel'><h2>Queue an academy build</h2>{academy_form}"
        "</div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Automation", body, active="/automation", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# Academy build (same enqueue path as the Discord panel)
# ---------------------------------------------------------------------------

async def post_academy_build(request: web.Request) -> web.Response:
    bot = _bot(request)
    form = await request.post()
    academy_kind = str(form.get("academy") or "")
    spec = ACADEMIES.get(academy_kind)
    if spec is None:
        _redirect("/automation", err="Pick an academy type from the list.")
    service = getattr(bot, "academy", None)
    if service is None:
        _redirect(
            "/automation",
            err="Academy service is not loaded on this bot.",
        )
    # No Discord interaction behind a console click: discord_user_id stays
    # None so the row carries no fake requester id, exactly like the other
    # web-sourced requests.
    request_id = await service.enqueue(
        academy_kind, requester_name=WEB_ACTOR,
        discord_user_id=None, channel_id=None,
    )
    await bot.log_member_action(
        action="academy_build_clicked",
        detail=(
            f"{academy_kind} academy (request #{request_id}) "
            f"(via {WEB_ACTOR})"
        ),
        actor_name=WEB_ACTOR,
    )
    _kick_academy_build(bot, request_id)
    _redirect(
        "/automation",
        ok=(
            f"{spec['label']} build #{request_id} queued — it runs under "
            "the academy queue's own dry-run and funds gate."
        ),
    )


ROUTES = [
    web.get("/automation", automation_page),
    web.post("/automation/academy", post_academy_build),
]
NAV_ENTRY = ("/automation", "Automation")
