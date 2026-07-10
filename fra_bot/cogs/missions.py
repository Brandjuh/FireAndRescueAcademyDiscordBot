"""Discord front-end for the unified mission/event system.

Members request a mission through the ``/mission`` slash command or a
persistent panel (button → a small chooser → a modal). A request carries:

* a **location** (free text like "Grand Rapids", or a maps link),
* a **kind** — an alliance ``event`` or a ``large`` scale alliance mission,
* a **source** — a preset, a member-supplied ``custom`` Own mission, or one
  picked from MissionChief's ``saved`` missions dropdown,
* a **schedule** — one-time, or recurring (joins the admin rotation list).

Requests queue in ``scheduled_missions``; the
:class:`~fra_bot.services.missions.MissionScheduler` starts them at the next
free slot. Outcomes are announced back to Discord by a publisher loop (never
to MissionChief while in dry-run), so nothing here raises red flags.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..db.repos import MissionsRepo
from ..mc.parsers.events import EVENT_TYPES, resolve_event_type
from ..mc.parsers.mission_spec import PRESET_TYPE_IDS, MissionSpec, MissionSpecError
from ..mc.parsers.missions_custom import (
    CustomMission,
    CustomMissionError,
    parse_custom_values,
)

log = logging.getLogger(__name__)

PANEL_BUTTON_ID = "fra:mission:new"
_POST_PAUSE_SECONDS = 1.2
_DESC_LIMIT = 4096

# Preset name -> mission_type_id, for the slash-command choice.
_PRESET_BY_NAME = {name: type_id for type_id, name in PRESET_TYPE_IDS.items()}

_STATUS_STYLE = {
    "done": ("🚨 Mission started", discord.Colour.green()),
    "failed": ("❌ Mission failed", discord.Colour.red()),
    "skipped": ("🧪 Mission (dry-run)", discord.Colour.gold()),
    "waiting": ("⏳ Mission queued", discord.Colour.blurple()),
    "cancelled": ("🚫 Mission cancelled", discord.Colour.light_grey()),
}


def build_spec(
    *,
    location: str,
    kind: str = "large",
    schedule: str = "once",
    preset: str | None = None,
    saved: str | None = None,
    custom: str | None = None,
    name: str | None = None,
    event_type: str | None = None,
    area: str | None = None,
    shape: str | None = None,
    call_volume: str | None = None,
) -> MissionSpec:
    """Turn raw intake fields (slash args or modal text) into a validated
    :class:`MissionSpec`. Raises :class:`MissionSpecError` on bad input."""
    kind = (kind or "large").lower()

    # Alliance event: its own knobs, none of the large-mission source options.
    if kind == "event":
        try:
            event_type_id, event_random = resolve_event_type(event_type)
        except ValueError as exc:
            raise MissionSpecError(str(exc)) from exc
        return MissionSpec(
            location_text=location,
            kind="event",
            source="preset",
            event_type_id=event_type_id,
            event_random=event_random,
            area=(area or "medium"),
            shape=(shape or "rectangle"),
            call_volume=(call_volume or "45"),
            recurring=(schedule or "once").lower() == "recurring",
        ).validate()

    saved = (saved or "").strip() or None
    custom = (custom or "").strip() or None
    if saved and custom:
        raise MissionSpecError("choose either a saved mission or custom data, not both")

    source = "preset"
    custom_mission = None
    if custom:
        source = "custom"
        try:
            values = parse_custom_values(custom)
        except CustomMissionError as exc:
            raise MissionSpecError(str(exc)) from exc
        caption = (name or "").strip() or location
        custom_mission = CustomMission(caption=caption, values=values)
    elif saved:
        source = "saved"

    preset_type_id = _PRESET_BY_NAME.get(preset) if preset else None

    return MissionSpec(
        location_text=location,
        kind=kind,
        source=source,
        preset_type_id=preset_type_id,
        custom=custom_mission,
        saved_name=saved,
        recurring=(schedule or "once").lower() == "recurring",
    ).validate()


# ---------------------------------------------------------------------------
# Panel: button -> chooser (selects) -> modal
# ---------------------------------------------------------------------------

class MissionDetailsModal(discord.ui.Modal):
    """Collects the free-text fields; which ones appear depends on the source
    the member picked in the chooser."""

    def __init__(self, cog: "MissionsCog", *, kind: str, schedule: str, source: str) -> None:
        super().__init__(title="Request a mission")
        self._cog = cog
        self._kind = kind
        self._schedule = schedule
        self._source = source

        self.location = discord.ui.TextInput(
            label="Location (place name or Google Maps link)",
            placeholder="e.g. Grand Rapids  ·  or a maps link",
            max_length=200,
        )
        self.add_item(self.location)

        self.name = None
        self.saved = None
        self.custom = None
        self.event_type = None
        self.area = None
        self.shape = None
        self.call_volume = None
        if kind == "event":
            self.event_type = discord.ui.TextInput(
                label="Event type", required=False, default="random", max_length=20,
                placeholder="Storm / Civil Unrest / Sports Event / … or random",
            )
            self.area = discord.ui.TextInput(
                label="Area", required=False, default="medium", max_length=8,
                placeholder="small / medium / large",
            )
            self.shape = discord.ui.TextInput(
                label="Shape", required=False, default="rectangle", max_length=10,
                placeholder="rectangle / circle",
            )
            self.call_volume = discord.ui.TextInput(
                label="Call volume (seconds)", required=False, default="45", max_length=2,
                placeholder="30 / 45 / 60",
            )
            self.add_item(self.event_type)
            self.add_item(self.area)
            self.add_item(self.shape)
            self.add_item(self.call_volume)
        elif source == "custom":
            self.name = discord.ui.TextInput(
                label="Mission name", required=False, max_length=30,
                placeholder="defaults to the location",
            )
            self.custom = discord.ui.TextInput(
                label="Required units (key=value …)",
                style=discord.TextStyle.paragraph, max_length=500,
                placeholder="need_lf=25 need_elw1=6 water_needed=15000",
            )
            self.add_item(self.name)
            self.add_item(self.custom)
        elif source == "saved":
            self.saved = discord.ui.TextInput(
                label="Saved mission name",
                placeholder="exactly as it appears in the game's dropdown",
                max_length=60,
            )
            self.add_item(self.saved)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog.submit_request(
            interaction,
            location=str(self.location),
            kind=self._kind,
            schedule=self._schedule,
            source=self._source,
            name=str(self.name) if self.name else None,
            saved=str(self.saved) if self.saved else None,
            custom=str(self.custom) if self.custom else None,
            event_type=str(self.event_type) if self.event_type else None,
            area=str(self.area) if self.area else None,
            shape=str(self.shape) if self.shape else None,
            call_volume=str(self.call_volume) if self.call_volume else None,
        )


class MissionChooserView(discord.ui.View):
    """Ephemeral chooser shown after the panel button: pick kind / schedule /
    source, then open the details modal. Not persistent (created per click)."""

    def __init__(self, cog: "MissionsCog") -> None:
        super().__init__(timeout=300)
        self._cog = cog
        self.kind = "large"
        self.schedule = "once"
        self.source = "preset"

    @discord.ui.select(
        placeholder="Kind — large scale mission or event",
        options=[
            discord.SelectOption(label="Large scale alliance mission", value="large", default=True, emoji="🚨"),
            discord.SelectOption(label="Alliance event", value="event", emoji="🎉"),
        ],
    )
    async def pick_kind(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.kind = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="Schedule — one-time or recurring",
        options=[
            discord.SelectOption(label="One-time", value="once", default=True),
            discord.SelectOption(label="Recurring (add to rotation)", value="recurring", emoji="🔁"),
        ],
    )
    async def pick_schedule(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.schedule = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="Mission data — preset, custom, or a saved mission",
        options=[
            discord.SelectOption(label="Preset", value="preset", default=True,
                                 description="a standard mission at the location"),
            discord.SelectOption(label="Custom Own mission", value="custom",
                                 description="you supply the required units"),
            discord.SelectOption(label="Saved mission", value="saved",
                                 description="pick one from the game's dropdown"),
        ],
    )
    async def pick_source(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.source = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, emoji="➡️")
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # For an event the "mission data" select is ignored — the modal asks
        # for the event type / area / shape / call volume instead.
        await interaction.response.send_modal(
            MissionDetailsModal(self._cog, kind=self.kind, schedule=self.schedule, source=self.source)
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
        await interaction.response.send_message(
            "Choose what you'd like to start, then press **Continue**:",
            view=MissionChooserView(self._cog),
            ephemeral=True,
        )


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

    async def submit_request(
        self,
        interaction: discord.Interaction,
        *,
        location: str,
        kind: str,
        schedule: str,
        source: str,
        name: str | None = None,
        saved: str | None = None,
        custom: str | None = None,
        event_type: str | None = None,
        area: str | None = None,
        shape: str | None = None,
        call_volume: str | None = None,
    ) -> None:
        try:
            spec = build_spec(
                location=location, kind=kind, schedule=schedule,
                saved=saved, custom=custom, name=name,
                event_type=event_type, area=area, shape=shape, call_volume=call_volume,
            )
        except (ValueError, MissionSpecError) as exc:
            await self._respond(interaction, f"⚠️ {exc}", ephemeral=True)
            return
        await self._enqueue_and_ack(interaction, spec)

    @app_commands.command(
        name="mission",
        description="Request an alliance event or large scale mission (queued to the next free slot)",
    )
    @app_commands.describe(
        location="Place name (e.g. Grand Rapids) or a Google Maps link",
        kind="Alliance event, or a large scale alliance mission (default)",
        schedule="One-time, or recurring (adds it to the rotation list)",
        preset="Large scale: optional preset mission type",
        saved="Large scale: start a saved mission by its name",
        custom="Large scale: custom Own mission units, e.g. need_lf=25 need_elw1=6 water_needed=15000",
        name="Large scale: name for a custom mission",
        event_type="Event: which event (default Random picks a standard one)",
        area="Event: footprint size",
        shape="Event: footprint shape",
        call_volume="Event: mission call volume in seconds",
    )
    async def slash_mission(
        self,
        interaction: discord.Interaction,
        location: str,
        kind: Literal["large", "event"] = "large",
        schedule: Literal["once", "recurring"] = "once",
        preset: Literal["Major fire", "Unannounced demonstration", "Pile-up", "Bomb Explosion"] | None = None,
        saved: str | None = None,
        custom: str | None = None,
        name: str | None = None,
        event_type: Literal[
            "Random", "Storm", "Civil Unrest", "Storm Surge", "Fall weather",
            "Winter weather", "Spring weather", "Summer weather", "Sports Event",
        ] = "Random",
        area: Literal["Small", "Medium", "Large"] = "Medium",
        shape: Literal["Rectangle", "Circle"] = "Rectangle",
        call_volume: Literal["30", "45", "60"] = "45",
    ) -> None:
        try:
            spec = build_spec(
                location=location, kind=kind, schedule=schedule,
                preset=preset, saved=saved, custom=custom, name=name,
                event_type=event_type, area=area, shape=shape, call_volume=call_volume,
            )
        except (ValueError, MissionSpecError) as exc:
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
        note = "" if self.bot.cfg.automation.mission.enabled else (
            "\n_The mission scheduler is currently off, so this will wait until "
            "an admin enables it._"
        )
        sched = " · 🔁 recurring (joins the rotation)" if spec.recurring else ""
        await self._respond(
            interaction,
            f"✅ Request **#{mission_id}** queued — {spec.describe()}{sched}, at "
            f"*{spec.location_text}*. It will start at the next free alliance "
            f"mission slot.{note}",
            ephemeral=True,
        )

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str, *, ephemeral: bool) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    # -- panel (posted/maintained by the panel keeper) -------------------

    def panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🚨 Request an alliance mission or event",
            colour=discord.Colour.blurple(),
            description=(
                "Click below to request a **large scale alliance mission** or an "
                "**alliance event**. Give a location (a place name like "
                "*Grand Rapids*, or a maps link), choose one-time or recurring, "
                "and optionally supply custom Own-mission units or pick a saved "
                "mission. The bot queues it and starts it at the next free slot.\n\n"
                "You can also use the **/mission** slash command."
            ),
        )

    def panel_view(self) -> discord.ui.View:
        return MissionPanelView(self)

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
            lines = [f"**#{row['id']}** — {row['kind']} · {row['mission_source']} · "
                     f"requested by {requester}"]
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
