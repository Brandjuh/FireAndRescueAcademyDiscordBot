"""Web console: the sanctions register.

The full register with filters, a detail view with the audit history,
and the same actions the Discord side offers: add (CoC catalogue or
free text), edit, revoke, and approve/dismiss for unverified game-log
imports. Mutations run through ``bot.sanction_service`` when the live
bot provides it — that is the path with REAL mute execution and early
unmute on revoke — and fall back to the bare repo otherwise (tests,
partial boots), which records but never touches the game.

This module also owns the member-page sanction actions (the "Add
sanction" form and the per-row Revoke button POST here), moved out of
the core handlers so every sanction mutation lives in one place.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..cogs.sanctions import SANCTION_TYPE_KEYS
from ..db.repos import SanctionsRepo
from ..services.dossier import DossierService
from ..services.sanction_rules import (
    COC_RULES,
    advice_for,
    effective_status,
)
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page, tile

log = logging.getLogger(__name__)

NAV_ENTRY = ("/sanctions", "Sanctions")

_LIMIT = 300
_STATUS_KINDS = {
    "active": "off", "unverified": "warn", "expired": "dim",
    "revoked": "dim", "dismissed": "dim",
}
#: Stored statuses offered in the filter (display adds derived expiry).
_STATUSES = ("active", "unverified", "expired", "revoked", "dismissed")
_SOURCES = ("manual", "panel", "web", "game_log", "tax", "escalation")


def _status_badge(row) -> str:
    status = effective_status(row)
    return badge(status, _STATUS_KINDS.get(status, "dim"))


def _service(bot):
    return getattr(bot, "sanction_service", None)


async def _issue(bot, **kwargs) -> tuple[int, str | None]:
    """(sanction_id, mute note) via the service when the bot has one
    (real mutes), else the bare repo (record only)."""
    service = _service(bot)
    if service is not None:
        result = await service.issue(**kwargs)
        return result["sanction_id"], result["mute_note"]
    from ..services.sanction_rules import mute_expiry

    repo = SanctionsRepo(bot.db)
    sanction_id = await repo.add(
        mc_user_id=kwargs["mc_user_id"], mc_username=kwargs["mc_username"],
        discord_user_id=kwargs["discord_user_id"],
        admin_discord_id=kwargs["admin_discord_id"],
        admin_name=kwargs["admin_name"],
        sanction_type=kwargs["sanction_type"], reason=kwargs["reason"],
        notes=kwargs.get("notes"),
        reason_category=kwargs.get("reason_category"),
        source=kwargs.get("source", "web"),
        expires_at=mute_expiry(kwargs["sanction_type"]),
    )
    return sanction_id, None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def _filter_form(query: dict) -> str:
    status_opts = "<option value=''>any status</option>" + "".join(
        f"<option value='{s}'{' selected' if query.get('status') == s else ''}>"
        f"{s}</option>"
        for s in _STATUSES
    )
    type_opts = "<option value=''>any type</option>" + "".join(
        f"<option value='{esc(label)}'"
        f"{' selected' if query.get('type') == label else ''}>{esc(label)}"
        "</option>"
        for label in SANCTION_TYPE_KEYS.values()
    )
    source_opts = "<option value=''>any source</option>" + "".join(
        f"<option value='{s}'{' selected' if query.get('source') == s else ''}>"
        f"{s}</option>"
        for s in _SOURCES
    )
    member = esc(query.get("member") or "")
    return (
        "<form class='inline' method='get' action='/sanctions'>"
        f"<select name='status'>{status_opts}</select> "
        f"<select name='type'>{type_opts}</select> "
        f"<select name='source'>{source_opts}</select> "
        f"<input name='member' placeholder='member name or MC id' "
        f"value='{member}'> "
        "<button class='small'>Filter</button></form>"
    )


def _add_form() -> str:
    rule_opts = "<option value=''>— free text below —</option>" + "".join(
        f"<option value='{rule.code}'>{rule.code} — {esc(rule.title)}"
        "</option>"
        for rule in COC_RULES.values()
    )
    type_opts = "".join(
        f"<option value='{esc(label)}'>{esc(label)}</option>"
        for label in SANCTION_TYPE_KEYS.values()
    )
    return (
        "<form method='post' action='/sanctions/new'>"
        "<label>Member (MC name or id)</label>"
        "<input name='member' required>"
        f"<label>CoC rule</label><select name='rule'>{rule_opts}</select>"
        "<label>Free-text reason (used when no rule is picked)</label>"
        "<input name='reason'>"
        f"<label>Type</label><select name='type'>{type_opts}</select>"
        "<label>Notes (optional)</label><input name='notes'>"
        "<button>Record sanction</button></form>"
        "<p class='muted'>The advised sanction per rule shows on the "
        "detail page; timed mutes get a real expiry.</p>"
    )


async def sanctions_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    repo = SanctionsRepo(bot.db)
    query = {
        key: (request.query.get(key) or "").strip() or None
        for key in ("status", "type", "source", "member")
    }
    rows = await repo.filtered(
        status=query.get("status"), sanction_type=query.get("type"),
        source=query.get("source"), member=query.get("member"),
        limit=_LIMIT,
    )
    summary = await repo.status_summary()
    tiles = "".join(
        tile(status, summary.get(status, 0)) for status in _STATUSES
    )
    lines = "".join(
        "<tr>"
        f"<td><a href='/sanctions/{row['id']}'>#{row['id']}</a></td>"
        f"<td>{esc(row['created_at'][:10])}</td>"
        f"<td>"
        + (
            f"<a href='/members/{row['mc_user_id']}'>"
            f"{esc(row['mc_username'] or '?')}</a>"
            if row["mc_user_id"] else esc(row["mc_username"] or "?")
        )
        + "</td>"
        f"<td>{esc(row['sanction_type'])}</td>"
        f"<td>{esc((row['reason'] or '')[:80])}</td>"
        f"<td>{esc(row['source'])}</td>"
        f"<td>{_status_badge(row)}</td>"
        "</tr>"
        for row in rows
    ) or "<tr><td colspan='7' class='muted'>No sanctions match.</td></tr>"
    body = (
        f"<div class='tiles'>{tiles}</div>"
        f"<div class='panel'><h2>Filter</h2>{_filter_form(query)}</div>"
        f"<div class='panel'><h2>Register ({len(rows)})</h2>"
        "<table><tr><th>#</th><th>Date</th><th>Member</th><th>Type</th>"
        f"<th>Reason</th><th>Source</th><th>Status</th></tr>{lines}</table>"
        "</div>"
        f"<div class='panel'><h2>Record a sanction</h2>{_add_form()}</div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Sanctions", body, active="/sanctions", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def _edit_form(row) -> str:
    type_opts = "".join(
        f"<option value='{esc(label)}'"
        f"{' selected' if row['sanction_type'] == label else ''}>"
        f"{esc(label)}</option>"
        for label in SANCTION_TYPE_KEYS.values()
    )
    rule_opts = "<option value=''>— none —</option>" + "".join(
        f"<option value='{rule.code}'"
        f"{' selected' if row['reason_category'] == rule.code else ''}>"
        f"{rule.code} — {esc(rule.title)}</option>"
        for rule in COC_RULES.values()
    )
    return (
        f"<form method='post' action='/sanctions/{row['id']}/edit'>"
        f"<label>Type</label><select name='type'>{type_opts}</select>"
        f"<label>CoC rule</label><select name='rule'>{rule_opts}</select>"
        "<label>Reason</label>"
        f"<input name='reason' value='{esc(row['reason'] or '')}' required>"
        "<label>Notes</label>"
        f"<input name='notes' value='{esc(row['notes'] or '')}'>"
        "<button>Save changes</button></form>"
    )


async def sanction_detail(request: web.Request) -> web.Response:
    bot = _bot(request)
    repo = SanctionsRepo(bot.db)
    sanction_id = int(request.match_info["sanction_id"])
    row = await repo.get(sanction_id)
    if row is None:
        raise web.HTTPNotFound(text="Unknown sanction")
    status = effective_status(row)
    advice = advice_for(row["reason_category"])
    history = await repo.history(sanction_id)
    history_lines = "".join(
        f"<li><span class='muted'>{esc(h['created_at'][:16])}</span> "
        f"<strong>{esc(h['action'])}</strong> — {esc(h['actor'])}"
        + (f" <span class='soft'>{esc(h['detail'])}</span>" if h["detail"] else "")
        + "</li>"
        for h in history
    ) or "<li class='muted'>No history recorded.</li>"

    facts = [
        f"Member: <strong>{esc(row['mc_username'] or '?')}</strong>"
        + (f" (MC <code>{row['mc_user_id']}</code>)" if row["mc_user_id"] else ""),
        f"Status: {_status_badge(row)} · source <code>{esc(row['source'])}</code>",
        f"Recorded by {esc(row['admin_name'])} on "
        f"{esc(row['created_at'][:16])}",
    ]
    if row["expires_at"]:
        facts.append(f"Expires: {esc(row['expires_at'][:16])}")
    if row["edited_at"]:
        facts.append(
            f"Edited by {esc(row['edited_by'] or '?')} on "
            f"{esc(row['edited_at'][:16])}"
        )
    if row["revoked_at"]:
        facts.append(
            f"Settled by {esc(row['revoked_by'] or '?')} on "
            f"{esc(row['revoked_at'][:16])}"
        )
    if advice:
        facts.append(
            f"CoC advice for rule {esc(row['reason_category'])}: "
            f"<strong>{esc(advice[0])}</strong> — {esc(advice[1])}"
        )

    actions = []
    if row["status"] == "unverified":
        actions.append(
            f"<form class='inline' method='post' "
            f"action='/sanctions/{sanction_id}/resolve'>"
            "<input type='hidden' name='action' value='approve'>"
            "<button class='small'>Approve</button></form> "
            f"<form class='inline' method='post' "
            f"action='/sanctions/{sanction_id}/resolve'>"
            "<input type='hidden' name='action' value='dismiss'>"
            "<button class='small ghost'>Dismiss</button></form>"
        )
    if row["status"] == "active":
        actions.append(
            f"<form class='inline' method='post' "
            f"action='/sanctions/{sanction_id}/revoke'>"
            "<button class='small ghost'>Revoke</button></form>"
        )

    body = (
        f"<p><a href='/sanctions'>← register</a></p>"
        f"<div class='panel'><h2>{esc(row['sanction_type'])} — "
        f"{esc((row['reason'] or '')[:120])}</h2>"
        + "".join(f"<p>{fact}</p>" for fact in facts)
        + (f"<p>Notes: {esc(row['notes'])}</p>" if row["notes"] else "")
        + (f"<p>{' '.join(actions)}</p>" if actions else "")
        + "</div>"
        f"<div class='grid2'>"
        f"<div class='panel'><h2>Edit</h2>{_edit_form(row)}</div>"
        f"<div class='panel'><h2>History</h2>"
        f"<ul class='timeline'>{history_lines}</ul></div>"
        "</div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page(f"Sanction #{sanction_id}", body, active="/sanctions",
                  flash=flash, flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

async def post_new_sanction(request: web.Request) -> web.Response:
    bot = _bot(request)
    form = await request.post()
    member = str(form.get("member") or "").strip()
    sanction_type = str(form.get("type") or "")
    if sanction_type not in SANCTION_TYPE_KEYS.values():
        _redirect("/sanctions", err="Unknown sanction type.")
    rule_code = str(form.get("rule") or "").strip() or None
    if rule_code is not None and rule_code not in COC_RULES:
        _redirect("/sanctions", err="Unknown CoC rule.")
    reason = str(form.get("reason") or "").strip()
    if rule_code:
        rule = COC_RULES[rule_code]
        reason = f"{rule_code}. {rule.title}"
    if not member or not reason:
        _redirect("/sanctions", err="Member and a rule or reason are required.")
    candidates = await DossierService(bot.db).search(member)
    if not candidates:
        _redirect("/sanctions", err=f"No member found for '{member}'.")
    if len(candidates) > 1 and candidates[0].score <= candidates[1].score:
        _redirect(
            "/sanctions",
            err=f"'{member}' is ambiguous — use the exact name or MC id.",
        )
    target = candidates[0]
    sanction_id, mute_note = await _issue(
        bot,
        mc_user_id=target.mc_user_id, mc_username=target.name,
        discord_user_id=target.discord_id, admin_discord_id=0,
        admin_name=WEB_ACTOR, sanction_type=sanction_type, reason=reason,
        reason_category=rule_code,
        notes=str(form.get("notes") or "").strip() or None, source="web",
    )
    await bot.log_member_action(
        action="sanction_received",
        detail=f"#{sanction_id} {sanction_type} — {reason[:120]} "
               f"(via {WEB_ACTOR})",
        discord_user_id=target.discord_id, mc_user_id=target.mc_user_id,
        actor_name=target.name,
    )
    note = f" ({mute_note})" if mute_note else ""
    _redirect(
        f"/sanctions/{sanction_id}",
        ok=f"Sanction #{sanction_id} recorded for {target.name}.{note}",
    )


async def post_edit_sanction(request: web.Request) -> web.Response:
    from ..services.sanction_rules import mute_expiry

    bot = _bot(request)
    repo = SanctionsRepo(bot.db)
    sanction_id = int(request.match_info["sanction_id"])
    row = await repo.get(sanction_id)
    if row is None:
        raise web.HTTPNotFound(text="Unknown sanction")
    form = await request.post()
    sanction_type = str(form.get("type") or "")
    if sanction_type not in SANCTION_TYPE_KEYS.values():
        _redirect(f"/sanctions/{sanction_id}", err="Unknown sanction type.")
    rule_code = str(form.get("rule") or "").strip() or None
    if rule_code is not None and rule_code not in COC_RULES:
        _redirect(f"/sanctions/{sanction_id}", err="Unknown CoC rule.")
    reason = str(form.get("reason") or "").strip()
    if not reason:
        _redirect(f"/sanctions/{sanction_id}", err="Reason is required.")
    kwargs: dict = {
        "sanction_type": sanction_type, "reason": reason,
        "notes": str(form.get("notes") or "").strip() or None,
    }
    if rule_code is not None:
        kwargs["reason_category"] = rule_code
    if sanction_type != row["sanction_type"]:
        expires = mute_expiry(sanction_type)
        if expires is not None:
            kwargs["expires_at"] = expires
    await repo.edit(sanction_id, actor=WEB_ACTOR, **kwargs)
    _redirect(f"/sanctions/{sanction_id}", ok="Sanction updated.")


async def post_resolve_sanction(request: web.Request) -> web.Response:
    bot = _bot(request)
    repo = SanctionsRepo(bot.db)
    sanction_id = int(request.match_info["sanction_id"])
    form = await request.post()
    confirm = str(form.get("action") or "") == "approve"
    if not await repo.resolve_review(
        sanction_id, confirm=confirm, by=WEB_ACTOR,
    ):
        _redirect(
            f"/sanctions/{sanction_id}",
            err="Not an unverified sanction — nothing changed.",
        )
    row = await repo.get(sanction_id)
    if confirm and row is not None:
        await bot.log_member_action(
            action="sanction_received",
            detail=f"#{sanction_id} {row['sanction_type']} — "
                   f"{row['reason'][:120]} (game log, approved via "
                   f"{WEB_ACTOR})",
            discord_user_id=row["discord_user_id"],
            mc_user_id=row["mc_user_id"], actor_name=row["mc_username"],
        )
    verb = "approved" if confirm else "dismissed"
    _redirect(f"/sanctions/{sanction_id}", ok=f"Sanction {verb}.")


async def post_revoke_sanction(request: web.Request) -> web.Response:
    """Revoke — shared by the register page and the member page (the
    member page passes ``mc_id`` to land back there)."""
    bot = _bot(request)
    repo = SanctionsRepo(bot.db)
    sanction_id = int(request.match_info["sanction_id"])
    form = await request.post()
    back = (
        f"/members/{int(form.get('mc_id') or 0)}"
        if form.get("mc_id") else f"/sanctions/{sanction_id}"
    )
    service = _service(bot)
    if service is not None:
        ok, note = await service.revoke(sanction_id, revoked_by=WEB_ACTOR)
        if not ok:
            _redirect(back, err=f"Sanction #{sanction_id}: {note}")
    elif not await repo.revoke(sanction_id, revoked_by=WEB_ACTOR):
        _redirect(back, err=f"Sanction #{sanction_id} not found or not active.")
    row = await repo.get(sanction_id)
    await bot.log_member_action(
        action="sanction_revoked",
        detail=f"#{sanction_id} (via {WEB_ACTOR})",
        discord_user_id=row["discord_user_id"] if row else None,
        mc_user_id=row["mc_user_id"] if row else None,
        actor_name=row["mc_username"] if row else None,
    )
    _redirect(back, ok=f"Sanction #{sanction_id} revoked.")


async def post_member_sanction(request: web.Request) -> web.Response:
    """The member page's quick "Add sanction" form (kept for one-click
    use from a dossier; the register page has the full form)."""
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
    sanction_id, mute_note = await _issue(
        bot,
        mc_user_id=mc_user_id, mc_username=dossier.name,
        discord_user_id=dossier.discord_id, admin_discord_id=0,
        admin_name=WEB_ACTOR, sanction_type=sanction_type, reason=reason,
        notes=str(form.get("notes") or "").strip() or None, source="web",
    )
    await bot.log_member_action(
        action="sanction_received",
        detail=f"#{sanction_id} {sanction_type} — {reason} (via {WEB_ACTOR})",
        discord_user_id=dossier.discord_id, mc_user_id=mc_user_id,
        actor_name=dossier.name,
    )
    note = f" ({mute_note})" if mute_note else ""
    _redirect(
        f"/members/{mc_user_id}", ok=f"Sanction #{sanction_id} recorded.{note}",
    )


ROUTES = [
    web.get("/sanctions", sanctions_page),
    web.get("/sanctions/{sanction_id:\\d+}", sanction_detail),
    web.post("/sanctions/new", post_new_sanction),
    web.post("/sanctions/{sanction_id:\\d+}/edit", post_edit_sanction),
    web.post("/sanctions/{sanction_id:\\d+}/resolve", post_resolve_sanction),
    web.post("/sanctions/{sanction_id:\\d+}/revoke", post_revoke_sanction),
    web.post("/members/{mc_id:\\d+}/sanctions", post_member_sanction),
]
