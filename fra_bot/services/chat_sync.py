"""Alliance chat bridge I/O (reference bot: chatmanager).

The service owns everything MissionChief-side: fetching the history,
posting a message through the main page's chat form, the watermark
(last seen chat id) and the outgoing-echo memory that keeps our own
relayed messages from bouncing back into Discord. The cog owns the
Discord side (loop, channel sends, the message listener).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ..db.database import Database
from ..db.repos import StateRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import FetchError
from ..mc.parsers.chat import (
    ChatMessage,
    build_chat_payload,
    format_discord_message_for_mc,
    parse_chat_form,
    parse_chat_history,
)

log = logging.getLogger(__name__)

MAIN_PATH = "/"
CHATS_PATH = "/alliance_chats"

LAST_SEEN_KEY = "chat_bridge_last_seen_id"
ECHOES_KEY = "chat_bridge_outgoing_echoes"

#: Never post to the game chat faster than this (the reference bot's
#: anti-spam spacing — separate from, and on top of, the global pacer).
MIN_MC_POST_INTERVAL_SECONDS = 30.0
#: Outgoing echoes older than this are forgotten.
ECHO_TTL_SECONDS = 30 * 60


class ChatSyncService:
    def __init__(self, client: MissionChiefClient, db: Database) -> None:
        self.client = client
        self.state = StateRepo(db)
        self._post_lock = asyncio.Lock()
        self._last_post_at = 0.0

    # -- MissionChief → Discord ------------------------------------------

    async def fetch_history(self) -> list[ChatMessage]:
        """Current chat history, oldest first. Raises MissionChiefError."""
        return parse_chat_history(await self.client.fetch_page(CHATS_PATH))

    async def last_seen(self) -> int:
        raw = await self.state.get(LAST_SEEN_KEY)
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    async def set_last_seen(self, chat_id: int) -> None:
        await self.state.set(LAST_SEEN_KEY, str(int(chat_id)))

    async def reset_watermark(self) -> None:
        """Next pass re-baselines: current history is marked seen, nothing
        is replayed into Discord."""
        await self.state.set(LAST_SEEN_KEY, "0")

    # -- Discord → MissionChief ------------------------------------------

    async def send_from_discord(self, username: str, body: str) -> str:
        """Relay a Discord message into the game chat; returns the exact
        text sent (for the echo memory). Enforces the reference bot's
        30-second spacing between game-chat posts on top of the pacer."""
        text = format_discord_message_for_mc(username, body)
        async with self._post_lock:
            wait = MIN_MC_POST_INTERVAL_SECONDS - (time.monotonic() - self._last_post_at)
            if wait > 0:
                await asyncio.sleep(wait)
            form = parse_chat_form(
                await self.client.fetch_page(MAIN_PATH), self.client.url(MAIN_PATH)
            )
            if form.method != "post":
                raise FetchError(
                    form.action, message=f"unexpected chat form method {form.method!r}"
                )
            payload = build_chat_payload(form, text)
            # client.url() resolves relative AND absolute form actions.
            status, _, _ = await self.client.post_form(
                form.action,
                payload,
                referer=self.client.url(MAIN_PATH),
                ajax=True,
                csrf_token=form.hidden_fields.get("authenticity_token"),
            )
            if status >= 400:
                raise FetchError(form.action, status,
                                 f"chat post rejected (HTTP {status})")
            self._last_post_at = time.monotonic()
        await self.remember_echo(text)
        return text

    # -- echo memory -------------------------------------------------------

    async def _load_echoes(self) -> list[dict]:
        raw = await self.state.get(ECHOES_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        now = int(time.time())
        return [
            e for e in data
            if isinstance(e, dict)
            and now - int(e.get("created_at") or 0) <= ECHO_TTL_SECONDS
        ]

    async def remember_echo(self, text: str) -> None:
        echoes = await self._load_echoes()
        echoes.append({"message": text, "created_at": int(time.time())})
        await self.state.set(ECHOES_KEY, json.dumps(echoes))

    async def consume_echo(self, text: str) -> bool:
        """True (and forget one copy) when *text* is a message WE relayed
        into the game — the poll must not mirror it back into Discord."""
        echoes = await self._load_echoes()
        for index, echo in enumerate(echoes):
            if str(echo.get("message") or "") == text:
                del echoes[index]
                await self.state.set(ECHOES_KEY, json.dumps(echoes))
                return True
        await self.state.set(ECHOES_KEY, json.dumps(echoes))
        return False
