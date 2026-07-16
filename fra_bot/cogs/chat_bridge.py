"""Alliance chat ↔ Discord bridge (reference bot: chatmanager).

Game → Discord: poll ``/alliance_chats`` and mirror new messages into the
bridge channel as embeds. The FIRST pass only baselines (marks current
history seen) so enabling the bridge never floods the channel with
history. Messages we ourselves relayed into the game are recognised via
the echo memory and skipped — no double posting.

Discord → game: every human message in the bridge channel is relayed into
the alliance chat as ``[DiscordName] text``, spaced at least 30 s apart
(the reference bot's anti-spam), honouring the global dry_run switch
(🚫 reaction instead of a game post) and flagging failures with ⚠️.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from ..mc.errors import MissionChiefError
from ..mc.parsers.chat import (
    ChatMessage,
    discord_timestamp,
    truncate_embed_value,
)

log = logging.getLogger(__name__)

#: The reference bot polled every 30 s; never faster.
MIN_POLL_SECONDS = 30


class ChatBridgeCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.chat = bot.chat_sync
        interval = max(
            MIN_POLL_SECONDS, int(bot.cfg.automation.chat.interval_seconds)
        )
        self.sync_loop.change_interval(seconds=interval)
        self.sync_loop.start()

    def cog_unload(self) -> None:
        self.sync_loop.cancel()

    # -- config helpers ----------------------------------------------------

    def _channel(self):
        channel_id = int(
            getattr(self.bot.cfg.discord.channels, "chat_bridge", 0) or 0
        )
        return self.bot.get_channel(channel_id) if channel_id else None

    @property
    def _enabled(self) -> bool:
        return bool(self.bot.cfg.automation.chat.enabled)

    # -- game → Discord ------------------------------------------------------

    @tasks.loop(seconds=MIN_POLL_SECONDS)
    async def sync_loop(self) -> None:
        if not self._enabled or self._channel() is None:
            return
        try:
            await self._sync_once()
        except MissionChiefError as exc:
            # Includes a tripped circuit breaker: skip quietly, next tick
            # retries — chat is a live feed, not a queue to drain.
            log.info("chat bridge: sync pass skipped (%s)", exc)
        except Exception:
            log.exception("chat bridge: sync pass failed")

    @sync_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_once(self) -> dict:
        messages = await self.chat.fetch_history()
        if not messages:
            return {"seen": 0, "posted": 0, "skipped_echoes": 0}
        last_seen = await self.chat.last_seen()
        newest = max(m.chat_id for m in messages)
        if last_seen <= 0:
            # Baseline: mark history seen, never replay it into Discord.
            await self.chat.set_last_seen(newest)
            return {"seen": len(messages), "posted": 0, "skipped_echoes": 0}

        channel = self._channel()
        fresh = [m for m in messages if m.chat_id > last_seen]
        posted = skipped = 0
        for message in fresh:
            if await self.chat.consume_echo(message.message):
                skipped += 1
            else:
                if channel is None:
                    break  # keep the watermark so nothing is lost
                await channel.send(
                    embed=self._embed(message),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                posted += 1
            last_seen = max(last_seen, message.chat_id)
            await self.chat.set_last_seen(last_seen)
        return {"seen": len(fresh), "posted": posted, "skipped_echoes": skipped}

    @staticmethod
    def _embed(message: ChatMessage) -> discord.Embed:
        embed = discord.Embed(
            title="MissionChief Alliance Chat",
            colour=discord.Colour.blue(),
        )
        embed.add_field(name="Name", value=message.username, inline=True)
        embed.add_field(
            name="Time", value=discord_timestamp(message.timestamp), inline=False
        )
        embed.add_field(
            name="Message", value=truncate_embed_value(message.message), inline=False
        )
        embed.set_footer(text=f"MissionChief chat ID: {message.chat_id}")
        return embed

    # -- Discord → game ------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if getattr(message.author, "bot", False):
            return
        channel = self._channel()
        if channel is None or message.channel.id != channel.id:
            return
        if not self._enabled:
            return
        parts = [str(message.content or "").strip()]
        parts += [a.url for a in getattr(message, "attachments", [])]
        body = " ".join(p for p in parts if p).strip()
        if not body:
            return
        if self.bot.cfg.automation.dry_run:
            await self._react(message, "🚫")
            log.info("chat bridge: dry-run, NOT relaying %r", body[:80])
            return
        username = getattr(message.author, "display_name", None) or str(message.author)
        try:
            await self.chat.send_from_discord(str(username), body)
        except (MissionChiefError, ValueError) as exc:
            log.warning("chat bridge: relay to game failed: %s", exc)
            await self._react(message, "⚠️")

    @staticmethod
    async def _react(message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass


async def setup(bot) -> None:
    await bot.add_cog(ChatBridgeCog(bot))
