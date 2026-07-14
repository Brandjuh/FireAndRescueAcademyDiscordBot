"""Panel keeper: the Discord panels place and replace themselves.

Each panel (mission requests, training/building requests, member
management, DM mirror, class availability) has a configured channel. On
startup and every half hour the keeper makes sure that channel holds
exactly ONE current panel:

* missing (never posted, or someone deleted it) → post it;
* text changed after a bot update → EDIT the existing message in place
  (the panel keeps its position, no repost spam);
* moved to another channel in config → delete the old one, post anew;
* stray copies (from the old manual commands) → cleaned up.

The tracked message id and a content hash live in the state table, so
restarts never lose track of a panel. The manual commands
(`!fra missionpanel`, `!fra requestpanel`, `!memberpanel`) delegate here
with ``force`` so a hand-triggered repost stays the tracked panel.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Callable

import discord
from discord.ext import commands, tasks

from ..db.repos import StateRepo

log = logging.getLogger(__name__)

_SWEEP_MINUTES = 30
# How many recent messages to scan for stray panel copies.
_STRAY_SCAN_LIMIT = 30


@dataclass(frozen=True)
class PanelSpec:
    key: str                              # state-key segment
    cog_name: str                         # cog carrying panel_embed/panel_view
    channel_id: Callable[[], int]         # configured channel (0 = off)


def panel_digest(embed: discord.Embed) -> str:
    raw = f"{embed.title or ''}\n{embed.description or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class PanelKeeperCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._state = StateRepo(bot.db)
        self._lock = asyncio.Lock()
        self.sweep_loop.start()

    def cog_unload(self) -> None:
        self.sweep_loop.cancel()

    # -- panel registry ---------------------------------------------------

    def _specs(self) -> list[PanelSpec]:
        cfg = self.bot.cfg
        return [
            PanelSpec(
                "mission", "MissionsCog",
                lambda: int(cfg.automation.mission.panel_channel_id or 0),
            ),
            PanelSpec(
                "requests", "RequestsCog",
                lambda: int(getattr(cfg.discord.channels, "request_panel", 0) or 0),
            ),
            PanelSpec(
                "member", "DossierCog",
                lambda: int(getattr(cfg.discord.channels, "member_panel", 0) or 0),
            ),
            PanelSpec(
                "dms", "DmMirrorCog",
                lambda: int(getattr(cfg.discord.channels, "dm_panel", 0) or 0),
            ),
            PanelSpec(
                "classes", "ClassesPanelCog",
                lambda: int(getattr(cfg.discord.channels, "class_panel", 0) or 0),
            ),
            PanelSpec(
                "academy", "AcademyCog",
                lambda: int(getattr(cfg.discord.channels, "academy_panel", 0) or 0),
            ),
        ]

    def _spec(self, key: str) -> PanelSpec | None:
        for spec in self._specs():
            if spec.key == key:
                return spec
        return None

    # -- the keeper loop ----------------------------------------------------

    @tasks.loop(minutes=_SWEEP_MINUTES)
    async def sweep_loop(self) -> None:
        async with self._lock:
            for spec in self._specs():
                try:
                    outcome = await self.ensure(spec.key)
                    if outcome not in ("ok", "skipped"):
                        log.info("panel %s: %s", spec.key, outcome)
                except Exception:
                    log.exception("panel keeper failed for %s", spec.key)

    @sweep_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # -- core ---------------------------------------------------------------

    async def ensure(
        self,
        key: str,
        *,
        channel: discord.abc.Messageable | None = None,
        force: bool = False,
    ) -> str:
        """Make sure panel *key* exists, is current, and is unique in its
        channel. Returns what happened: 'skipped' (no channel), 'ok',
        'updated' (edited in place), 'posted' (new message)."""
        spec = self._spec(key)
        if spec is None:
            return "skipped"
        cog = self.bot.get_cog(spec.cog_name)
        if cog is None:
            return "skipped"
        if channel is None:
            channel_id = spec.channel_id()
            channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return "skipped"

        embed = cog.panel_embed()
        digest = panel_digest(embed)
        message = await self._tracked_message(key)

        if message is not None and not force and message.channel.id == channel.id:
            if await self._state.get(f"panel:{key}:hash") == digest:
                return "ok"
            # The panel text changed with an update — refresh in place.
            await message.edit(embed=embed, view=cog.panel_view())
            await self._state.set(f"panel:{key}:hash", digest)
            return "updated"

        # Reposting (forced, moved channel, or nothing tracked): the old
        # message and any stray copies go away first so the channel never
        # shows two panels.
        if message is not None:
            await self._delete_quietly(message)
        await self._delete_strays(channel, embed.title)
        sent = await channel.send(embed=embed, view=cog.panel_view())
        await self._state.set(f"panel:{key}:message", f"{channel.id}:{sent.id}")
        await self._state.set(f"panel:{key}:hash", digest)
        return "posted"

    # -- helpers --------------------------------------------------------------

    async def _tracked_message(self, key: str) -> discord.Message | None:
        stored = await self._state.get(f"panel:{key}:message")
        if not stored:
            return None
        chan_raw, _, msg_raw = stored.partition(":")
        try:
            chan_id, msg_id = int(chan_raw), int(msg_raw)
        except ValueError:
            return None
        channel = self.bot.get_channel(chan_id)
        if channel is None:
            return None
        try:
            return await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @staticmethod
    async def _delete_quietly(message: discord.Message) -> None:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _delete_strays(self, channel, title: str | None) -> None:
        """Remove old copies of this panel (same embed title, posted by us)
        that we lost track of — e.g. hand-posted before the keeper existed."""
        if not title:
            return
        me = self.bot.user
        try:
            async for msg in channel.history(limit=_STRAY_SCAN_LIMIT):
                if me is not None and msg.author.id != me.id:
                    continue
                if any(e.title == title for e in msg.embeds):
                    await self._delete_quietly(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass
