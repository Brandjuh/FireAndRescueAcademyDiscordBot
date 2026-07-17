"""Web console route handlers.

Every handler reads the live bot from ``request.app["bot"]`` — the
console runs inside the bot process, on the same DB handle and the same
paced MissionChief client, so nothing here opens a second path to
either. Mutations go through the SAME repos/services the Discord
commands use and land in the member-action log as "Web console".
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from ..core import settings as rt
from ..db.repos import (
    GameSyncRepo,
    LinksRepo,
    MemberActionsRepo,
    MemberProfilesRepo,
    PROFILE_FIELDS,
    SanctionsRepo,
    StateRepo,
)
from ..services.dossier import DossierService
from .html import badge, esc, page, tile

log = logging.getLogger(__name__)

WEB_ACTOR = "Web console"
_LIST_LIMIT = 300


def _bot(request: web.Request):
    return request.app["bot"]


def _flash(request: web.Request) -> tuple[str | None, bool]:
    if "err" in request.query:
        return request.query["err"], True
    if "ok" in request.query:
        return request.query["ok"], False
    return None, False


def _redirect(path: str, *, ok: str | None = None, err: str | None = None):
    from urllib.parse import quote

    if err:
        path += f"?err={quote(err)}"
    elif ok:
        path += f"?ok={quote(ok)}"
    raise web.HTTPFound(path)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    bot = _bot(request)
    counts = {}
    for key, query in {
        "members": "SELECT COUNT(*) AS n FROM members WHERE is_active = 1",
        "linked": ("SELECT COUNT(*) AS n FROM member_links "
                   "WHERE status = 'approved'"),
        "synced": "SELECT COUNT(*) AS n FROM game_sync",
        "sanctions": ("SELECT COUNT(*) AS n FROM sanctions "
                      "WHERE status = 'active'"),
    }.items():
        async with bot.db.conn.execute(query) as cur:
            counts[key] = (await cur.fetchone())["n"]
    tiles = (
        tile("Active members", counts["members"])
        + tile("Linked to Discord", counts["linked"])
        + tile("Game-synced", counts["synced"])
        + tile("Active sanctions", counts["sanctions"])
    )
    body = (
        f"<div class='tiles'>{tiles}</div>"
        "<div class='panel'><h2>Alliance snapshot</h2>"
        "<img class='card' src='/images/infographic.png' "
        "alt='Alliance snapshot' loading='lazy'></div>"
        "<div class='panel'><h2>Fleet</h2>"
        "<img class='card' src='/images/fleet.png' alt='Alliance fleet' "
        "loading='lazy'></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Dashboard", body, active="/", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

async def members(request: web.Request) -> web.Response:
    bot = _bot(request)
    query = (request.query.get("q") or "").strip()
    show = request.query.get("show", "active")
    where, params = [], []
    if show == "active":
        where.append("m.is_active = 1")
    elif show == "left":
        where.append("m.is_active = 0")
    if query:
        if query.isdigit():
            where.append("(m.name LIKE ? OR m.mc_user_id = ?)")
            params += [f"%{query}%", int(query)]
        else:
            where.append("m.name LIKE ?")
            params.append(f"%{query}%")
    sql = (
        "SELECT m.mc_user_id, m.name, m.role, m.is_active, m.earned_credits, "
        "m.contribution_rate, l.discord_id AS linked_discord "
        "FROM members m LEFT JOIN member_links l "
        "ON l.mc_user_id = m.mc_user_id AND l.status = 'approved' "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY m.is_active DESC, m.name COLLATE NOCASE LIMIT ?"
    )
    async with bot.db.conn.execute(sql, (*params, _LIST_LIMIT)) as cur:
        rows = await cur.fetchall()

    options = "".join(
        f"<option value='{value}'{' selected' if show == value else ''}>"
        f"{label}</option>"
        for value, label in (
            ("active", "Active"), ("left", "Left"), ("all", "All"),
        )
    )
    lines = "".join(
        "<tr>"
        f"<td><a href='/members/{row['mc_user_id']}'>{esc(row['name'])}</a></td>"
        f"<td>{esc(row['role'] or '—')}</td>"
        f"<td>{row['earned_credits'] or 0:,}</td>"
        f"<td>{row['contribution_rate'] if row['contribution_rate'] is not None else '—'}</td>"
        f"<td>{badge('linked', 'ok') if row['linked_discord'] else badge('not linked')}</td>"
        f"<td>{badge('active', 'ok') if row['is_active'] else badge('left', 'off')}</td>"
        "</tr>"
        for row in rows
    )
    body = (
        "<form class='searchbar' method='get'>"
        f"<input name='q' placeholder='Name or MC id' value='{esc(query)}'>"
        f"<select name='show' style='max-width:130px'>{options}</select>"
        "<button>Search</button></form>"
        "<div class='panel'><table><tr><th>Name</th><th>Rank</th>"
        "<th>Credits</th><th>Rate %</th><th>Discord</th><th>Status</th></tr>"
        f"{lines}</table>"
        f"<p class='muted'>{len(rows)} member(s)"
        f"{' (capped)' if len(rows) == _LIST_LIMIT else ''}</p></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Members", body, active="/members", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


def _profile_form(mc_user_id: int, current: dict) -> str:
    fields = "".join(
        f"<label>{field.replace('_', ' ')}</label>"
        + (
            f"<textarea name='{field}'>{esc(current.get(field) or '')}</textarea>"
            if field in ("bio", "specialties", "vehicles", "buildings")
            else f"<input name='{field}' value='{esc(current.get(field) or '')}'>"
        )
        for field in PROFILE_FIELDS
    )
    return (
        f"<form method='post' action='/members/{mc_user_id}/profile'>"
        f"{fields}<button>Save profile</button></form>"
    )


def _sanction_form(mc_user_id: int) -> str:
    from ..cogs.sanctions import SANCTION_TYPE_KEYS

    options = "".join(
        f"<option value='{key}'>{esc(label)}</option>"
        for key, label in SANCTION_TYPE_KEYS.items()
    )
    return (
        f"<form method='post' action='/members/{mc_user_id}/sanctions'>"
        f"<label>Type</label><select name='type'>{options}</select>"
        "<label>Reason</label><input name='reason' required>"
        "<label>Notes (optional)</label><input name='notes'>"
        "<button>Add sanction</button></form>"
    )


async def member_detail(request: web.Request) -> web.Response:
    bot = _bot(request)
    mc_user_id = int(request.match_info["mc_id"])
    dossier = await DossierService(bot.db).build(mc_user_id)
    if dossier is None:
        raise web.HTTPNotFound(text="Unknown member")

    link_line = badge("not linked to Discord")
    profile_html = "<p class='muted'>No Discord link — no profile.</p>"
    if dossier.link_status == "approved" and dossier.discord_id:
        link_line = badge(f"Discord {dossier.discord_id}", "ok")
        row = await MemberProfilesRepo(bot.db).get(int(dossier.discord_id))
        profile_html = _profile_form(
            mc_user_id, dict(row) if row is not None else {}
        )

    sync_html = "<p class='muted'>No game sync yet.</p>"
    sync = await GameSyncRepo(bot.db).get_by_mc(mc_user_id)
    if sync is not None:
        sync_html = (
            f"<p>{sync['building_count']} buildings · "
            f"{sync['vehicle_count']} vehicles "
            f"<span class='muted'>(synced {esc(str(sync['synced_at'])[:16])})"
            "</span></p>"
        )

    sanctions_repo = SanctionsRepo(bot.db)
    sanction_rows = await sanctions_repo.for_member(
        mc_user_id=mc_user_id, discord_user_id=dossier.discord_id,
        name=dossier.name,
    )
    warnings = await sanctions_repo.official_warning_count(
        mc_user_id=mc_user_id, discord_user_id=dossier.discord_id,
        name=dossier.name,
    )
    sanction_lines = "".join(
        "<tr>"
        f"<td>#{row['id']}</td><td>{esc(row['created_at'][:10])}</td>"
        f"<td>{esc(row['sanction_type'])}</td><td>{esc(row['reason'])}</td>"
        f"<td>{badge('active', 'off') if row['status'] == 'active' else badge('revoked')}</td>"
        "<td>"
        + (
            f"<form class='inline' method='post' action='/sanctions/{row['id']}/revoke'>"
            f"<input type='hidden' name='mc_id' value='{mc_user_id}'>"
            "<button class='small ghost'>Revoke</button></form>"
            if row["status"] == "active" else ""
        )
        + "</td></tr>"
        for row in sanction_rows
    ) or "<tr><td colspan='6' class='muted'>No sanctions on record.</td></tr>"

    actions = await MemberActionsRepo(bot.db).for_member(
        discord_user_id=dossier.discord_id, mc_user_id=mc_user_id,
        name=dossier.name,
    )
    action_lines = "".join(
        f"<li><span class='muted'>{esc(row['created_at'][:16])}</span> "
        f"{esc(row['action'].replace('_', ' '))}"
        + (f" — {esc(row['detail'])}" if row["detail"] else "")
        + "</li>"
        for row in actions
    ) or "<li class='muted'>No bot actions on record.</li>"

    from ..services.timeline import build_timeline

    events = await build_timeline(
        bot.db, mc_user_id=mc_user_id, name=dossier.name,
        discord_user_id=dossier.discord_id,
    )
    timeline_lines = "".join(
        f"<li><span class='muted'>{esc(event.at[:16])}</span> {esc(event.icon)} "
        f"{esc(event.title)}"
        + (f" <span class='soft'>{esc(event.detail)}</span>" if event.detail else "")
        + "</li>"
        for event in events[:40]
    ) or "<li class='muted'>No history yet.</li>"

    status = badge("active", "ok") if dossier.is_active else badge("left", "off")
    credits = (
        f"{dossier.earned_credits:,}" if dossier.earned_credits is not None
        else "—"
    )
    rate = (
        f"{dossier.contribution_rate:g}%"
        if dossier.contribution_rate is not None else "—"
    )
    body = (
        f"<p>{status} {link_line} · MC id <code>{mc_user_id}</code> · "
        f"rank {esc(dossier.role or '—')} · credits {credits} · "
        f"contribution {rate} · "
        f"<a href='https://www.missionchief.com/profile/{mc_user_id}' "
        "target='_blank'>game profile ↗</a></p>"
        "<div class='grid2'>"
        f"<div class='panel'><h2>Profile</h2>{profile_html}</div>"
        "<div>"
        f"<div class='panel'><h2>Game sync</h2>{sync_html}</div>"
        f"<div class='panel'><h2>Add note</h2>"
        f"<form method='post' action='/members/{mc_user_id}/note'>"
        "<input name='note' placeholder='Visible in the action log' required>"
        "<button>Add note</button></form></div>"
        f"<div class='panel'><h2>Add sanction</h2>{_sanction_form(mc_user_id)}"
        "</div></div></div>"
        f"<div class='panel'><h2>Sanctions (official warnings: {warnings}/3)"
        "</h2><table><tr><th>#</th><th>Date</th><th>Type</th><th>Reason</th>"
        f"<th>Status</th><th></th></tr>{sanction_lines}</table></div>"
        f"<div class='panel'><h2>Bot actions</h2>"
        f"<ul class='timeline'>{action_lines}</ul></div>"
        f"<div class='panel'><h2>Timeline</h2>"
        f"<ul class='timeline'>{timeline_lines}</ul></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page(dossier.name, body, active="/members", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


async def post_profile(request: web.Request) -> web.Response:
    bot = _bot(request)
    mc_user_id = int(request.match_info["mc_id"])
    link = await LinksRepo(bot.db).get_by_mc(mc_user_id)
    if link is None or link["status"] != "approved":
        _redirect(f"/members/{mc_user_id}", err="No approved Discord link.")
    form = await request.post()
    values = {field: str(form.get(field) or "") for field in PROFILE_FIELDS}
    await MemberProfilesRepo(bot.db).set_fields(
        int(link["discord_id"]), **values
    )
    await bot.log_member_action(
        action="profile_updated", detail=f"via {WEB_ACTOR}",
        discord_user_id=int(link["discord_id"]), mc_user_id=mc_user_id,
    )
    _redirect(f"/members/{mc_user_id}", ok="Profile saved.")


async def post_sanction(request: web.Request) -> web.Response:
    from ..cogs.sanctions import SANCTION_TYPE_KEYS

    bot = _bot(request)
    mc_user_id = int(request.match_info["mc_id"])
    dossier = await DossierService(bot.db).build(mc_user_id)
    if dossier is None:
        raise web.HTTPNotFound(text="Unknown member")
    form = await request.post()
    sanction_type = SANCTION_TYPE_KEYS.get(str(form.get("type") or ""))
    reason = str(form.get("reason") or "").strip()
    if not sanction_type or not reason:
        _redirect(f"/members/{mc_user_id}", err="Type and reason are required.")
    sanction_id = await SanctionsRepo(bot.db).add(
        mc_user_id=mc_user_id, mc_username=dossier.name,
        discord_user_id=dossier.discord_id, admin_discord_id=0,
        admin_name=WEB_ACTOR, sanction_type=sanction_type, reason=reason,
        notes=str(form.get("notes") or "").strip() or None,
    )
    await bot.log_member_action(
        action="sanction_received",
        detail=f"#{sanction_id} {sanction_type} — {reason} (via {WEB_ACTOR})",
        discord_user_id=dossier.discord_id, mc_user_id=mc_user_id,
        actor_name=dossier.name,
    )
    _redirect(f"/members/{mc_user_id}", ok=f"Sanction #{sanction_id} recorded.")


async def post_sanction_revoke(request: web.Request) -> web.Response:
    bot = _bot(request)
    sanction_id = int(request.match_info["sanction_id"])
    form = await request.post()
    back = f"/members/{int(form.get('mc_id') or 0)}" if form.get("mc_id") else "/members"
    if not await SanctionsRepo(bot.db).revoke(sanction_id, revoked_by=WEB_ACTOR):
        _redirect(back, err=f"Sanction #{sanction_id} not found or not active.")
    row = await SanctionsRepo(bot.db).get(sanction_id)
    await bot.log_member_action(
        action="sanction_revoked",
        detail=f"#{sanction_id} (via {WEB_ACTOR})",
        discord_user_id=row["discord_user_id"] if row else None,
        mc_user_id=row["mc_user_id"] if row else None,
        actor_name=row["mc_username"] if row else None,
    )
    _redirect(back, ok=f"Sanction #{sanction_id} revoked.")


async def post_note(request: web.Request) -> web.Response:
    bot = _bot(request)
    mc_user_id = int(request.match_info["mc_id"])
    dossier = await DossierService(bot.db).build(mc_user_id)
    if dossier is None:
        raise web.HTTPNotFound(text="Unknown member")
    form = await request.post()
    note = str(form.get("note") or "").strip()
    if not note:
        _redirect(f"/members/{mc_user_id}", err="Note is empty.")
    await bot.log_member_action(
        action="note_added", detail=f"{note[:300]} (via {WEB_ACTOR})",
        discord_user_id=dossier.discord_id, mc_user_id=mc_user_id,
        actor_name=dossier.name,
    )
    _redirect(f"/members/{mc_user_id}", ok="Note recorded.")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def settings_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    state = StateRepo(bot.db)
    rows = []
    for setting in rt.SETTINGS:
        current = rt.format_value(rt.current(bot.cfg, setting))
        override = await rt.get_override(state, setting)
        rows.append(
            "<tr>"
            f"<td><code>{esc(setting.path)}</code><br>"
            f"<span class='muted'>{esc(setting.description)}</span></td>"
            f"<td>{esc(current)}"
            + (" " + badge("override", "dim") if override is not None else "")
            + ("" if setting.live else " " + badge("restart", "off"))
            + "</td>"
            "<td><form class='inline' method='post' action='/settings'>"
            f"<input type='hidden' name='key' value='{esc(setting.path)}'>"
            f"<input name='value' placeholder='new value' "
            "style='max-width:180px'> "
            "<button class='small'>Apply</button></form></td></tr>"
        )
    body = (
        "<div class='panel'><p class='muted'>The same registry as "
        "<code>!fra set</code>: changes apply in memory and persist as "
        "overrides on top of config.yaml. Settings marked "
        f"{badge('restart', 'off')} only take effect after a restart.</p>"
        "<table><tr><th>Setting</th><th>Current</th><th>Change</th></tr>"
        + "".join(rows) + "</table></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Settings", body, active="/settings", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


async def post_settings(request: web.Request) -> web.Response:
    bot = _bot(request)
    form = await request.post()
    key = str(form.get("key") or "")
    value = str(form.get("value") or "")
    try:
        setting = rt.resolve(key)
        parsed = rt.parse_value(setting, value, bot.cfg)
    except rt.SettingError as exc:
        _redirect("/settings", err=str(exc))
    rt.apply(bot.cfg, setting, parsed)
    await rt.store_override(StateRepo(bot.db), setting, parsed)
    try:
        rt.post_apply(bot, setting)
    except Exception:  # noqa: BLE001 — post-apply hooks are best-effort
        log.warning("web settings: post_apply failed for %s", key, exc_info=True)
    log.info("web settings: %s = %s (via %s)", key, rt.format_value(parsed),
             WEB_ACTOR)
    # Same safety messaging as !fra set: the operator must see when a
    # change is restart-gated, and MUST see when the bot goes live.
    message = f"{key} = {rt.format_value(parsed)}"
    if not setting.live:
        message += " — takes effect after a restart"
    if setting.path == "automation.dry_run" and parsed is False:
        message += (" — WARNING: dry-run is OFF, the bot will now perform "
                    "REAL MissionChief actions")
    _redirect("/settings", ok=message)


# ---------------------------------------------------------------------------
# Images (rendered from the live data, same code as the Discord commands)
# ---------------------------------------------------------------------------

async def _game_sync_cog(request: web.Request):
    cog = _bot(request).get_cog("GameSyncCog")
    if cog is None:
        raise web.HTTPNotFound(text="Game sync not loaded")
    return cog


#: Rendered PNGs cached in memory: the dashboard embeds both images, so
#: every landing-page load would otherwise re-geocode + re-download map
#: tiles from third parties. The data only changes when a member syncs.
_IMAGE_TTL_SECONDS = 600.0
_image_cache: dict[str, tuple[float, bytes]] = {}
_image_locks: dict[str, "asyncio.Lock"] = {}


async def _cached_png(key: str, build) -> bytes | None:
    import asyncio
    import time

    hit = _image_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _IMAGE_TTL_SECONDS:
        return hit[1]
    lock = _image_locks.setdefault(key, asyncio.Lock())
    async with lock:  # single-flight: parallel loads render once
        hit = _image_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _IMAGE_TTL_SECONDS:
            return hit[1]
        png = await build()
        if png is not None:
            _image_cache[key] = (time.monotonic(), png)
        return png


def _png_response(png: bytes | None) -> web.Response:
    if png is None:
        raise web.HTTPNotFound(text="Renderer unavailable")
    return web.Response(body=png, content_type="image/png",
                        headers={"Cache-Control": "private, max-age=600"})


async def infographic_png(request: web.Request) -> web.Response:
    import asyncio
    import datetime as dt

    from ..services.game_sync import (
        cluster_hotspots,
        merge_by_place,
        top_building_types,
        top_vehicle_types,
    )
    from ..services.infographic import AllianceSnapshot, render_infographic

    cog = await _game_sync_cog(request)

    async def build() -> bytes | None:
        (member_coords, building_dicts, vehicle_dicts,
         building_total, vehicle_total) = await cog._sync_stats()
        spots = merge_by_place(await cog._named(
            cluster_hotspots(member_coords, top=24)
        ))
        snapshot = AllianceSnapshot(
            title="Fire & Rescue Academy",
            date_label=dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y"),
            members_synced=len(member_coords),
            building_total=building_total,
            vehicle_total=vehicle_total,
            top_types=top_building_types(building_dicts),
            top_vehicle_types=top_vehicle_types(
                vehicle_dicts, await cog._vehicle_names()
            ),
            spots=spots,
            map_png=await cog._map_image(spots),
        )
        # Pillow work is CPU-bound: keep it off the bot's event loop.
        return await asyncio.to_thread(render_infographic, snapshot)

    return _png_response(await _cached_png("infographic", build))


async def fleet_png(request: web.Request) -> web.Response:
    import asyncio
    import datetime as dt
    import functools

    from ..services.game_sync import top_vehicle_types
    from ..services.infographic import render_fleet_card

    cog = await _game_sync_cog(request)

    async def build() -> bytes | None:
        (member_coords, _, vehicle_dicts,
         _, vehicle_total) = await cog._sync_stats()
        type_ids = set()
        for by_type in vehicle_dicts:
            for key in by_type:
                try:
                    type_ids.add(int(key))
                except (TypeError, ValueError):
                    continue
        return await asyncio.to_thread(functools.partial(
            render_fleet_card,
            title="Fire & Rescue Academy",
            date_label=dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y"),
            members_synced=len(member_coords), vehicle_total=vehicle_total,
            type_count=len(type_ids),
            top_vehicle_types=top_vehicle_types(
                vehicle_dicts, await cog._vehicle_names(), top=10
            ),
        ))

    return _png_response(await _cached_png("fleet", build))


async def health(request: web.Request) -> web.Response:
    return web.Response(
        text=json.dumps({"ok": True}), content_type="application/json"
    )
