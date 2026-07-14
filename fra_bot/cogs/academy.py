"""Academy-build panel: four role-gated buttons that queue a fixed-address
academy build (Fire / Police / Rescue / Coastal). The heavy lifting lives in
:mod:`fra_bot.services.academy`; this cog is the Discord surface — a persistent
button panel (re-registered at startup) plus the ``panel_embed``/``panel_view``
the panel keeper renders.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..services.academy import ACADEMIES

log = logging.getLogger(__name__)

_CUSTOM_ID = {kind: f"fra:academy:{kind}" for kind in ACADEMIES}


def _member_may_build(member, cfg) -> bool:
    """A member may use the panel if they're a guild administrator, hold the
    configured academy role, or hold an admin role (fallback)."""
    if member.guild_permissions.administrator:
        return True
    allowed = set(cfg.discord.admin_role_ids)
    role_id = cfg.automation.academy.role_id
    if role_id:
        allowed.add(role_id)
    return any(role.id in allowed for role in member.roles)


def _role_ok(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    return _member_may_build(member, interaction.client.cfg)


async def _send(interaction: discord.Interaction, content: str) -> None:
    """Ephemeral reply that works whether or not the interaction was deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


def _outcome_text(spec: dict, row) -> str:
    if row is None:
        return "⚠️ The request could not be found — please try again."
    detail = row["status_detail"] or ""
    return {
        "done": f"✅ {detail}",
        "waiting": f"⏳ Queued — {detail}",
        "skipped": f"🧪 {detail}",
        "failed": f"❌ {detail}",
    }.get(row["status"], f"… {spec['label']}: {row['status']}")


class AcademyPanelView(discord.ui.View):
    """Persistent panel; re-registered at startup so its buttons survive
    restarts (``timeout=None`` + a stable ``custom_id`` per button)."""

    def __init__(self, cog: "AcademyCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(label="Fire academy", style=discord.ButtonStyle.primary,
                       emoji="🚒", custom_id=_CUSTOM_ID["fire"])
    async def fire(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._cog.build(interaction, "fire")

    @discord.ui.button(label="Police academy", style=discord.ButtonStyle.primary,
                       emoji="🚓", custom_id=_CUSTOM_ID["police"])
    async def police(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._cog.build(interaction, "police")

    @discord.ui.button(label="Rescue academy", style=discord.ButtonStyle.primary,
                       emoji="🚑", custom_id=_CUSTOM_ID["rescue"])
    async def rescue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._cog.build(interaction, "rescue")

    @discord.ui.button(label="Coastal Rescue school", style=discord.ButtonStyle.primary,
                       emoji="🌊", custom_id=_CUSTOM_ID["coastal"])
    async def coastal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._cog.build(interaction, "coastal")


class AcademyCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.service = bot.academy

    async def build(self, interaction: discord.Interaction, academy_kind: str) -> None:
        spec = ACADEMIES.get(academy_kind)
        if spec is None:
            await _send(interaction, "⚠️ Unknown academy type.")
            return
        if not _role_ok(interaction):
            await _send(
                interaction,
                "⛔ You don't have permission to use the academy build panel.",
            )
            return
        # Acknowledge within Discord's 3s window BEFORE the (seconds-long)
        # scan + funds read + browser build; surface failures so a swallowed
        # exception isn't a silent hang.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            request_id = await self.service.enqueue(
                academy_kind,
                requester_name=str(interaction.user),
                discord_user_id=interaction.user.id,
                channel_id=interaction.channel_id,
            )
            # Build now, serialised against the retry poller via the shared
            # job lock so two clicks can't build at once.
            async with self.bot.job_lock("academy-builds"):
                row = await self.service.run_one(request_id)
        except Exception as exc:  # noqa: BLE001 — show the member what broke
            log.exception("academy build click failed")
            await _send(interaction, f"❌ Something went wrong: {exc}")
            return
        await _send(interaction, _outcome_text(spec, row))

    # -- panel (posted/maintained by the panel keeper) ------------------

    def panel_embed(self) -> discord.Embed:
        address = self.bot.cfg.automation.academy.address
        return discord.Embed(
            title="🏫 Build an alliance academy",
            colour=discord.Colour.blurple(),
            description=(
                f"Press a button to build that academy at **{address}** "
                "(duplicates allowed). It is named "
                "`[AA] <type> academy #N` with the next free number, scanned "
                "live from the alliance building list.\n\n"
                "🚒 **Fire academy** · 🚓 **Police academy** · "
                "🚑 **Rescue academy** · 🌊 **Coastal Rescue school**\n\n"
                "If the alliance balance is too low the build is **queued** and "
                "starts automatically once funds recover. Only staff with the "
                "configured role (or server admins) can use these buttons."
            ),
        )

    def panel_view(self) -> discord.ui.View:
        return AcademyPanelView(self)
