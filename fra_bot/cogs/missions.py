"""Discord front-end for custom "Own mission" scheduling.

Members request a large scale alliance mission — supplying the full
parameter set — through a persistent panel (button → modal) or the
``/mission`` slash command. Requests are queued in ``scheduled_missions``;
the :class:`~fra_bot.services.missions.MissionScheduler` starts them at the
next free slot. Outcomes are announced back to Discord by a publisher loop
(never to MissionChief while in dry-run), so nothing here raises red flags.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..db.repos import MissionsRepo
from ..mc.parsers.mission_spec import MissionSpec, MissionSpecError

log = logging.getLogger(__name__)

PANEL_BUTTON_ID = "fra:mission:new"
_POST_PAUSE_SECONDS = 1.2
_DESC_LIMIT = 4096

_STATUS_STYLE = {
    "done": ("🚨 Mission started", discord.Colour.green()),
    "failed": ("❌ Mission failed", discord.Colour.red()),
    "skipped": ("🧪 Mission (dry-run)", discord.Colour.gold()),
    "waiting": ("⏳ Mission queued", discord.Colour.blurple()),
    "cancelled": ("🚫 Mission cancelled", discord.Colour.light_grey()),
}


def _opt_int(text: str | None, default: int | None) -> int | None:
    text = (text or "").strip()
    if not text:
        return default
    return int(text)  # ValueError bubbles up to the caller


def spec_from_inputs(
    *, location: str, mission_type: str | None, size: str | None, amount: str | None
) -> MissionSpec:
    """Build a validated MissionSpec from free-text panel/slash inputs."""
    return MissionSpec(
        location_text=location,
        mission_type_id=_opt_int(mission_type, None),
        size=_opt_int(size, 1) or 1,
        amount=_opt_int(amount, 1) or 1,
    ).validate()


class MissionRequestModal(discord.ui.Modal, title="Request a mission"):
    location = discord.ui.TextInput(
        label="Location (address or Google Maps link)",
        placeholder="e.g. 350 5th Ave, New York",
        max_length=200,
    )
    mission_type = discord.ui.TextInput(
        label="Mission type ID (optional)", required=False, max_length=8
    )
    size = discord.ui.TextInput(
        label="Size (1-20)", required=False, default="1", max_length=2
    )
    amount = discord.ui.TextInput(
        label="Amount (1-50)", required=False, default="1", max_length=2
    )

    def __init__(self, cog: "MissionsCog") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog.handle_request(
            interaction,
            location=str(self.location),
            mission_type=str(self.mission_type),
            size=str(self.size),
            amount=str(self.amount),
        )


class MissionPanelView(discord.ui.View):
    """Persistent panel; re-registered at startup so its button survives
    restarts."""

    def __init__(self, cog: "MissionsCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(
        label="Request a mission",
        style=discord.ButtonStyle.primary,
        emoji="🚨",
        custom_id=PANEL_BUTTON_ID,
    )
    async def request(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(MissionRequestModal(self._cog))


class MissionsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.service = bot.missions_service
        self.repo = MissionsRepo(bot.db)
        self._lock = asyncio.Lock()
        self.publish_loop.start()

    def cog_unload(self) -> None:
        self.publish_loop.cancel()

    # -- request intake --------------------------------------------------

    async def handle_request(
        self,
        interaction: discord.Interaction,
        *,
        location: str,
        mission_type: str | None,
        size: str | None,
        amount: str | None,
    ) -> None:
        try:
            spec = spec_from_inputs(
                location=location, mission_type=mission_type, size=size, amount=amount
            )
        except (ValueError, MissionSpecError) as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await self._enqueue_and_ack(interaction, spec)

    @app_commands.command(
        name="mission",
        description="Request a large scale alliance mission (queued to the next free slot)",
    )
    @app_commands.describe(
        location="Address or Google Maps link",
        mission_type="Mission type ID (optional)",
        size="Footprint size 1-20 (default 1)",
        amount="Amount 1-50 (default 1)",
    )
    async def slash_mission(
        self,
        interaction: discord.Interaction,
        location: str,
        mission_type: int | None = None,
        size: int = 1,
        amount: int = 1,
    ) -> None:
        try:
            spec = MissionSpec(
                location_text=location,
                mission_type_id=mission_type,
                size=size,
                amount=amount,
            ).validate()
        except MissionSpecError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await self._enqueue_and_ack(interaction, spec)

    async def _enqueue_and_ack(
        self, interaction: discord.Interaction, spec: MissionSpec
    ) -> None:
        mission_id = await self.service.enqueue_discord(
            spec,
            requester_name=interaction.user.display_name,
            requester_mc_id=None,
            discord_user_id=interaction.user.id,
            channel_id=interaction.channel_id,
        )
        type_txt = (
            f"type `{spec.mission_type_id}`"
            if spec.mission_type_id is not None
            else "default type"
        )
        note = "" if self.bot.cfg.automation.mission.enabled else (
            "\n_The mission scheduler is currently off, so this will wait until "
            "an admin enables it._"
        )
        await interaction.response.send_message(
            f"✅ Mission **#{mission_id}** queued — {type_txt}, size {spec.size}, "
            f"amount {spec.amount}, at *{spec.location_text}*. It will start at the "
            f"next free alliance mission slot.{note}",
            ephemeral=True,
        )

    # -- panel posting (called from AdminCog) ---------------------------

    async def post_panel(self, channel: discord.abc.Messageable) -> None:
        embed = discord.Embed(
            title="🚨 Request an alliance mission",
            colour=discord.Colour.blurple(),
            description=(
                "Click below to request a **large scale alliance mission**. "
                "You supply the location and parameters; the bot queues it and "
                "starts it at the next free mission slot.\n\n"
                "You can also use the **/mission** slash command."
            ),
        )
        await channel.send(embed=embed, view=MissionPanelView(self))

    # -- outcome publisher ----------------------------------------------

    @tasks.loop(seconds=45)
    async def publish_loop(self) -> None:
        async with self._lock:
            try:
                await self._publish_outcomes()
            except Exception:
                log.exception("Mission outcome publisher failed")

    @publish_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _publish_outcomes(self) -> None:
        admin_channel = self.bot.channel_for("admin_log")
        for row in await self.repo.pending_announcements():
            channel = None
            if row["channel_id"]:
                channel = self.bot.get_channel(int(row["channel_id"]))
            channel = channel or admin_channel
            if channel is None:
                # Nowhere to post it; mark posted so it can't wedge the queue.
                await self.repo.mark_posted(row["id"])
                continue
            title, colour = _STATUS_STYLE.get(
                row["status"], ("Mission update", discord.Colour.light_grey())
            )
            requester = row["requester_name"] or "member"
            lines = [f"**#{row['id']}** — requested by {requester}"]
            if row["address"] or row["location_text"]:
                lines.append(f"📍 {row['address'] or row['location_text']}")
            if row["status_detail"]:
                lines.append(row["status_detail"])
            embed = discord.Embed(
                title=title,
                colour=colour,
                description="\n".join(lines)[:_DESC_LIMIT],
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                status = getattr(exc, "status", None)
                if status is not None and 400 <= status < 500:
                    log.error("Dropping unpostable mission update (HTTP %s)", status)
                    await self.repo.mark_posted(row["id"])
                    continue
                log.warning("Transient failure posting mission update; retry next tick")
                return
            await self.repo.mark_posted(row["id"])
            await asyncio.sleep(_POST_PAUSE_SECONDS)
