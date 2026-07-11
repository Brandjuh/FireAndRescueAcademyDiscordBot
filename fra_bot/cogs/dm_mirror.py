"""DM-mirror cog: staff replies in mirrored threads go back into the game.

The forum side of the in-game DM mirror. The scheduled scan (see
``DmMirrorService``) posts every conversation into the configured forum;
this cog listens for staff messages typed in those threads and forwards
them as replies inside the matching game conversation. Feedback follows
the reference bot: ✅ reaction on confirmed delivery, ⚠️ + explanation when
the game refuses, an error reply when something breaks.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


class DmMirrorCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    def _member_allowed(self, member) -> bool:
        """Staff only: admins, admin roles, or the configured staff roles."""
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.administrator:
            return True
        allowed = set(self.bot.cfg.discord.admin_role_ids) | set(
            self.bot.cfg.discord.staff_role_ids
        )
        return any(role.id in allowed for role in getattr(member, "roles", []))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if getattr(message.author, "bot", False):
            return
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return
        forum_id = self.bot.cfg.discord.channels.dm_mirror
        if not forum_id or getattr(channel, "parent_id", None) != forum_id:
            return
        content = str(message.content or "").strip()
        if content.startswith("!"):
            return  # bot commands typed in the thread are not replies
        if not self._member_allowed(message.author):
            return
        # Attachments can't travel into the game; pass their URLs along.
        parts = [content] if content else []
        parts.extend(
            attachment.url
            for attachment in getattr(message, "attachments", []) or []
            if getattr(attachment, "url", "")
        )
        body = "\n".join(parts).strip()
        if not body:
            return

        try:
            ok, detail = await self.bot.dm_mirror.reply_from_thread(
                channel.id, body
            )
        except Exception as exc:  # noqa: BLE001 — never crash the listener
            log.exception("Forwarding a DM-mirror reply failed")
            await self._safe_reply(
                message, f"❌ Could not send the reply to the game: {exc}"
            )
            return
        if ok:
            await self._safe_react(message, "✅")
        else:
            await self._safe_react(message, "⚠️")
            await self._safe_reply(
                message, f"⚠️ The game did not accept this reply: {detail}"
            )

    @staticmethod
    async def _safe_react(message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            log.debug("Could not add DM-mirror feedback reaction", exc_info=True)

    @staticmethod
    async def _safe_reply(message, text: str) -> None:
        try:
            await message.reply(text[:1900], mention_author=False)
        except discord.HTTPException:
            log.debug("Could not post DM-mirror feedback reply", exc_info=True)
