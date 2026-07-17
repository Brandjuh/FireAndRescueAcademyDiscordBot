"""Web console: Discord <-> MissionChief identity links (MemberSync).

Read views over ``member_links`` and the ``verify_queue`` retry queue,
plus the two link actions that are pure repository work, reusing the
exact paths the Discord commands use:

* manual link = ``MemberSyncService.approve_manual`` (what ``!link``
  calls): approved-link upsert + queue removal, logged as "verified"
  in the same detail style;
* unlink = ``LinksRepo.delete`` (what ``!unlink`` calls).

The Verified Discord ROLE is only ever touched Discord-side: the
MemberSync cog grants it on ``!link``/``!verify``, and its hourly
prune/restore loop reconciles it against the approved links. The page
says so instead of pretending — a link made here gets the role on the
next restore pass (MC account active in the roster), an unlink made
here leaves the role on the member until an admin removes it in
Discord. No Discord flow creates ``status='denied'`` links, so denied
rows render read-only and there is no deny action here either.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..db.repos import LinksRepo
from ..services.membersync import QUEUE_MAX_ATTEMPTS, MemberSyncService
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page, tile

log = logging.getLogger(__name__)

_LIST_LIMIT = 300

#: reviewer_id sentinel for console-made links: 0 already means "auto"
#: (migration 0008), real admins carry their Discord id — the console
#: needs its own value so the audit trail shows who really decided.
CONSOLE_REVIEWER_ID = -1


def _reviewer_badge(reviewer_id) -> str:
    reviewer_id = int(reviewer_id or 0)
    if reviewer_id == CONSOLE_REVIEWER_ID:
        return badge("console", "dim")
    return badge("auto") if reviewer_id == 0 else badge("admin")

_LINKS_SELECT = (
    "SELECT l.discord_id, l.mc_user_id, l.status, l.reviewer_id, "
    "l.created_at, m.name AS mc_name, m.is_active AS mc_active "
    "FROM member_links l LEFT JOIN members m ON m.mc_user_id = l.mc_user_id "
    "WHERE l.status = ? ORDER BY l.created_at DESC LIMIT ?"
)

_ROLE_NOTE = (
    "The Verified Discord role is only assigned by the bot's Discord-side "
    "flow — never from this console. A link made here gets the role on the "
    "bot's hourly auto-restore pass (once the MC account is active in the "
    "roster); for an instant role grant and a DM to the member, use "
    "<code>!link</code> in Discord."
)


async def _links(bot, status: str) -> list:
    async with bot.db.conn.execute(_LINKS_SELECT, (status, _LIST_LIMIT)) as cur:
        return list(await cur.fetchall())


def _member_cell(row) -> str:
    """MC-side identity: roster name linked to the member dossier, or an
    honest "not in roster" (a link can predate its roster row, or point
    at a typo'd id — the roster join is the only truth we have)."""
    if row["mc_name"] is not None:
        cell = (
            f"<a href='/members/{int(row['mc_user_id'])}'>"
            f"{esc(row['mc_name'])}</a>"
        )
        if not row["mc_active"]:
            cell += " " + badge("left alliance", "off")
        return cell
    return badge("not in roster", "off")


def _unlink_form(discord_id: int, label: str = "Unlink") -> str:
    return (
        "<form class='inline' method='post' action='/verification/unlink'>"
        f"<input type='hidden' name='discord_id' value='{int(discord_id)}'>"
        f"<button class='small ghost'>{esc(label)}</button></form>"
    )


async def verification_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    approved = await _links(bot, "approved")
    denied = await _links(bot, "denied")
    queued = await LinksRepo(bot.db).queue_all()

    approved_lines = "".join(
        "<tr>"
        f"<td><code>{int(row['discord_id'])}</code></td>"
        f"<td>{_member_cell(row)}</td>"
        f"<td><code>{int(row['mc_user_id'])}</code></td>"
        f"<td>{_reviewer_badge(row['reviewer_id'])}</td>"
        f"<td>{esc(str(row['created_at'])[:16])}</td>"
        f"<td>{_unlink_form(row['discord_id'])}</td>"
        "</tr>"
        for row in approved
    ) or "<tr><td colspan='6' class='muted'>No approved links yet.</td></tr>"

    denied_lines = "".join(
        "<tr>"
        f"<td><code>{int(row['discord_id'])}</code></td>"
        f"<td>{_member_cell(row)}</td>"
        f"<td><code>{int(row['mc_user_id'])}</code></td>"
        f"<td>{esc(str(row['created_at'])[:16])}</td>"
        f"<td>{_unlink_form(row['discord_id'], 'Remove')}</td>"
        "</tr>"
        for row in denied
    ) or "<tr><td colspan='5' class='muted'>No denied links.</td></tr>"

    queue_lines = "".join(
        "<tr>"
        f"<td><code>{int(row['discord_id'])}</code></td>"
        f"<td>{esc(row['display_name'] or '—')}</td>"
        f"<td>{esc(row['mc_user_id'] if row['mc_user_id'] else '—')}</td>"
        f"<td>{int(row['attempts'])}/{QUEUE_MAX_ATTEMPTS}</td>"
        f"<td>{esc(str(row['enqueued_at'])[:16])}</td>"
        "</tr>"
        for row in queued
    ) or "<tr><td colspan='5' class='muted'>Verification queue is empty.</td></tr>"

    tiles = (
        tile("Approved links", len(approved))
        + tile("Denied links", len(denied))
        + tile("In retry queue", len(queued))
    )
    body = (
        f"<div class='tiles'>{tiles}</div>"
        "<div class='grid2'>"
        "<div class='panel'><h2>Manual link</h2>"
        f"<p>{_ROLE_NOTE}</p>"
        "<form method='post' action='/verification/link'>"
        "<label>Discord user id</label>"
        "<input name='discord_id' required placeholder='e.g. 123456789012345678'>"
        "<label>MC user id</label>"
        "<input name='mc_user_id' required placeholder='e.g. 424242'>"
        "<button>Link &amp; verify</button></form>"
        "<p class='muted'>Same repo path as <code>!link</code>: approved link, "
        "queue entry removed, a &quot;verified&quot; action logged. Re-linking "
        "an already-claimed MC id moves the claim (people re-verify after "
        "renames).</p></div>"
        "<div class='panel'><h2>Verification queue</h2>"
        "<table><tr><th>Discord id</th><th>Display name</th><th>MC hint</th>"
        f"<th>Attempts</th><th>Enqueued</th></tr>{queue_lines}</table>"
        "<p class='muted'>Retried automatically every 2 minutes by the bot; "
        f"entries expire after {QUEUE_MAX_ATTEMPTS} attempts (~90 minutes). "
        "A manual link above removes the member from this queue.</p></div>"
        "</div>"
        "<div class='panel'><h2>Approved links</h2>"
        "<table><tr><th>Discord id</th><th>MC member</th><th>MC id</th>"
        "<th>Verified by</th><th>Since</th><th></th></tr>"
        f"{approved_lines}</table>"
        f"<p class='muted'>{len(approved)} link(s)"
        f"{' (capped)' if len(approved) == _LIST_LIMIT else ''}. Unlink "
        "removes the stored link only — the Verified role stays until an "
        "admin removes it in Discord (<code>!unlink</code> there does both)."
        "</p></div>"
        "<div class='panel'><h2>Denied links</h2>"
        "<table><tr><th>Discord id</th><th>MC member</th><th>MC id</th>"
        f"<th>Since</th><th></th></tr>{denied_lines}</table>"
        "<p class='muted'>No bot flow denies links today; anything listed "
        "here was written by hand and does not block re-verification.</p>"
        "</div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Verification", body, active="/verification", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


async def post_link(request: web.Request) -> web.Response:
    bot = _bot(request)
    form = await request.post()
    try:
        discord_id = int(str(form.get("discord_id") or "").strip())
        mc_user_id = int(str(form.get("mc_user_id") or "").strip())
        if discord_id <= 0 or mc_user_id <= 0:
            raise ValueError
    except ValueError:
        _redirect("/verification",
                  err="Discord id and MC id must be positive numbers.")
    # Roster lookup is informational only: !link does not require roster
    # presence either (a fresh join predates its roster row by a sweep).
    async with bot.db.conn.execute(
        "SELECT name FROM members WHERE mc_user_id = ?", (mc_user_id,)
    ) as cur:
        member = await cur.fetchone()
    # The exact repo path !link uses: approved upsert + queue removal.
    # reviewer_id 0 is the AUTO-verified sentinel (migration 0008); a
    # console decision must stay distinguishable in the audit trail, so
    # it gets its own sentinel.
    await MemberSyncService(bot.db).approve_manual(
        discord_id, mc_user_id, reviewer_id=CONSOLE_REVIEWER_ID
    )
    await bot.log_member_action(
        action="verified",
        detail=f"manually linked to MC {mc_user_id} (via {WEB_ACTOR})",
        discord_user_id=discord_id, mc_user_id=mc_user_id,
        actor_name=member["name"] if member is not None else None,
    )
    who = (
        member["name"] if member is not None
        else "not in the alliance roster — check the id"
    )
    _redirect(
        "/verification",
        ok=(f"Linked Discord {discord_id} to MC {mc_user_id} ({who}). "
            "The Verified role follows via the bot's Discord-side flow."),
    )


async def post_unlink(request: web.Request) -> web.Response:
    bot = _bot(request)
    form = await request.post()
    try:
        discord_id = int(str(form.get("discord_id") or "").strip())
    except ValueError:
        _redirect("/verification", err="Invalid Discord id.")
    links = LinksRepo(bot.db)
    row = await links.get_by_discord(discord_id)
    if row is None:
        _redirect("/verification",
                  err=f"No link on record for Discord {discord_id}.")
    await links.delete(discord_id)
    await bot.log_member_action(
        action="unlinked",
        detail=f"MC link to {row['mc_user_id']} removed (via {WEB_ACTOR})",
        discord_user_id=discord_id, mc_user_id=row["mc_user_id"],
    )
    _redirect(
        "/verification",
        ok=(f"Unlinked Discord {discord_id}. The Verified role, if granted, "
            "must still be removed in Discord."),
    )


ROUTES = [
    web.get("/verification", verification_page),
    web.post("/verification/link", post_link),
    web.post("/verification/unlink", post_unlink),
]
NAV_ENTRY = ("/verification", "Verification")
