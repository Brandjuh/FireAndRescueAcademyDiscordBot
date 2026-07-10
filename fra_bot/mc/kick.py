"""Kick a member from the alliance (the reference bot's route).

``/verband/kick/<mc_user_id>`` either performs the kick directly or
renders a confirmation form; when a form comes back it is submitted
with all its fields. Only used by the tax-warning auto-kick, which is
OFF by default.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from .errors import MissionChiefError

log = logging.getLogger(__name__)


def parse_kick_confirmation_form(html: str) -> tuple[str, dict] | None:
    """(action, payload) of a confirmation form on the kick page, if any."""
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "kick" not in action.lower():
            continue
        payload: dict[str, str] = {}
        for tag in form.find_all(("input", "select", "textarea")):
            name = tag.get("name")
            if not name or tag.get("type") in ("submit", "button"):
                continue
            payload[name] = tag.get("value") or ""
        return action, payload
    return None


async def kick_alliance_member(client, mc_user_id: int) -> tuple[bool, str]:
    """Kick via the game's kick route. Returns (ok, detail); the members
    sync confirms the departure within the hour."""
    path = f"/verband/kick/{int(mc_user_id)}"
    try:
        html = await client.fetch_page(path, ajax=True)
    except MissionChiefError as exc:
        return False, f"kick request failed ({exc})"
    confirmation = parse_kick_confirmation_form(html)
    if confirmation is None:
        return True, "kick accepted (no confirmation form shown)"
    action, payload = confirmation
    try:
        status, _, _ = await client.post_form(
            action, payload, referer=client.url(path)
        )
    except MissionChiefError as exc:
        return False, f"kick confirmation failed ({exc})"
    if status >= 400:
        return False, f"kick confirmation rejected (HTTP {status})"
    return True, "kick confirmed"
