"""Send in-game MissionChief private messages (/messages/new).

The reference bot notifies BOARD requesters through an in-game PM — board
posts carry no Discord identity, but the game's own mailbox always reaches
them. The compose form is parsed defensively (field names are matched on
recipient/subject/body heuristics, hidden inputs are carried along) so a
form-layout tweak degrades to a clear error instead of a wrong POST.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .errors import MissionChiefError, ParseError

log = logging.getLogger(__name__)

NEW_MESSAGE_PATH = "/messages/new"

# The flash MissionChief shows after an actual send (the reference bot's
# delivery check). A POST that merely re-renders the compose form — bad
# recipient, validation error — also returns HTTP 200, so the status code
# alone must never be trusted.
SUCCESS_MARKER = "message sent."
_CONVERSATION_URL_RE = re.compile(r"/messages/\d+")

_RECIPIENT_TOKENS = ("recipient", "receiver", "username", "user name", "to]")
_SUBJECT_TOKENS = ("subject", "title")
_BODY_TOKENS = ("body", "content", "text", "message")


@dataclass
class MessageForm:
    action: str
    fields: list[tuple[str, str]] = field(default_factory=list)  # (name, value)
    recipient_field: str | None = None
    subject_field: str | None = None
    body_field: str | None = None
    # Rails forms often branch on the submit button's name (usually
    # "commit"); it must be POSTed along like a browser would.
    submit_name: str | None = None
    submit_value: str = ""


def parse_message_form(html: str) -> MessageForm:
    """The /messages/new compose form: action, all fields, and which of
    them are the recipient / subject / body."""
    soup = BeautifulSoup(html, "lxml")
    form = None
    for candidate in soup.find_all("form"):
        action = candidate.get("action") or ""
        if "message" in action:
            form = candidate
            break
    if form is None:
        form = soup.find("form")
    if form is None:
        raise ParseError("No compose form on /messages/new — layout change?")

    parsed = MessageForm(action=form.get("action") or "/messages")
    for tag in form.find_all(("input", "textarea", "select")):
        name = tag.get("name")
        if tag.get("type") == "submit":
            if name and parsed.submit_name is None:
                parsed.submit_name = name
                parsed.submit_value = tag.get("value") or ""
            continue
        if not name or tag.get("type") == "button":
            continue
        if tag.name == "textarea":
            value = tag.get_text() or ""
        elif tag.name == "select":
            selected = tag.find("option", selected=True) or tag.find("option")
            value = selected.get("value", "") if selected else ""
        else:
            value = tag.get("value") or ""
        parsed.fields.append((name, value))

        lowered = name.lower()
        if parsed.recipient_field is None and any(
            token in lowered for token in _RECIPIENT_TOKENS
        ):
            parsed.recipient_field = name
        elif parsed.subject_field is None and any(
            token in lowered for token in _SUBJECT_TOKENS
        ):
            parsed.subject_field = name
        elif (
            parsed.body_field is None
            and tag.name == "textarea"
            and any(token in lowered for token in _BODY_TOKENS)
        ):
            parsed.body_field = name

    # Any textarea is the body when none matched by name.
    if parsed.body_field is None:
        for tag in form.find_all("textarea"):
            if tag.get("name"):
                parsed.body_field = tag["name"]
                break

    missing = [
        label
        for label, value in (
            ("recipient", parsed.recipient_field),
            ("subject", parsed.subject_field),
            ("body", parsed.body_field),
        )
        if value is None
    ]
    if missing:
        raise ParseError(
            f"Compose form is missing fields: {', '.join(missing)} — layout change?"
        )
    return parsed


def build_message_payload(
    form: MessageForm, recipient: str, subject: str, body: str
) -> dict[str, str]:
    payload: dict[str, str] = {}
    overrides = {
        form.recipient_field: recipient,
        form.subject_field: subject,
        form.body_field: body,
    }
    for name, value in form.fields:
        payload[name] = overrides.get(name, value)
    for name, value in overrides.items():
        payload.setdefault(name, value)
    if form.submit_name:
        payload.setdefault(form.submit_name, form.submit_value)
    return payload


def message_was_sent(html: str, final_url: str = "") -> bool:
    """Did the POST actually deliver? True on the "Message Sent." flash or
    when we landed on a conversation page (/messages/<id>)."""
    if _CONVERSATION_URL_RE.search(final_url or ""):
        return True
    text = BeautifulSoup(html or "", "lxml").get_text(" ", strip=True)
    return SUCCESS_MARKER in text.lower()


def summarize_response(text: str, *, limit: int = 350) -> str:
    """A short, token-redacted plain-text digest of a response for logs."""
    text = re.sub(
        r"<script\b[^>]*>.*?</script>", " ", str(text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(
        r"authenticity_token[^\s&<>\"]+", "authenticity_token=REDACTED",
        text, flags=re.IGNORECASE,
    )
    return " ".join(text.split())[:limit]


async def send_ingame_message(
    client, recipient: str, subject: str, body: str
) -> bool:
    """Send an in-game PM to a MissionChief username. Returns success —
    which requires the game to CONFIRM delivery, not just an HTTP 2xx
    (a validation failure re-renders the compose form with 200)."""
    try:
        form = parse_message_form(await client.fetch_page(NEW_MESSAGE_PATH))
        status, html, final_url = await client.post_form(
            form.action,
            build_message_payload(form, recipient, subject, body),
            referer=client.url(NEW_MESSAGE_PATH),
        )
    except MissionChiefError as exc:
        log.warning("in-game PM to %s failed: %s", recipient, exc)
        return False
    if status >= 400:
        log.warning("in-game PM to %s rejected (HTTP %s)", recipient, status)
        return False
    if not message_was_sent(html, final_url):
        log.warning(
            "in-game PM to %s NOT confirmed (HTTP %s, landed on %s): %s",
            recipient, status, final_url or "?", summarize_response(html),
        )
        return False
    log.info("in-game PM sent to %s (%s)", recipient, subject)
    return True
