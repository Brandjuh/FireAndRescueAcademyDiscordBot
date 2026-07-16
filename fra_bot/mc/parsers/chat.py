"""Alliance chat parsers (reference bot: chatmanager).

Two pages are involved:

* the MAIN page (``/``) carries the ``new_alliance_chat`` form whose
  hidden fields (utf8 + authenticity_token) must be echoed back when
  posting a chat message;
* ``/alliance_chats`` renders the recent history as
  ``div#chat_message_<id>`` nodes — the numeric id is the watermark.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

#: MissionChief truncates/rejects longer chat messages.
MAX_MC_CHAT_LENGTH = 1000
#: Discord embed field limit.
MAX_EMBED_MESSAGE_LENGTH = 1024

_CHAT_NODE_ID = re.compile(r"^chat_message_\d+$")
_PROFILE_HREF = re.compile(r"/profile/(\d+)")


@dataclass(frozen=True)
class ChatForm:
    action: str
    method: str
    hidden_fields: dict[str, str]
    message_field: str


@dataclass(frozen=True)
class ChatMessage:
    chat_id: int
    username: str
    mc_user_id: int | None
    message: str
    timestamp: str  # raw data-message-time (ISO with offset), may be ""


def parse_chat_form(html: str, page_url: str) -> ChatForm:
    """The ``new_alliance_chat`` form on the main page, or raise ValueError."""
    soup = BeautifulSoup(html or "", "lxml")
    form = soup.find("form", id="new_alliance_chat")
    if form is None:
        raise ValueError("MissionChief alliance chat form not found")
    message_input = form.find(attrs={"name": "alliance_chat[message]"})
    if message_input is None:
        raise ValueError("MissionChief alliance chat message field not found")
    hidden = {}
    for field in form.find_all("input"):
        name = field.get("name")
        if name and str(field.get("type") or "").lower() == "hidden":
            hidden[str(name)] = str(field.get("value") or "")
    return ChatForm(
        action=urljoin(page_url, str(form.get("action") or "/alliance_chats")),
        method=str(form.get("method") or "post").lower(),
        hidden_fields=hidden,
        message_field=str(message_input.get("name")),
    )


def build_chat_payload(form: ChatForm, message: str) -> dict[str, str]:
    text = normalize_mc_message(message)
    if not text:
        raise ValueError("Chat message cannot be empty")
    payload = dict(form.hidden_fields)
    payload[form.message_field] = text
    return payload


def parse_chat_history(html: str) -> list[ChatMessage]:
    """All chat messages on ``/alliance_chats``, oldest first."""
    soup = BeautifulSoup(html or "", "lxml")
    messages: list[ChatMessage] = []
    for node in soup.find_all(id=_CHAT_NODE_ID):
        try:
            chat_id = int(str(node.get("id")).rsplit("_", 1)[-1])
        except ValueError:
            continue
        username_node = node.select_one("strong a") or node.find("strong")
        username = (
            username_node.get_text(" ", strip=True) if username_node else "Unknown"
        )
        mc_user_id = None
        if username_node is not None and username_node.name == "a":
            match = _PROFILE_HREF.search(str(username_node.get("href") or ""))
            if match:
                mc_user_id = int(match.group(1))
        content = node.select_one(".message-content")
        message = content.get_text("\n", strip=True) if content else ""
        if not message:
            continue
        messages.append(ChatMessage(
            chat_id=chat_id,
            username=username,
            mc_user_id=mc_user_id,
            message=message,
            timestamp=str(node.get("data-message-time") or "").strip(),
        ))
    return sorted(messages, key=lambda m: m.chat_id)


def normalize_mc_message(message: str) -> str:
    """Collapse whitespace and cap at the game's chat length limit."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if len(text) > MAX_MC_CHAT_LENGTH:
        return text[: MAX_MC_CHAT_LENGTH - 3].rstrip() + "..."
    return text


def format_discord_message_for_mc(username: str, message: str) -> str:
    """The relay format the reference bot used: ``[DiscordName] text``."""
    return normalize_mc_message(f"[{username}] {message}")


def discord_timestamp(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Unknown"
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    unix = int(parsed.timestamp())
    return f"<t:{unix}:F> (<t:{unix}:R>)"


def truncate_embed_value(value: str, limit: int = MAX_EMBED_MESSAGE_LENGTH) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text or "No message content."
    return text[: limit - 3].rstrip() + "..."
