"""Discord front-end for training and building requests.

A persistent panel gives members two buttons:

* **Request a training** — pick the academy type, pick the course, optionally
  toggle a reminder, submit. The request lands in ``automation_requests``
  exactly like a board post would, and the trainings poller opens the class
  at its next pass.
* **Request a building** — paste a Google Maps link to a real hospital or
  prison; the buildings poller detects the type and builds it.

Discord-sourced requests carry ``thread_id = 0`` (no board post), so the
services skip board replies for them; outcomes are announced by the
automation publisher in Discord instead. An opted-in training reminder pings
the member once the course should be finished (start + catalog duration).
"""

from __future__ import annotations

import asyncio
import json
import logging

import discord
from discord.ext import commands, tasks

from ..db.repos import AutomationRepo, RemindersRepo
from ..geo.maps_links import find_maps_links
from ..mc.trainings_catalog import DISCIPLINES

log = logging.getLogger(__name__)

PANEL_TRAINING_ID = "fra:request:training"
PANEL_BUILDING_ID = "fra:request:building"
#: Sentinel thread id marking a request that came from Discord, not a board.
DISCORD_THREAD = 0

_DISCIPLINE_CHOICES = (
    ("fire", "🚒 Fire"),
    ("police", "🚓 Police"),
    ("ems", "🚑 EMS"),
    ("coastal", "🌊 Water Rescue"),
)
_DISCIPLINE_LABEL = dict(_DISCIPLINE_CHOICES)


async def _send(interaction: discord.Interaction, content: str) -> None:
    """Ephemeral reply that works whether or not the interaction was
    already acknowledged (deferred)."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class TrainingChooserView(discord.ui.View):
    """Ephemeral, per-click chooser: academy type → course → submit."""

    def __init__(self, cog: "RequestsCog") -> None:
        super().__init__(timeout=300)
        self._cog = cog
        self.discipline: str | None = None
        self.training: str | None = None
        self.remind = False

        self.d_select = discord.ui.Select(
            placeholder="1️⃣ Pick the academy type",
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in _DISCIPLINE_CHOICES
            ],
            row=0,
        )
        self.d_select.callback = self._pick_discipline
        self.t_select = discord.ui.Select(
            placeholder="2️⃣ Pick the course (academy type first)",
            options=[discord.SelectOption(label="—", value="_none")],
            disabled=True,
            row=1,
        )
        self.t_select.callback = self._pick_training
        self.remind_btn = discord.ui.Button(
            label="🔕 Remind me when it's done: off",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        self.remind_btn.callback = self._toggle_remind
        self.go_btn = discord.ui.Button(
            label="Request training",
            style=discord.ButtonStyle.success,
            emoji="🎓",
            row=2,
        )
        self.go_btn.callback = self._submit
        for item in (self.d_select, self.t_select, self.remind_btn, self.go_btn):
            self.add_item(item)

    async def _pick_discipline(self, interaction: discord.Interaction) -> None:
        self.discipline = self.d_select.values[0]
        self.training = None
        courses = sorted(DISCIPLINES.get(self.discipline, {}).items())
        self.t_select.options = [
            discord.SelectOption(label=f"{name} ({days}d)"[:100], value=name[:100])
            for name, days in courses
        ][:25]
        self.t_select.disabled = False
        self.t_select.placeholder = "2️⃣ Pick the course"
        for option in self.d_select.options:
            option.default = option.value == self.discipline
        await interaction.response.edit_message(view=self)

    async def _pick_training(self, interaction: discord.Interaction) -> None:
        self.training = self.t_select.values[0]
        for option in self.t_select.options:
            option.default = option.value == self.training
        await interaction.response.edit_message(view=self)

    async def _toggle_remind(self, interaction: discord.Interaction) -> None:
        self.remind = not self.remind
        self.remind_btn.label = (
            "🔔 Remind me when it's done: ON"
            if self.remind else "🔕 Remind me when it's done: off"
        )
        self.remind_btn.style = (
            discord.ButtonStyle.primary if self.remind
            else discord.ButtonStyle.secondary
        )
        await interaction.response.edit_message(view=self)

    async def _submit(self, interaction: discord.Interaction) -> None:
        if not self.discipline or not self.training:
            await interaction.response.send_message(
                "Pick an academy type and a course first.", ephemeral=True
            )
            return
        # Acknowledge within Discord's 3s window BEFORE doing any work, and
        # surface failures — a swallowed exception here looks like a hang.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._cog.submit_training(
                interaction, self.discipline, self.training, remind=self.remind
            )
        except Exception as exc:  # noqa: BLE001 — show the member what broke
            log.exception("training request submit failed")
            await _send(interaction, f"❌ Something went wrong: {exc}")
        self.stop()


class BuildingRequestModal(discord.ui.Modal, title="Request a building"):
    link = discord.ui.TextInput(
        label="Google Maps link to a REAL hospital/prison",
        placeholder="https://maps.app.goo.gl/…",
        max_length=400,
    )

    def __init__(self, cog: "RequestsCog") -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._cog.submit_building(interaction, str(self.link.value))
        except Exception as exc:  # noqa: BLE001 — show the member what broke
            log.exception("building request submit failed")
            await _send(interaction, f"❌ Something went wrong: {exc}")


class RequestPanelView(discord.ui.View):
    """Persistent panel; re-registered at startup so it survives restarts."""

    def __init__(self, cog: "RequestsCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(
        label="Request a training",
        style=discord.ButtonStyle.primary,
        emoji="🎓",
        custom_id=PANEL_TRAINING_ID,
    )
    async def training(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Pick the academy type and the course, then press "
            "**Request training**:",
            view=TrainingChooserView(self._cog),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Request a building",
        style=discord.ButtonStyle.primary,
        emoji="🏥",
        custom_id=PANEL_BUILDING_ID,
    )
    async def building(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(BuildingRequestModal(self._cog))


def training_request_payload(
    discipline: str, training: str, *, user_id: int, channel_id: int | None,
    remind: bool,
) -> dict:
    """The automation_requests payload for a Discord training request —
    the same shape the board parser produces, plus the Discord flags."""
    days = DISCIPLINES.get(discipline, {}).get(training, 0)
    return {
        "trainings": [
            {"discipline": discipline, "name": training, "duration": days}
        ],
        "ambiguous": [],
        "discord_user_id": user_id,
        "channel_id": channel_id,
        "remind": bool(remind),
    }


def building_request_payload(
    link: str, *, user_id: int, channel_id: int | None
) -> dict | None:
    """Payload for a Discord building request, or None for a non-maps link."""
    links = find_maps_links(link)
    if not links:
        return None
    return {
        "link": links[0],
        "discord_user_id": user_id,
        "channel_id": channel_id,
    }


class RequestsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.requests = AutomationRepo(bot.db)
        self.reminders = RemindersRepo(bot.db)
        self._lock = asyncio.Lock()
        self.reminder_loop.start()

    def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    # -- intake -----------------------------------------------------------

    async def submit_training(
        self, interaction: discord.Interaction, discipline: str, training: str,
        *, remind: bool,
    ) -> None:
        payload = training_request_payload(
            discipline, training,
            user_id=interaction.user.id, channel_id=interaction.channel_id,
            remind=remind,
        )
        rid = await self.requests.create(
            kind="training", thread_id=DISCORD_THREAD, post_id=interaction.id,
            requester_name=interaction.user.display_name, requester_mc_id=None,
            payload=json.dumps(payload),
        )
        days = payload["trainings"][0]["duration"]
        notes = []
        if remind:
            notes.append(f"🔔 I'll ping you in ~{days} day(s) when it should be done")
        if not self.bot.cfg.automation.training.enabled:
            notes.append(
                "⚠️ training automation is currently OFF — an admin must enable it"
            )
        note = ("\n" + " · ".join(notes)) if notes else ""
        await _send(
            interaction,
            f"✅ Request **#{rid}** queued — **{training}** "
            f"({_DISCIPLINE_LABEL.get(discipline, discipline)}). I'll open a "
            f"free class at the next pass (~5 min).{note}",
        )

    async def submit_building(
        self, interaction: discord.Interaction, link: str
    ) -> None:
        payload = building_request_payload(
            link, user_id=interaction.user.id, channel_id=interaction.channel_id
        )
        if payload is None:
            await _send(
                interaction,
                "⚠️ That doesn't look like a Google Maps link. Copy the share "
                "link of a real hospital or prison and try again.",
            )
            return
        rid = await self.requests.create(
            kind="building", thread_id=DISCORD_THREAD, post_id=interaction.id,
            requester_name=interaction.user.display_name, requester_mc_id=None,
            payload=json.dumps(payload),
        )
        notes = []
        if self.bot.cfg.automation.dry_run:
            notes.append("🧪 dry-run is on: I'll report what I *would* build")
        if not self.bot.cfg.automation.building.enabled:
            notes.append(
                "⚠️ building automation is currently OFF — an admin must enable it"
            )
        note = ("\n" + " · ".join(notes)) if notes else ""
        await interaction.response.send_message(
            f"✅ Request **#{rid}** queued. I'll check the pin (must be a real "
            f"hospital or prison) and handle it at the next pass (~5 min).{note}",
            ephemeral=True,
        )

    # -- panel --------------------------------------------------------------

    async def post_panel(self, channel: discord.abc.Messageable) -> None:
        embed = discord.Embed(
            title="🚒 Fire & Rescue Academy — requests",
            colour=discord.Colour.red(),
            description=(
                "**🎓 Request a training**\n"
                "Pick the academy type and the course. I open a **free** class "
                "that's open to the whole alliance for 1 hour to join. Optional: "
                "a reminder when the course should be finished.\n\n"
                "**🏥 Request a building**\n"
                "Paste a Google Maps link to a **real hospital or prison** and "
                "I'll build it for the alliance. Clinics, police stations and "
                "the like are refused.\n\n"
                "_Requests are picked up within ~5 minutes; the result is "
                "announced in the log channel._"
            ),
        )
        await channel.send(embed=embed, view=RequestPanelView(self))

    # -- reminders ------------------------------------------------------------

    @tasks.loop(minutes=10)
    async def reminder_loop(self) -> None:
        async with self._lock:
            try:
                await self._send_due_reminders()
            except Exception:
                log.exception("Training reminder sweep failed")

    @reminder_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_due_reminders(self) -> None:
        for row in await self.reminders.due():
            text = (
                f"🔔 <@{row['discord_user_id']}> your **{row['training']}** "
                "course should be finished now — time to collect your people!"
            )
            sent = False
            channel = (
                self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            )
            if channel is not None:
                try:
                    await channel.send(text)
                    sent = True
                except discord.HTTPException as exc:
                    log.warning("Reminder to channel %s failed: %s",
                                row["channel_id"], exc)
            if not sent:
                try:
                    user = await self.bot.fetch_user(row["discord_user_id"])
                    await user.send(text)
                    sent = True
                except discord.HTTPException as exc:
                    log.warning("Reminder DM to %s failed: %s",
                                row["discord_user_id"], exc)
            # Mark posted either way — a permanently unreachable target must
            # not retry forever.
            await self.reminders.mark_posted(row["id"])
            await asyncio.sleep(1.0)
