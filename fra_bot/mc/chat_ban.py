"""Set / remove an in-game chat ban (alliance moderation action).

⚠️ ROUTE UNVERIFIED. The reference cogs only OBSERVE chat bans in the
alliance log; none of them ever sets one, so the exact route could not
be ported and missionchief.com is unreachable from this sandbox. The
paths below are candidates modeled on the verified kick route
(``/verband/kick/<id>``). Until an admin confirms them on the live bot
(page-dump diagnostics on a test mute), execution stays behind the
``automation.sanctions.mute_execution_enabled`` switch (default OFF):
mutes are then registered + announced but no game request is made.

The game lifts a TIMED chat ban by itself — the bot never needs to
schedule an unmute; ``remove_chat_ban`` exists only for early lifting
(a revoked mute). Requires "Moderator action" rights on the bot account.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from .errors import MissionChiefError

log = logging.getLogger(__name__)

#: Candidate routes — VERIFY ON THE LIVE BOT before enabling execution.
CHAT_BAN_PATH = "/verband/chat_ban/{mc_user_id}"
CHAT_BAN_REMOVE_PATH = "/verband/chat_ban_remove/{mc_user_id}"

#: Form-field name fragments that smell like a ban-duration input.
_DURATION_HINTS = ("duration", "time", "until", "hour", "minute", "day")


def parse_chat_ban_form(html: str) -> tuple[str, dict, object] | None:
    """(action, payload, form soup) of a chat-ban form on the page, if
    any — same contract as the kick route's confirmation-form parser."""
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "chat_ban" not in action.lower():
            continue
        payload: dict[str, str] = {}
        for tag in form.find_all(("input", "select", "textarea")):
            name = tag.get("name")
            if not name or tag.get("type") in ("submit", "button"):
                continue
            payload[name] = tag.get("value") or ""
        return action, payload, form
    return None


def _apply_duration(form, payload: dict, duration_minutes: int | None) -> str:
    """Best-effort: pick the form's duration option closest to (without
    exceeding, when possible) the requested minutes. Returns a short
    note for the admin summary."""
    if duration_minutes is None:
        return ""
    for select in form.find_all("select"):
        name = (select.get("name") or "").lower()
        if not any(hint in name for hint in _DURATION_HINTS):
            continue
        options = []
        for option in select.find_all("option"):
            value = option.get("value")
            if value is None:
                continue
            try:
                options.append((int(value), value))
            except ValueError:
                continue
        if not options:
            return " (duration select had no numeric options — game default used)"
        options.sort()
        chosen = options[0]
        for numeric, raw in options:
            if numeric <= duration_minutes:
                chosen = (numeric, raw)
        payload[select.get("name")] = chosen[1]
        return f" (duration option {chosen[0]} chosen for {duration_minutes}m)"
    for name in payload:
        if any(hint in name.lower() for hint in _DURATION_HINTS):
            payload[name] = str(duration_minutes)
            return f" (duration field '{name}' set to {duration_minutes}m)"
    return " (no duration field found — game default used)"


async def _submit(client, path: str, *, duration_minutes: int | None,
                  verb: str) -> tuple[bool, str]:
    try:
        html = await client.fetch_page(path, ajax=True)
    except MissionChiefError as exc:
        return False, f"{verb} request failed ({exc})"
    parsed = parse_chat_ban_form(html)
    if parsed is None:
        # Like the kick route: no form usually means the action was
        # performed directly. The log-verification pass proves it.
        return True, f"{verb} accepted (no confirmation form shown)"
    action, payload, form = parsed
    note = _apply_duration(form, payload, duration_minutes)
    try:
        status, _, _ = await client.post_form(
            action, payload, referer=client.url(path)
        )
    except MissionChiefError as exc:
        return False, f"{verb} confirmation failed ({exc})"
    if status >= 400:
        return False, f"{verb} confirmation rejected (HTTP {status})"
    return True, f"{verb} confirmed{note}"


async def set_chat_ban(
    client, mc_user_id: int, *, duration_minutes: int | None = None
) -> tuple[bool, str]:
    """Set a chat ban; (ok, detail). The alliance-log sync must show a
    ``chat_ban_set`` row shortly after — callers verify that."""
    return await _submit(
        client, CHAT_BAN_PATH.format(mc_user_id=int(mc_user_id)),
        duration_minutes=duration_minutes, verb="chat ban",
    )


async def remove_chat_ban(client, mc_user_id: int) -> tuple[bool, str]:
    """Lift a chat ban early (revoked mute); (ok, detail)."""
    return await _submit(
        client, CHAT_BAN_REMOVE_PATH.format(mc_user_id=int(mc_user_id)),
        duration_minutes=None, verb="chat ban removal",
    )
