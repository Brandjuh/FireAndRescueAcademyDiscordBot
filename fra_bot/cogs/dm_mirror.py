"""DM-mirror cog: staff replies in mirrored threads go back into the game,
plus the button panel the old bot had (Send Message / Check Inbox / Reply).

The forum side of the in-game DM mirror. The scheduled scan (see
``DmMirrorService``) posts every conversation into the configured forum;
this cog listens for staff messages typed in those threads and forwards
them as replies inside the matching game conversation. Feedback follows
the reference bot: ✅ reaction on confirmed delivery, ⚠️ + explanation when
the game refuses, an error reply when something breaks.

The panel (self-maintained by the panel keeper in
``discord.channels.dm_panel``) offers the same three actions as the old
MessageManager panel, for staff who prefer buttons over commands.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

PANEL_SEND_ID = "dmmirror:send"
PANEL_SCAN_ID = "dmmirror:scan"
PANEL_REPLY_ID = "dmmirror:reply"


async def _ephemeral(interaction: discord.Interaction, content: str) -> None:
    content = content[:1900]
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class SendMessageModal(discord.ui.Modal, title="Send a MissionChief PM"):
    username = discord.ui.TextInput(
        label="MissionChief username",
        placeholder="Case doesn't matter; must be an alliance member",
        max_length=100,
    )
    subject = discord.ui.TextInput(label="Subject", max_length=120)
    body = discord.ui.TextInput(
        label="Message", style=discord.TextStyle.paragraph, max_length=1800
    )

    def __init__(self, cog: "DmMirrorCog") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self._cog.bot.dm_mirror.send_new(
                str(self.username.value), str(self.subject.value),
                str(self.body.value),
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            log.exception("Panel send failed")
            await _ephemeral(interaction, f"❌ Something went wrong: {exc}")
            return
        if not result["ok"]:
            await _ephemeral(interaction, f"❌ Not sent: {result['detail']}")
            return
        thread = result.get("thread")
        where = f" — continue in {thread.mention}" if thread is not None else ""
        await _ephemeral(
            interaction, f"✅ PM sent to **{self.username.value}**{where}"
        )


class ReplyModal(discord.ui.Modal, title="Reply in a conversation"):
    conversation_id = discord.ui.TextInput(
        label="Conversation id",
        placeholder="The number after # in the thread title (e.g. 12345)",
        max_length=20,
    )
    body = discord.ui.TextInput(
        label="Reply", style=discord.TextStyle.paragraph, max_length=1800
    )

    def __init__(self, cog: "DmMirrorCog") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ok, detail = await self._cog.bot.dm_mirror.reply_to_conversation(
                str(self.conversation_id.value).strip().lstrip("#"),
                str(self.body.value),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Panel reply failed")
            await _ephemeral(interaction, f"❌ Something went wrong: {exc}")
            return
        if ok:
            await _ephemeral(interaction, "✅ Reply delivered.")
        else:
            await _ephemeral(interaction, f"⚠️ Not delivered: {detail}")


class DmPanelView(discord.ui.View):
    """Persistent panel buttons (survive restarts via custom_ids)."""

    def __init__(self, cog: "DmMirrorCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if self._cog._member_allowed(interaction.user):
            return True
        await _ephemeral(interaction, "⛔ Only staff can use the message panel.")
        return False

    @discord.ui.button(
        label="Send message", emoji="✉️",
        style=discord.ButtonStyle.primary, custom_id=PANEL_SEND_ID,
    )
    async def send_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._gate(interaction):
            await interaction.response.send_modal(SendMessageModal(self._cog))

    @discord.ui.button(
        label="Check inbox", emoji="📥",
        style=discord.ButtonStyle.secondary, custom_id=PANEL_SCAN_ID,
    )
    async def check_inbox(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._gate(interaction):
            return
        lock = self._cog.bot.job_lock("dm-mirror")
        if lock.locked():
            await _ephemeral(interaction, "⏳ A DM-mirror scan is already running.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with lock:
            try:
                summary = await self._cog.bot.dm_mirror.scan()
            except Exception as exc:  # noqa: BLE001
                log.exception("Panel inbox scan failed")
                await _ephemeral(interaction, f"❌ Scan failed: {exc}")
                return
        icon = "❌" if summary.get("error") else "✅"
        await _ephemeral(interaction, f"{icon} " + "\n".join(summary["lines"]))

    @discord.ui.button(
        label="Reply", emoji="↩️",
        style=discord.ButtonStyle.secondary, custom_id=PANEL_REPLY_ID,
    )
    async def reply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._gate(interaction):
            await interaction.response.send_modal(ReplyModal(self._cog))


class DmMirrorCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    # -- the panel (found by the panel keeper) --------------------------

    def panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title="📬 MissionChief messages",
            colour=discord.Colour.blurple(),
            description=(
                "In-game private messages, from Discord.\n\n"
                "✉️ **Send message** — PM an alliance member "
                "(a forum thread opens for the conversation)\n"
                "📥 **Check inbox** — scan the game inbox now\n"
                "↩️ **Reply** — answer a conversation by its number\n\n"
                "Tip: every conversation has its own thread in the DM forum — "
                "typing there replies directly."
            ),
        )

    def panel_view(self) -> discord.ui.View:
        return DmPanelView(self)

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
