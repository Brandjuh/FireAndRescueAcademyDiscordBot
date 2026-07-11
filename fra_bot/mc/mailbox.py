"""MissionChief mailbox: inbox listing, conversation pages, replies.

The game's PM system is conversation-based. ``/messages`` lists the
conversations (an HTML table inside the ``current_box=inbox`` form);
``/messages/<id>`` shows one conversation (every message is a Bootstrap
``.well`` with the author profile link, ``<p>`` body paragraphs and an ISO
timestamp in ``data-message-time``) and carries the reply form
(``message[conversation_id]`` + ``message[body]``).

Selectors mirror the reference bot's MessageManager cog, which ran against
the live site. Opening a conversation page marks it read in-game, so the
inbox "New" badge is a one-shot signal — callers must persist their own
progress marker.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .errors import MissionChiefError, ParseError
from .messages import message_was_sent, summarize_response

log = logging.getLogger(__name__)

MESSAGES_PATH = "/messages"

_PROFILE_RE = re.compile(r"/profile/\d+")


def _text(element) -> str:
    return " ".join(element.get_text(" ", strip=True).split()) if element else ""


@dataclass
class InboxRow:
    conversation_id: str
    sender: str
    subject: str
    is_new: bool = False


@dataclass
class ConversationMessage:
    author: str
    body: str
    timestamp: str = ""  # ISO string from data-message-time, may be empty


def parse_inbox(html: str) -> list[InboxRow]:
    """Conversations on the /messages page; system messages are skipped."""
    soup = BeautifulSoup(html or "", "lxml")
    inbox_form = None
    for form in soup.find_all("form"):
        current_box = form.find("input", attrs={"name": "current_box"})
        if current_box and (current_box.get("value") or "").lower() == "inbox":
            inbox_form = form
            break
    if inbox_form is None:
        return []

    rows: list[InboxRow] = []
    for row in inbox_form.find_all("tr"):
        checkbox = row.find("input", attrs={"name": "conversations[]"})
        if not checkbox:
            continue
        conversation_id = str(checkbox.get("value") or "").strip()
        cells = row.find_all("td")
        if len(cells) < 4 or not conversation_id:
            continue
        subject_link = cells[3].find("a", href=True)
        if not subject_link:
            continue
        if "/messages/system_message/" in str(subject_link.get("href") or ""):
            continue
        sender_link = cells[2].find("a", href=True)
        rows.append(
            InboxRow(
                conversation_id=conversation_id,
                sender=_text(sender_link) if sender_link else _text(cells[2]),
                subject=_text(subject_link),
                is_new=_text(cells[1]).casefold() == "new",
            )
        )
    return rows


def parse_conversation(html: str) -> list[ConversationMessage]:
    """Messages of a conversation page, in the page's order (newest first)."""
    soup = BeautifulSoup(html or "", "lxml")
    messages: list[ConversationMessage] = []
    for well in soup.find_all(
        "div", class_=lambda value: value and "well" in str(value).split()
    ):
        author_link = well.find("a", href=_PROFILE_RE)
        body = "\n".join(
            _text(p) for p in well.find_all("p") if _text(p)
        ).strip()
        if not author_link or not body:
            continue
        messages.append(
            ConversationMessage(
                author=_text(author_link),
                body=body,
                timestamp=str(well.get("data-message-time") or "").strip(),
            )
        )
    return messages


def build_reply_payload(
    html: str, body: str
) -> tuple[str, list[tuple[str, str]]]:
    """(action, payload) for the reply form on a conversation page. Every
    input is echoed verbatim (authenticity_token, conversation_id, …); only
    ``message[body]`` is ours, and the submit button rides along."""
    if not str(body or "").strip():
        raise ValueError("Reply body is required.")
    soup = BeautifulSoup(html or "", "lxml")
    form = None
    for candidate in soup.find_all("form"):
        if candidate.find(attrs={"name": "message[conversation_id]"}) and candidate.find(
            attrs={"name": "message[body]"}
        ):
            form = candidate
            break
    if form is None:
        raise ParseError("No reply form on this conversation page — layout change?")

    action = form.get("action") or MESSAGES_PATH
    payload: list[tuple[str, str]] = []
    submit: tuple[str, str] | None = None
    for input_el in form.find_all("input"):
        name = input_el.get("name")
        if not name:
            continue
        field_type = (input_el.get("type") or "text").lower()
        if field_type in ("button", "image", "reset"):
            continue
        if field_type == "submit":
            submit = (name, input_el.get("value") or "")
            continue
        payload.append((name, input_el.get("value") or ""))

    body_seen = False
    for textarea in form.find_all("textarea"):
        name = textarea.get("name")
        if not name:
            continue
        if name == "message[body]":
            payload.append((name, str(body)))
            body_seen = True
        else:
            payload.append((name, textarea.get_text() or ""))
    if not body_seen:
        raise ParseError("Reply form has no message[body] field — layout change?")
    if submit:
        payload.append(submit)
    return action, payload


async def fetch_inbox(client) -> list[InboxRow]:
    return parse_inbox(await client.fetch_page(MESSAGES_PATH))


async def fetch_conversation(client, conversation_id: str) -> list[ConversationMessage]:
    conversation_id = str(conversation_id).strip()
    if not conversation_id.isdigit():
        raise ValueError("Conversation id must be numeric.")
    html = await client.fetch_page(f"{MESSAGES_PATH}/{conversation_id}")
    return parse_conversation(html)


async def send_reply(client, conversation_id: str, body: str) -> tuple[bool, str]:
    """Reply inside an existing conversation. Success requires the game's
    own confirmation ("Message Sent."), never just an HTTP 2xx — a
    validation failure re-renders the page with 200."""
    conversation_id = str(conversation_id).strip()
    if not conversation_id.isdigit():
        return False, "conversation id must be numeric"
    page_path = f"{MESSAGES_PATH}/{conversation_id}"
    try:
        html = await client.fetch_page(page_path)
        action, payload = build_reply_payload(html, body)
        status, response_html, _final_url = await client.post_form(
            action, payload, referer=client.url(page_path)
        )
    except (MissionChiefError, ValueError) as exc:
        log.warning("reply in conversation %s failed: %s", conversation_id, exc)
        return False, str(exc)
    if status >= 400:
        return False, f"HTTP {status}"
    # Marker only, no redirect-URL fallback: a failed reply may re-render
    # the very conversation URL a success would redirect to.
    if not message_was_sent(response_html):
        digest = summarize_response(response_html)
        log.warning(
            "reply in conversation %s NOT confirmed (HTTP %s): %s",
            conversation_id, status, digest,
        )
        return False, f"the game did not confirm delivery ({digest[:120] or 'empty response'})"
    return True, "sent"
