"""Web console: training & building requests (board + Discord + web).

Read side: every ``automation_requests`` row newest-first, filterable by
kind, status and source. Write side: the console enqueues EXACTLY like
the Discord panel (``cogs/requests_panel.py``) — the same
``AutomationRepo.create`` call, the same payload builders and the same
member-action log entries — and the existing pollers execute the rows
under their own dry-run switch, pacing and job locks. Nothing here
talks to MissionChief directly.

Deliberate divergences from the Discord intake, each with the reason
the original behaviour existed and why it does not apply here:

* no contribution gate — the gate keys off the *requester's* verified
  Discord link (anyone could dodge it otherwise); a console submission
  is the operator acting as "Web console", not a member request.
* no immediate geocode of a building link — the handler must stay
  offline. The Discord flow already queues link-only payloads when the
  geocoder hiccups, and the buildings poller resolves + validates the
  pin itself at its next pass (board posts always take that path).
* a non-maps link flashes an error instead of writing a ``skipped``
  audit row — those rows exist to announce *member* rejections in the
  admin log; echoing the operator's own typo there is just noise.

Re-queueing reuses ``AutomationRepo.requeue`` — the exact transition
behind the Discord Approve button — so only terminal (failed/skipped)
rows move and an in-flight request can never be double-armed.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from ..db.repos import AutomationRepo, StateRepo
from ..services.trainings import (
    CLASS_CAPACITY,
    MAX_CLASSES_PER_REQUEST,
    clamp_class_count,
    merged_course_catalog,
)
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page, tile

log = logging.getLogger(__name__)

_LIST_LIMIT = 200
_KINDS = ("training", "building", "event", "academy")
_STATUSES = ("pending", "waiting", "processing", "done", "failed", "skipped")
_STATUS_BADGE = {"done": "ok", "failed": "off"}
#: Same academies (and labels) as the Discord chooser, emoji-free.
_DISCIPLINES = (
    ("fire", "Fire"), ("police", "Police"),
    ("ems", "EMS"), ("coastal", "Water Rescue"),
)


def _payload_data(payload) -> dict:
    try:
        data = json.loads(payload or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _summarize(data: dict) -> str:
    """One-line payload summary, mirroring the admin-log embed fields."""
    parts = []
    for item in data.get("trainings") or []:
        if isinstance(item, dict):
            name = str(item.get("name", "?"))
            count = item.get("count") or 1
            parts.append(f"{name} ×{count}" if count != 1 else name)
        else:
            parts.append(str(item))
    if data.get("building_type"):
        parts.append(str(data["building_type"]))
    if data.get("address"):
        parts.append(str(data["address"]))
    elif data.get("link"):
        parts.append(str(data["link"]))
    if data.get("building_id"):
        parts.append(f"building #{data['building_id']}")
    return " · ".join(parts)


def _options(choices, selected: str) -> str:
    return "".join(
        f"<option value='{esc(value)}'"
        + (" selected" if value == selected else "")
        + f">{esc(label)}</option>"
        for value, label in choices
    )


def _kick_training_queue(bot) -> None:
    """The Discord panel's immediate-execution kick: run the training
    queue in the background under the poll's shared job lock, so the two
    can never overlap. The service path honours dry-run and the pacer;
    a console without the live services (offline tests) simply leaves
    the committed row for the next scheduled poll."""
    trainings = getattr(bot, "trainings", None)
    job_lock = getattr(bot, "job_lock", None)
    if trainings is None or job_lock is None:
        return

    async def _run() -> None:
        try:
            async with job_lock("board-trainings"):
                await trainings.execute_queue_now()
        except Exception:  # noqa: BLE001 — the normal poll retries the row
            log.exception("immediate training execution failed")

    asyncio.get_running_loop().create_task(_run())


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------

async def requests_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    kind = request.query.get("kind", "")
    status = request.query.get("status", "")
    source = request.query.get("source", "")

    where, params = [], []
    if kind in _KINDS:
        where.append("kind = ?")
        params.append(kind)
    if status in _STATUSES:
        where.append("status = ?")
        params.append(status)
    if source == "board":
        where.append("thread_id != 0")
    elif source == "discord":
        # Discord AND web submissions carry the sentinel thread_id 0.
        where.append("thread_id = 0")
    sql = (
        "SELECT * FROM automation_requests "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY id DESC LIMIT ?"
    )
    async with bot.db.conn.execute(sql, (*params, _LIST_LIMIT)) as cur:
        rows = list(await cur.fetchall())

    async with bot.db.conn.execute(
        "SELECT status, COUNT(*) AS n FROM automation_requests GROUP BY status"
    ) as cur:
        by_status = {row["status"]: row["n"] for row in await cur.fetchall()}
    open_count = sum(
        by_status.get(key, 0) for key in ("pending", "waiting", "processing")
    )
    tiles = (
        tile("Open", open_count)
        + tile("Done", by_status.get("done", 0))
        + tile("Failed", by_status.get("failed", 0))
        + tile("Skipped", by_status.get("skipped", 0))
    )

    filter_bar = (
        "<form class='searchbar' method='get'>"
        "<select name='kind' style='max-width:150px'>"
        + _options(
            [("", "All kinds")] + [(k, k.title()) for k in _KINDS], kind
        )
        + "</select><select name='status' style='max-width:150px'>"
        + _options(
            [("", "All statuses")] + [(s, s.title()) for s in _STATUSES],
            status,
        )
        + "</select><select name='source' style='max-width:170px'>"
        + _options(
            [("", "All sources"), ("board", "Board"),
             ("discord", "Discord / Web")],
            source,
        )
        + "</select><button>Filter</button></form>"
    )

    lines = []
    for row in rows:
        data = _payload_data(row["payload"])
        if row["thread_id"]:
            source_html = (
                "<a href='https://www.missionchief.com/alliance_threads/"
                f"{int(row['thread_id'])}' target='_blank'>"
                f"board #{int(row['post_id'])}</a>"
            )
        else:
            source_html = "<span class='soft'>Discord / Web</span>"
        requester = esc(row["requester_name"] or "—")
        if row["requester_mc_id"]:
            requester = (
                f"<a href='/members/{int(row['requester_mc_id'])}'>"
                f"{requester}</a>"
            )
        status_html = badge(
            row["status"], _STATUS_BADGE.get(row["status"], "dim")
        )
        info = []
        if row["status_detail"]:
            info.append(str(row["status_detail"])[:140])
        if row["attempts"]:
            info.append(f"{row['attempts']} attempt(s)")
        if row["next_attempt_at"]:
            info.append(f"retry {str(row['next_attempt_at'])[:16]}")
        if info:
            status_html += (
                f"<br><span class='muted'>{esc(' · '.join(info))}</span>"
            )
        created = str(row["created_at"])[:16]
        updated = str(row["updated_at"])[:16]
        when = esc(created)
        if updated != created:
            when += f"<br><span class='muted'>upd {esc(updated)}</span>"
        action = ""
        # Only real failures get a retry — the Discord flow never
        # re-queues 'skipped' rows (intake rejections, dry-run simulations).
        if row["status"] == "failed":
            action = (
                "<form class='inline' method='post' "
                f"action='/requests/{row['id']}/requeue'>"
                "<button class='small ghost'>Requeue</button></form>"
            )
        lines.append(
            "<tr>"
            f"<td>#{row['id']}</td>"
            f"<td>{esc(row['kind'])}</td>"
            f"<td>{source_html}</td>"
            f"<td>{requester}</td>"
            f"<td>{esc(_summarize(data)[:90])}</td>"
            f"<td>{status_html}</td>"
            f"<td>{when}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    table = "".join(lines) or (
        "<tr><td colspan='8' class='muted'>No requests recorded.</td></tr>"
    )

    notes = []
    if bot.cfg.automation.dry_run:
        notes.append("dry-run is ON — actions are simulated")
    if not bot.cfg.automation.training.enabled:
        notes.append("training automation is OFF — queued requests wait")
    if not bot.cfg.automation.building.enabled:
        notes.append("building automation is OFF — queued requests wait")
    note_html = ""
    if notes:
        note_html = f"<p class='muted'>{esc(' · '.join(notes))}</p>"

    # Same course universe as the Discord chooser: the live-harvested
    # catalog where one exists, the built-in one as bootstrap fallback.
    catalog = await merged_course_catalog(StateRepo(bot.db))
    groups = []
    for discipline, label in _DISCIPLINES:
        options = []
        for name, days in sorted((catalog.get(discipline) or {}).items()):
            value = f"{discipline}|{name}"
            text = f"{name} ({days}d)" if days else name
            options.append(
                f"<option value='{esc(value)}'>{esc(text)}</option>"
            )
        if options:
            groups.append(
                f"<optgroup label='{esc(label)}'>" + "".join(options)
                + "</optgroup>"
            )
    count_options = []
    for n in range(1, MAX_CLASSES_PER_REQUEST + 1):
        people = n * CLASS_CAPACITY
        label = (
            f"1 class — {people} people" if n == 1
            else f"{n} classes — {people} people"
        )
        count_options.append(f"<option value='{n}'>{label}</option>")
    training_form = (
        "<form method='post' action='/requests/training'>"
        "<label>Course</label>"
        f"<select name='course'>{''.join(groups)}</select>"
        "<label>Classes</label>"
        f"<select name='count'>{''.join(count_options)}</select>"
        "<button>Queue training request</button></form>"
    )
    building_form = (
        "<form method='post' action='/requests/building'>"
        "<label>Google Maps link (real hospital or prison)</label>"
        "<input name='link' placeholder='https://maps.app.goo.gl/…' required>"
        "<p class='muted'>The pin is resolved and validated by the building "
        "poller at its next pass (~5 min).</p>"
        "<button>Queue building request</button></form>"
    )

    body = (
        f"<div class='tiles'>{tiles}</div>"
        + note_html
        + filter_bar
        + "<div class='panel'><table><tr><th>#</th><th>Kind</th>"
        "<th>Source</th><th>Requester</th><th>Details</th><th>Status</th>"
        f"<th>Created</th><th></th></tr>{table}"
        f"<p class='muted'>{len(rows)} request(s)"
        f"{' (capped)' if len(rows) == _LIST_LIMIT else ''}</p></div>"
        "<div class='grid2'>"
        f"<div class='panel'><h2>New training request</h2>{training_form}"
        "</div>"
        f"<div class='panel'><h2>New building request</h2>{building_form}"
        "</div></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Requests", body, active="/requests", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# Create (same enqueue path as the Discord panel)
# ---------------------------------------------------------------------------

async def post_training_request(request: web.Request) -> web.Response:
    from ..cogs.requests_panel import DISCORD_THREAD, training_request_payload
    from ..mc.trainings_catalog import DISCIPLINES

    bot = _bot(request)
    form = await request.post()
    raw = str(form.get("course") or "")
    discipline, sep, course = raw.partition("|")
    catalog = await merged_course_catalog(StateRepo(bot.db))
    days = (catalog.get(discipline) or {}).get(course) if sep else None
    if days is None:
        _redirect("/requests", err="Pick a course from the list.")
    if not days:
        # Same fallback as the chooser: a live-harvested course without a
        # duration takes the built-in catalog's number (0 if unknown).
        days = DISCIPLINES.get(discipline, {}).get(course, 0)
    count = clamp_class_count(form.get("count"))
    payload = training_request_payload(
        discipline, course, user_id=0, channel_id=None, remind=False,
        count=count, days=int(days),
    )
    # post_id 0 (vs the panel's interaction id): there is no interaction,
    # and a falsy post_id keeps the board tidy-up from scheduling the
    # deletion of a post that never existed.
    rid = await AutomationRepo(bot.db).create(
        kind="training", thread_id=DISCORD_THREAD, post_id=0,
        requester_name=WEB_ACTOR, requester_mc_id=None,
        payload=json.dumps(payload),
    )
    await bot.log_member_action(
        action="training_requested",
        detail=f"{course} ×{count} (request #{rid}) (via {WEB_ACTOR})",
        actor_name=WEB_ACTOR,
    )
    if bot.cfg.automation.training.enabled:
        _kick_training_queue(bot)
        _redirect(
            "/requests",
            ok=f"Training request #{rid} queued — opening now.",
        )
    _redirect(
        "/requests",
        ok=f"Training request #{rid} queued — training automation is OFF, "
           "it runs once enabled.",
    )


async def post_building_request(request: web.Request) -> web.Response:
    from ..cogs.requests_panel import DISCORD_THREAD, building_request_payload

    bot = _bot(request)
    form = await request.post()
    link = str(form.get("link") or "").strip()
    payload = building_request_payload(link, user_id=0, channel_id=None)
    if payload is None:
        _redirect(
            "/requests",
            err="That does not look like a Google Maps link — paste the "
                "share link of a real hospital or prison.",
        )
    rid = await AutomationRepo(bot.db).create(
        kind="building", thread_id=DISCORD_THREAD, post_id=0,
        requester_name=WEB_ACTOR, requester_mc_id=None,
        payload=json.dumps(payload),
    )
    await bot.log_member_action(
        action="building_requested",
        detail=f"{payload['link'][:200]} (request #{rid}) (via {WEB_ACTOR})",
        actor_name=WEB_ACTOR,
    )
    _redirect(
        "/requests",
        ok=f"Building request #{rid} queued — the pin is checked at the "
           "next building pass.",
    )


# ---------------------------------------------------------------------------
# Requeue (the Discord Approve button's transition)
# ---------------------------------------------------------------------------

async def post_requeue(request: web.Request) -> web.Response:
    bot = _bot(request)
    request_id = int(request.match_info["request_id"])
    repo = AutomationRepo(bot.db)
    row = await repo.get(request_id)
    if row is None:
        _redirect("/requests", err=f"Request #{request_id} not found.")
    if row["status"] != "failed":
        # 'skipped' rows are intake rejections or dry-run simulations —
        # the Discord flow never re-queues those, so neither does the web.
        _redirect(
            "/requests",
            err=f"Request #{request_id} is {row['status']} — only "
                "failed requests can be re-queued.",
        )
    data = _payload_data(row["payload"])
    # Like the Approve button: a fresh attempt is intentional, so the
    # verify-only flag is cleared before re-queueing.
    data.pop("pending_confirm", None)
    if not await repo.requeue(request_id, payload=json.dumps(data)):
        _redirect(
            "/requests",
            err=f"Request #{request_id} could not be re-queued.",
        )
    await bot.log_member_action(
        action="request_requeued",
        detail=f"{row['kind']} request #{request_id} (via {WEB_ACTOR})",
        discord_user_id=(
            int(data["discord_user_id"]) if data.get("discord_user_id")
            else None
        ),
        mc_user_id=row["requester_mc_id"],
        actor_name=row["requester_name"],
    )
    _redirect("/requests", ok=f"Request #{request_id} re-queued.")


ROUTES = [
    web.get("/requests", requests_page),
    web.post("/requests/training", post_training_request),
    web.post("/requests/building", post_building_request),
    web.post("/requests/{request_id:\\d+}/requeue", post_requeue),
]
NAV_ENTRY = ("/requests", "Requests")
