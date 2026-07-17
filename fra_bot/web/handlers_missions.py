"""Web console: the scheduled mission/event queue and the rotation list.

Read side: the open ``scheduled_missions`` requests per kind in the exact
FIFO order the scheduler serves them (the head of each kind is the ONLY
row that may take that kind's next free window), the recently finished
ones, and the admin rotation list with its enabled state.

Write side is enqueue-only. A new request goes through the SAME
``MissionScheduler.enqueue_discord`` call as the ``/mission`` slash
command, so the scheduler poller — which honours ``automation.dry_run``,
the paced client and the shared start lock — remains the only thing that
ever starts a mission. Rotation changes reuse the exact repo calls behind
``!fra rotation add/on/off/remove``; cancel reuses ``!fra cancelmission``.
Nothing here talks to MissionChief.

Deliberate divergences from the Discord intake, with the reasons:

* no contribution gate — it keys off the *requester's* verified Discord
  link; a console submission is the operator acting as "Web console".
* custom Own-mission values ARE accepted (the Discord chooser drops them
  because its modals can't carry the unit values — a web form can; the
  in-game board template takes the same path).
* there is no rotation *reorder*: the cycle order is derived
  (least-recently-started round-robin), not stored, so none exists to
  edit in Discord either.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from ..db.repos import MissionsRepo, RotationRepo
from ..mc.parsers.events import EVENT_TYPES
from ..mc.parsers.mission_spec import MissionSpecError, PRESET_TYPE_IDS
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page, tile

log = logging.getLogger(__name__)

_QUEUE_LIMIT = 50
_RECENT_LIMIT = 15
_KIND_SECTIONS = (("large", "Large scale missions"), ("event", "Alliance events"))
_STATUS_BADGE = {"done": "ok", "failed": "off", "cancelled": "off"}
_OPEN_STATUSES = ("pending", "waiting", "processing")


def _describe(row) -> str:
    """Mirror ``MissionSpec.describe()`` for a stored queue/rotation row."""
    if row["kind"] == "event":
        if row["event_random"] or row["event_type_id"] is None:
            etype = "random"
        else:
            etype = EVENT_TYPES.get(row["event_type_id"], str(row["event_type_id"]))
        area = row["area"] or "medium"
        shape = row["shape"] or "rectangle"
        volume = row["call_volume"] or "45"
        return f"event · {etype} · {area}/{shape}/{volume}s"
    source = row["mission_source"]
    if source == "custom":
        return f"custom '{row['caption'] or '?'}'"
    if source == "saved":
        return f"saved '{row['saved_name'] or '?'}'"
    if row["preset_type_id"] is not None:
        name = PRESET_TYPE_IDS.get(row["preset_type_id"], row["preset_type_id"])
        return f"preset {name}"
    return "standard large mission"


def _requester_html(row) -> str:
    name = esc(row["requester_name"] or "—")
    if row["requester_mc_id"]:
        return f"<a href='/members/{int(row['requester_mc_id'])}'>{name}</a>"
    return name


def _options(choices, selected: str = "") -> str:
    return "".join(
        f"<option value='{esc(value)}'"
        + (" selected" if value == selected else "")
        + f">{esc(label)}</option>"
        for value, label in choices
    )


def _status_html(row) -> str:
    html = badge(row["status"], _STATUS_BADGE.get(row["status"], "dim"))
    info = []
    if row["status_detail"]:
        info.append(str(row["status_detail"])[:120])
    if row["attempts"]:
        info.append(f"{row['attempts']} attempt(s)")
    if row["next_attempt_at"]:
        info.append(f"next window {str(row['next_attempt_at'])[:16]}")
    if info:
        html += f"<br><span class='muted'>{esc(' · '.join(info))}</span>"
    return html


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

async def missions_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    missions = MissionsRepo(bot.db)
    rotation = RotationRepo(bot.db)

    async with bot.db.conn.execute(
        "SELECT status, COUNT(*) AS n FROM scheduled_missions GROUP BY status"
    ) as cur:
        by_status = {row["status"]: row["n"] for row in await cur.fetchall()}
    open_count = sum(by_status.get(key, 0) for key in _OPEN_STATUSES)
    tiles = (
        tile("Open requests", open_count)
        + tile("Started", by_status.get("done", 0))
        + tile("Failed", by_status.get("failed", 0))
        + tile("Rotation (active)", await rotation.active_count())
    )

    auto = bot.cfg.automation
    notes = []
    if auto.dry_run:
        notes.append("dry-run is ON — starts are simulated")
    if not auto.mission.enabled:
        notes.append("mission scheduler is OFF — queued requests wait")
    if not auto.events.enabled:
        notes.append("events board intake is OFF")
    note_html = f"<p class='muted'>{esc(' · '.join(notes))}</p>" if notes else ""

    # Open queues, one panel per kind, in the scheduler's strict FIFO
    # order — only the head row may take that kind's next free window.
    queue_panels = []
    for kind, label in _KIND_SECTIONS:
        rows = await missions.open_for_kind(kind, limit=_QUEUE_LIMIT)
        lines = []
        for index, row in enumerate(rows):
            marker = badge("next", "ok") + " " if index == 0 else ""
            schedule = badge("recurring", "dim") if row["recurring"] else "once"
            source = "board" if row["source"] == "board" else "discord/web"
            lines.append(
                "<tr>"
                f"<td>{marker}#{row['id']}</td>"
                f"<td>{esc(_describe(row))}</td>"
                f"<td>{esc(row['address'] or row['location_text'] or '?')}</td>"
                f"<td>{_requester_html(row)} "
                f"<span class='muted'>({esc(source)})</span></td>"
                f"<td>{schedule}</td>"
                f"<td>{_status_html(row)}</td>"
                f"<td>{esc(str(row['created_at'])[:16])}</td>"
                "<td><form class='inline' method='post' "
                f"action='/missions/{row['id']}/cancel'>"
                "<button class='small ghost'>Cancel</button></form></td>"
                "</tr>"
            )
        table = "".join(lines) or (
            "<tr><td colspan='8' class='muted'>Queue is empty.</td></tr>"
        )
        queue_panels.append(
            f"<div class='panel'><h2>{esc(label)} — queue (FIFO)</h2>"
            "<table><tr><th>#</th><th>Request</th><th>Location</th>"
            "<th>Requester</th><th>Schedule</th><th>Status</th>"
            f"<th>Created</th><th></th></tr>{table}</table></div>"
        )

    # Recently started / finished (terminal statuses), newest first.
    async with bot.db.conn.execute(
        "SELECT * FROM scheduled_missions "
        "WHERE status IN ('done', 'failed', 'skipped', 'cancelled') "
        "ORDER BY id DESC LIMIT ?",
        (_RECENT_LIMIT,),
    ) as cur:
        recent_rows = list(await cur.fetchall())
    recent_lines = "".join(
        "<tr>"
        f"<td>#{row['id']}</td>"
        f"<td>{esc(_describe(row))}</td>"
        f"<td>{esc(row['address'] or row['location_text'] or '?')}</td>"
        f"<td>{_requester_html(row)}</td>"
        f"<td>{_status_html(row)}</td>"
        f"<td>{esc(str(row['updated_at'])[:16])}</td>"
        "</tr>"
        for row in recent_rows
    ) or "<tr><td colspan='6' class='muted'>Nothing finished yet.</td></tr>"
    recent_panel = (
        "<div class='panel'><h2>Recently started / finished</h2>"
        "<table><tr><th>#</th><th>Request</th><th>Location</th>"
        f"<th>Requester</th><th>Outcome</th><th>Updated</th></tr>{recent_lines}"
        "</table></div>"
    )

    # Rotation list in stored order; the "next" marker is per kind — the
    # large (daily) and event (weekly) cycles run independently.
    next_ids = set()
    for kind, _ in _KIND_SECTIONS:
        entry = await rotation.next_entry(kind=kind)
        if entry is not None:
            next_ids.add(entry["id"])
    rotation_lines = []
    for row in await rotation.list_all():
        marker = badge("next", "ok") + " " if row["id"] in next_ids else ""
        state = badge("active", "ok") if row["active"] else badge("paused", "off")
        started = (
            f"×{row['start_count']} "
            f"(last {str(row['last_started_at'])[:10]})"
            if row["last_started_at"] else f"×{row['start_count']} (never)"
        )
        if row["active"]:
            toggle = (
                "<form class='inline' method='post' "
                f"action='/missions/rotation/{row['id']}/pause'>"
                "<button class='small ghost'>Pause</button></form>"
            )
        else:
            toggle = (
                "<form class='inline' method='post' "
                f"action='/missions/rotation/{row['id']}/resume'>"
                "<button class='small ghost'>Resume</button></form>"
            )
        rotation_lines.append(
            "<tr>"
            f"<td>{marker}#{row['id']}</td>"
            f"<td>{esc(row['kind'])}</td>"
            f"<td>{esc(_describe(row))}</td>"
            f"<td>{esc(row['address'] or row['location_text'] or '?')}</td>"
            f"<td>{esc(started)}</td>"
            f"<td>{state}</td>"
            f"<td>{esc(row['created_by'] or '—')}</td>"
            f"<td>{toggle} "
            "<form class='inline' method='post' "
            f"action='/missions/rotation/{row['id']}/remove'>"
            "<button class='small ghost'>Remove</button></form></td>"
            "</tr>"
        )
    rotation_table = "".join(rotation_lines) or (
        "<tr><td colspan='8' class='muted'>The rotation list is empty.</td></tr>"
    )
    rotation_panel = (
        "<div class='panel'><h2>Rotation (fills free slots when the queue "
        "is empty)</h2>"
        "<p class='muted'>Cycle order is least-recently-started round-robin "
        "per kind — member requests always go first.</p>"
        "<table><tr><th>#</th><th>Kind</th><th>Entry</th><th>Location</th>"
        "<th>Starts</th><th>State</th><th>Added by</th><th></th></tr>"
        f"{rotation_table}</table></div>"
    )

    preset_options = _options(
        [("", "Standard (form default)")]
        + [(name, f"Preset: {name}") for name in sorted(PRESET_TYPE_IDS.values())]
    )
    schedule_options = _options(
        [("once", "One-time"), ("recurring", "Recurring (joins the rotation)")]
    )
    large_form = (
        "<form method='post' action='/missions/request'>"
        "<input type='hidden' name='kind' value='large'>"
        "<label>Location</label>"
        "<input name='location' placeholder='Place name or maps link' required>"
        "<label>Schedule</label>"
        f"<select name='schedule'>{schedule_options}</select>"
        "<label>Mission data</label>"
        f"<select name='preset'>{preset_options}</select>"
        "<label>… or a saved mission (name from the game's dropdown)</label>"
        "<input name='saved'>"
        "<label>… or custom Own-mission values (e.g. need_lf=25 need_elw1=6)"
        "</label><input name='custom'>"
        "<label>Caption (custom only)</label><input name='name'>"
        "<button>Queue mission request</button></form>"
    )
    event_type_options = _options(
        [("", "Random")] + [(name, name) for name in EVENT_TYPES.values()]
    )
    event_form = (
        "<form method='post' action='/missions/request'>"
        "<input type='hidden' name='kind' value='event'>"
        "<label>Location</label>"
        "<input name='location' placeholder='Place name or maps link' required>"
        "<label>Schedule</label>"
        f"<select name='schedule'>{schedule_options}</select>"
        "<label>Event type</label>"
        f"<select name='event_type'>{event_type_options}</select>"
        "<label>Area</label><select name='area'>"
        + _options(
            [("small", "Small"), ("medium", "Medium"), ("large", "Large")],
            "medium",
        )
        + "</select>"
        "<label>Shape</label><select name='shape'>"
        + _options([("rectangle", "Rectangle"), ("circle", "Circle")])
        + "</select>"
        "<label>Call volume (seconds)</label><select name='call_volume'>"
        + _options([("30", "30"), ("45", "45"), ("60", "60")], "45")
        + "</select>"
        "<button>Queue event request</button></form>"
    )
    rotation_form = (
        "<form method='post' action='/missions/rotation/add'>"
        "<label>Location</label>"
        "<input name='location' placeholder='Place name or maps link' required>"
        "<label>Kind</label><select name='kind'>"
        + _options(
            [("large", "Large scale mission (daily)"),
             ("event", "Alliance event (weekly)")]
        )
        + "</select>"
        "<label>Mission data (large only)</label>"
        f"<select name='preset'>{preset_options}</select>"
        "<label>… or a saved mission</label><input name='saved'>"
        "<label>… or custom Own-mission values</label><input name='custom'>"
        "<label>Caption (custom only)</label><input name='name'>"
        "<button>Add rotation entry</button></form>"
    )

    body = (
        f"<div class='tiles'>{tiles}</div>"
        + note_html
        + "".join(queue_panels)
        + rotation_panel
        + recent_panel
        + "<div class='grid2'>"
        f"<div class='panel'><h2>Request a large scale mission</h2>{large_form}"
        "</div>"
        f"<div class='panel'><h2>Request an alliance event</h2>{event_form}"
        "</div></div>"
        f"<div class='panel'><h2>Add a rotation entry</h2>{rotation_form}</div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Missions", body, active="/missions", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# New request (the /mission slash command's enqueue path)
# ---------------------------------------------------------------------------

async def post_request(request: web.Request) -> web.Response:
    # Function-local: cogs.missions needs the discord package; the page
    # itself must render without it.
    from ..cogs.missions import build_spec

    bot = _bot(request)
    service = getattr(bot, "missions_service", None)
    if service is None:
        _redirect("/missions", err="Mission service is not running.")
    form = await request.post()
    try:
        spec = build_spec(
            location=str(form.get("location") or ""),
            kind=str(form.get("kind") or "large"),
            schedule=str(form.get("schedule") or "once"),
            preset=str(form.get("preset") or "") or None,
            saved=str(form.get("saved") or "") or None,
            custom=str(form.get("custom") or "") or None,
            name=str(form.get("name") or "") or None,
            event_type=str(form.get("event_type") or ""),
            area=str(form.get("area") or "") or None,
            shape=str(form.get("shape") or "") or None,
            call_volume=str(form.get("call_volume") or "") or None,
        )
    except (ValueError, MissionSpecError) as exc:
        _redirect("/missions", err=str(exc))
    mission_id = await service.enqueue_discord(
        spec, requester_name=WEB_ACTOR, requester_mc_id=None,
        discord_user_id=None, channel_id=None,
    )
    recurring = " — recurring" if spec.recurring else ""
    await bot.log_member_action(
        action=(
            "event_requested" if spec.kind == "event" else "mission_requested"
        ),
        detail=f"{spec.describe()} at {spec.location_text} "
               f"(request #{mission_id}){recurring} (via {WEB_ACTOR})",
        actor_name=WEB_ACTOR,
    )
    note = (
        "" if bot.cfg.automation.mission.enabled
        else " The scheduler is OFF — it waits until an admin enables it."
    )
    _redirect(
        "/missions",
        ok=f"Request #{mission_id} queued — it starts at the next free "
           f"alliance slot.{note}",
    )


async def post_cancel(request: web.Request) -> web.Response:
    bot = _bot(request)
    mission_id = int(request.match_info["mission_id"])
    repo = MissionsRepo(bot.db)
    row = await repo.get(mission_id)
    if row is None:
        _redirect("/missions", err=f"Request #{mission_id} not found.")
    # The exact `!fra cancelmission` transition: only open rows move, and
    # posted_at resets so the outcome publisher announces it to Discord.
    if not await repo.cancel(mission_id):
        _redirect(
            "/missions",
            err=f"Request #{mission_id} is {row['status']} — only "
                "pending/waiting requests can be cancelled.",
        )
    await bot.log_member_action(
        action="mission_cancelled",
        detail=f"request #{mission_id} — {_describe(row)} (via {WEB_ACTOR})",
        discord_user_id=(
            int(row["discord_user_id"]) if row["discord_user_id"] else None
        ),
        mc_user_id=row["requester_mc_id"],
        actor_name=row["requester_name"],
    )
    _redirect("/missions", ok=f"Request #{mission_id} cancelled.")


# ---------------------------------------------------------------------------
# Rotation (the `!fra rotation` repo calls)
# ---------------------------------------------------------------------------

async def post_rotation_add(request: web.Request) -> web.Response:
    from ..cogs.missions import build_spec

    bot = _bot(request)
    form = await request.post()
    try:
        spec = build_spec(
            location=str(form.get("location") or ""),
            kind=str(form.get("kind") or "large"),
            preset=str(form.get("preset") or "") or None,
            saved=str(form.get("saved") or "") or None,
            custom=str(form.get("custom") or "") or None,
            name=str(form.get("name") or "") or None,
        )
    except (ValueError, MissionSpecError) as exc:
        _redirect("/missions", err=str(exc))
    # Same stored fields as `!fra rotation add` — event knobs deliberately
    # unset, so an event entry gets a random standard type per start.
    rid = await RotationRepo(bot.db).add(
        location_text=spec.location_text,
        kind=spec.kind,
        mission_source=spec.source,
        preset_type_id=spec.preset_type_id,
        caption=spec.custom.caption if spec.custom else spec.saved_name,
        custom_values=json.dumps(spec.custom.values) if spec.custom else None,
        saved_name=spec.saved_name,
        active=1,
        created_by=WEB_ACTOR,
    )
    _redirect(
        "/missions",
        ok=f"Rotation #{rid} added — {spec.describe()} at {spec.location_text}.",
    )


async def post_rotation_state(request: web.Request) -> web.Response:
    bot = _bot(request)
    rotation_id = int(request.match_info["rotation_id"])
    active = request.match_info["state"] == "resume"
    if not await RotationRepo(bot.db).set_active(rotation_id, active):
        _redirect("/missions", err=f"Rotation #{rotation_id} not found.")
    verb = "resumed" if active else "paused"
    _redirect("/missions", ok=f"Rotation #{rotation_id} {verb}.")


async def post_rotation_remove(request: web.Request) -> web.Response:
    bot = _bot(request)
    rotation_id = int(request.match_info["rotation_id"])
    if not await RotationRepo(bot.db).remove(rotation_id):
        _redirect("/missions", err=f"Rotation #{rotation_id} not found.")
    _redirect("/missions", ok=f"Rotation #{rotation_id} removed.")


ROUTES = [
    web.get("/missions", missions_page),
    web.post("/missions/request", post_request),
    web.post("/missions/{mission_id:\\d+}/cancel", post_cancel),
    web.post("/missions/rotation/add", post_rotation_add),
    web.post("/missions/rotation/{rotation_id:\\d+}/{state:pause|resume}",
             post_rotation_state),
    web.post("/missions/rotation/{rotation_id:\\d+}/remove",
             post_rotation_remove),
]
NAV_ENTRY = ("/missions", "Missions")
