"""Web console: the alliance chat bridge (game ↔ Discord).

Read side: chat messages are never stored in the DB — the bridge is a
live feed, not an archive (the reference chatmanager kept none either;
only the watermark, echo memory and learned own-account name persist).
The page therefore shows the CURRENT game history through the same
``ChatSyncService.fetch_history`` call the Discord cog and the
``!fra chatbridge`` status command use — one paced client, one circuit
breaker — and a fetch failure renders as a notice, never a 500.

Write side: ``POST /chat/send`` relays through the SAME
``ChatSyncService.send_from_discord`` call as the Discord→game message
listener. The ``[Web console] text`` prefix mirrors the bridge's
``[DiscordName] text`` format, and the ≥30 s anti-spam spacing, the
live chat-form/CSRF fetch, the echo memory (so the poll cannot bounce
the message back into the bridge channel) and the pacer all live in
that one path — nothing here talks to MissionChief directly. The
handler mirrors the cog's gates: bridge disabled → refused, dry_run →
nothing is posted (the cog reacts 🚫 instead of relaying). Unlike the
cog it does not require the Discord bridge channel — that requirement
exists because the cog *listens* there; the relay itself needs none.
"""

from __future__ import annotations

import datetime as dt
import logging

from aiohttp import web

from ..mc.errors import MissionChiefError
from ..services.chat_sync import MIN_MC_POST_INTERVAL_SECONDS
from .handlers import WEB_ACTOR, _bot, _flash, _redirect
from .html import badge, esc, page

log = logging.getLogger(__name__)

#: The page shows at most this many messages; the game page itself only
#: renders the recent history, typically fewer.
_HISTORY_LIMIT = 100


def _when(raw: str) -> str:
    """Game timestamps are ISO-with-offset (may be empty) — render UTC."""
    if not raw:
        return "—"
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

async def chat_page(request: web.Request) -> web.Response:
    bot = _bot(request)
    chat = getattr(bot, "chat_sync", None)
    chat_cfg = bot.cfg.automation.chat
    channel_id = int(getattr(bot.cfg.discord.channels, "chat_bridge", 0) or 0)

    last_seen = 0
    own_account = None
    messages: list = []
    fetch_error: str | None = None
    if chat is not None:
        last_seen = await chat.last_seen()
        own_account = await chat.own_account()
        try:
            messages = await chat.fetch_history()
        except MissionChiefError as exc:
            fetch_error = str(exc)

    # -- bridge status ------------------------------------------------------
    if chat is None:
        watermark_html = badge("service not running", "off")
    elif last_seen <= 0:
        watermark_html = (
            "<code>0</code> <span class='muted'>— baseline pending: the "
            "next pass marks the current history seen, then only NEW game "
            "messages mirror to Discord</span>"
        )
    else:
        watermark_html = f"<code>{last_seen}</code>"
    # The poll loop lives in the Discord cog; a console without it (tests,
    # partial boots) simply reports the loop as not running.
    cog = bot.get_cog("ChatBridgeCog") if hasattr(bot, "get_cog") else None
    loop = getattr(cog, "sync_loop", None)
    loop_html = (
        badge("running", "ok")
        if loop is not None and loop.is_running()
        else badge("not running", "off")
    )
    status_rows = (
        ("Bridge",
         badge("enabled", "ok") if chat_cfg.enabled
         else badge("disabled", "off")
         + " <span class='muted'>automation.chat.enabled</span>"),
        ("Dry run",
         badge("ON — game posts are simulated", "off")
         if bot.cfg.automation.dry_run else badge("off", "ok")),
        ("Bridge channel",
         f"<code>{channel_id}</code>" if channel_id
         else badge("not set", "off")
         + " <span class='muted'>discord.channels.chat_bridge</span>"),
        ("Poll interval", f"{int(chat_cfg.interval_seconds)} s"),
        ("Poll loop", loop_html),
        ("Last seen chat id", watermark_html),
        ("Own game account",
         esc(own_account) if own_account
         else "<span class='muted'>not learned yet — learned from the "
              "first relayed message</span>"),
    )
    status_html = "".join(
        f"<tr><th>{esc(label)}</th><td>{value}</td></tr>"
        for label, value in status_rows
    )

    # -- send form ----------------------------------------------------------
    send_form = (
        "<form method='post' action='/chat/send'>"
        "<label>Message</label>"
        "<input name='message' maxlength='1000' "
        "placeholder='Appears in the game as [Web console] …' required>"
        "<button>Send to game chat</button></form>"
        "<p class='muted'>Relayed exactly like the Discord bridge: "
        "prefixed as [Web console], spaced at least "
        f"{int(MIN_MC_POST_INTERVAL_SECONDS)} s between game posts — a "
        "send may take up to that long to complete.</p>"
    )

    # -- history ------------------------------------------------------------
    notes = []
    if chat is None:
        notes.append(
            "Chat service is not running in this process — no live history."
        )
    elif fetch_error is not None:
        notes.append(
            f"Live fetch failed: {fetch_error} — the next poll retries."
        )
    else:
        notes.append(
            f"{len(messages)} message(s) on the game page — a live fetch; "
            "chat history is not stored in the database."
        )
        if messages and last_seen > 0:
            fresh = sum(1 for m in messages if m.chat_id > last_seen)
            if fresh:
                notes.append(
                    f"{fresh} newer than the watermark — they mirror to "
                    "Discord on the next pass."
                )

    lines = []
    for message in reversed(messages[-_HISTORY_LIMIT:]):
        author = esc(message.username)
        if message.mc_user_id:
            author = (
                "<a href='https://www.missionchief.com/profile/"
                f"{int(message.mc_user_id)}' target='_blank'>{author}</a>"
            )
        if chat is not None and await chat.is_own_account(message.username):
            author += " " + badge("own account")
        text_html = esc(message.message).replace("\n", "<br>")
        lines.append(
            "<tr>"
            f"<td class='soft'>{esc(_when(message.timestamp))}</td>"
            f"<td>{author}</td>"
            f"<td>{text_html}</td>"
            f"<td class='muted'>#{int(message.chat_id)}</td>"
            "</tr>"
        )
    table = "".join(lines) or (
        "<tr><td colspan='4' class='muted'>No messages.</td></tr>"
    )

    body = (
        "<div class='grid2'>"
        f"<div class='panel'><h2>Bridge status</h2><table>{status_html}"
        "</table></div>"
        f"<div class='panel'><h2>Send to alliance chat</h2>{send_form}</div>"
        "</div>"
        "<div class='panel'><h2>Alliance chat — live history "
        "(newest first)</h2>"
        f"<p class='muted'>{esc(' '.join(notes))}</p>"
        "<table><tr><th>Time</th><th>Author</th><th>Message</th><th>Id</th>"
        f"</tr>{table}</table></div>"
    )
    flash, is_err = _flash(request)
    return web.Response(
        text=page("Chat", body, active="/chat", flash=flash,
                  flash_error=is_err),
        content_type="text/html",
    )


# ---------------------------------------------------------------------------
# Send (the Discord→game relay path, verbatim)
# ---------------------------------------------------------------------------

async def post_chat_send(request: web.Request) -> web.Response:
    bot = _bot(request)
    chat = getattr(bot, "chat_sync", None)
    form = await request.post()
    body = str(form.get("message") or "").strip()
    if not body:
        _redirect("/chat", err="Message is empty.")
    if chat is None:
        _redirect("/chat", err="Chat service is not running in this process.")
    if not bot.cfg.automation.chat.enabled:
        _redirect(
            "/chat",
            err="The chat bridge is disabled — enable "
                "automation.chat.enabled first.",
        )
    if bot.cfg.automation.dry_run:
        # Same as the cog's 🚫 reaction: acknowledged, nothing posted.
        log.info("web chat: dry-run, NOT relaying %r", body[:80])
        _redirect(
            "/chat",
            ok="Dry-run is ON — the message was NOT sent to the game.",
        )
    try:
        sent = await chat.send_from_discord(WEB_ACTOR, body)
    except (MissionChiefError, ValueError) as exc:
        _redirect("/chat", err=f"Send failed: {exc}")
    await bot.log_member_action(
        action="chat_message_sent",
        detail=f"{sent[:200]} (via {WEB_ACTOR})",
        actor_name=WEB_ACTOR,
    )
    _redirect("/chat", ok="Message sent to the alliance chat.")


ROUTES = [
    web.get("/chat", chat_page),
    web.post("/chat/send", post_chat_send),
]
NAV_ENTRY = ("/chat", "Chat")
