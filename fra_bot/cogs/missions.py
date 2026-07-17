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

Every intake (panel and slash alike) runs the contribution gate before
queueing; a refused request still writes a ``scheduled_missions`` row
(status ``cancelled``) so there is always a log entry, announced to the
admin log. The large-scale chooser offers the presets and the game's
saved missions (cached from the mission form) as one-click options.
Custom Own missions are deliberately NOT requestable through Discord —
the required-unit values don't fit its forms; the in-game mission board
carries the copy-paste template for those.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
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
from ..services.intake import contribution_gate

log = logging.getLogger(__name__)

PANEL_EVENT_ID = "fra:mission:event"
PANEL_LARGE_ID = "fra:mission:large"
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
    the member picked in the chooser. A saved-mission pick from the chooser's
    list prefills the name, still editable. (Custom Own missions are NOT
    offered here: their required-unit values don't fit Discord's modal
    limits — the in-game mission board carries the copy-paste template.)"""

    def __init__(
        self,
        cog: "MissionsCog",
        *,
        kind: str,
        schedule: str,
        source: str,
        preset: str | None = None,
        saved_default: str | None = None,
    ) -> None:
        super().__init__(title="Request a mission")
        self._cog = cog
        self._kind = kind
        self._schedule = schedule
        self._source = source
        self._preset = preset

        self.location = discord.ui.TextInput(
            label="Location (place name or Google Maps link)",
            placeholder="e.g. Grand Rapids  ·  or a maps link",
            max_length=200,
        )
        self.add_item(self.location)

        self.saved = None
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
        elif source == "saved":
            self.saved = discord.ui.TextInput(
                label="Saved mission name",
                placeholder="exactly as it appears in the game's dropdown",
                max_length=60,
                default=(saved_default or "")[:60] or None,
            )
            self.add_item(self.saved)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog.submit_request(
            interaction,
            location=str(self.location),
            kind=self._kind,
            schedule=self._schedule,
            source=self._source,
            preset=self._preset,
            saved=str(self.saved) if self.saved else None,
            event_type=str(self.event_type) if self.event_type else None,
            area=str(self.area) if self.area else None,
            shape=str(self.shape) if self.shape else None,
            call_volume=str(self.call_volume) if self.call_volume else None,
        )


class MissionChooserView(discord.ui.View):
    """Ephemeral chooser for ONE kind — the member already picked **Alliance
    event** or **Large scale mission** (panel button or the two-way menu
    behind a bare ``/mission``). Events only choose a schedule (their options
    live in the modal); large missions also pick the mission data — the
    presets as one-click options, plus the game's **saved missions** as a
    pick-from list. Custom Own missions are NOT offered: Discord's UI can't
    carry the required-unit values, the in-game mission board can (it has
    the copy-paste template). Not persistent (created per click)."""

    def __init__(
        self, cog: "MissionsCog", kind: str, saved_names: list | None = None
    ) -> None:
        super().__init__(timeout=300)
        self._cog = cog
        self.kind = kind                # fixed: "large" | "event"
        self.schedule = "once"
        self.source = "preset"          # preset | saved
        self.preset: str | None = None  # preset display name, if one was picked
        self.pick: str | None = None    # a saved-mission name picked from the list

        self.schedule_select = discord.ui.Select(
            placeholder="Schedule — one-time or recurring",
            options=[
                discord.SelectOption(label="One-time", value="once", default=True),
                discord.SelectOption(label="Recurring (add to rotation)",
                                     value="recurring", emoji="🔁"),
            ],
            row=0,
        )
        self.schedule_select.callback = self._pick_schedule
        self.source_select: discord.ui.Select | None = None
        self.saved_select: discord.ui.Select | None = None
        if kind == "large":
            self.source_select = discord.ui.Select(
                placeholder="Mission data — a preset or a saved mission",
                options=[
                    discord.SelectOption(
                        label="Standard large mission", value="preset", default=True,
                        description="a standard mission at the location", emoji="🚨",
                    ),
                    *[
                        discord.SelectOption(label=f"Preset: {name}"[:100],
                                             value=f"preset:{name}"[:100], emoji="📋")
                        for name in sorted(_PRESET_BY_NAME)
                    ],
                    discord.SelectOption(
                        label="Saved mission", value="saved",
                        description="pick from the list below, or type the name",
                        emoji="💾",
                    ),
                ],
                row=1,
            )
            self.source_select.callback = self._pick_source
            if saved_names:
                self.saved_select = discord.ui.Select(
                    placeholder="💾 Saved missions — pick one",
                    options=[
                        discord.SelectOption(label=str(name)[:100],
                                             value=str(name)[:100], emoji="💾")
                        for name in saved_names
                    ][:25],
                    row=2,
                )
                self.saved_select.callback = self._pick_saved
        self.go_btn = discord.ui.Button(
            label="Continue", style=discord.ButtonStyle.primary, emoji="➡️", row=4,
        )
        self.go_btn.callback = self._cont
        for item in (self.schedule_select,
                     *((self.source_select,) if self.source_select else ()),
                     *((self.saved_select,) if self.saved_select else ()),
                     self.go_btn):
            self.add_item(item)

    @staticmethod
    def _mark(select: discord.ui.Select, value: str | None) -> None:
        for option in select.options:
            option.default = option.value == value

    async def _pick_schedule(self, interaction: discord.Interaction) -> None:
        self.schedule = self.schedule_select.values[0]
        self._mark(self.schedule_select, self.schedule)
        await interaction.response.edit_message(view=self)

    async def _pick_source(self, interaction: discord.Interaction) -> None:
        value = self.source_select.values[0]
        if value.startswith("preset:"):
            self.source, self.preset = "preset", value[len("preset:"):]
        else:
            self.source, self.preset = value, None
        # A preset choice overrides an earlier saved-mission pick.
        if self.source != "saved":
            self.pick = None
            if self.saved_select is not None:
                self._mark(self.saved_select, None)
        self._mark(self.source_select, value)
        await interaction.response.edit_message(view=self)

    async def _pick_saved(self, interaction: discord.Interaction) -> None:
        assert self.saved_select is not None
        self.pick = self.saved_select.values[0]
        self.source, self.preset = "saved", None
        self._mark(self.saved_select, self.pick)
        self._mark(self.source_select, "saved")
        await interaction.response.edit_message(view=self)

    async def _cont(self, interaction: discord.Interaction) -> None:
        # For an event the mission-data picks are ignored — the modal asks
        # for the event type / area / shape / call volume instead.
        modal = MissionDetailsModal(
            self._cog, kind=self.kind, schedule=self.schedule,
            source=self.source, preset=self.preset,
            saved_default=self.pick if self.source == "saved" else None,
        )
        await interaction.response.send_modal(modal)


class MissionKindPickView(discord.ui.View):
    """The two-way choice menu — **Alliance event** or **Large scale
    alliance mission** — shown ephemerally by a bare ``/mission``. The
    panel carries the same two buttons persistently."""

    def __init__(self, cog: "MissionsCog") -> None:
        super().__init__(timeout=300)
        self._cog = cog
        event_btn = discord.ui.Button(
            label="Alliance event", style=discord.ButtonStyle.primary, emoji="🎉",
        )
        event_btn.callback = self._pick_event
        large_btn = discord.ui.Button(
            label="Large scale alliance mission",
            style=discord.ButtonStyle.primary, emoji="🚨",
        )
        large_btn.callback = self._pick_large
        self.add_item(event_btn)
        self.add_item(large_btn)

    async def _pick_event(self, interaction: discord.Interaction) -> None:
        await self._cog.open_chooser(interaction, "event")

    async def _pick_large(self, interaction: discord.Interaction) -> None:
        await self._cog.open_chooser(interaction, "large")


class MissionPanelView(discord.ui.View):
    """Persistent panel; re-registered at startup so its buttons survive
    restarts. The kind choice IS the panel: one button per kind."""

    def __init__(self, cog: "MissionsCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(
        label="Alliance event",
        style=discord.ButtonStyle.primary,
        emoji="🎉",
        custom_id=PANEL_EVENT_ID,
    )
    async def request_event(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._cog.open_chooser(interaction, "event")

    @discord.ui.button(
        label="Large scale alliance mission",
        style=discord.ButtonStyle.primary,
        emoji="🚨",
        custom_id=PANEL_LARGE_ID,
    )
    async def request_large(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._cog.open_chooser(interaction, "large")


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

    async def open_chooser(self, interaction: discord.Interaction, kind: str) -> None:
        """The guided flow for one kind, behind the panel buttons and the
        two-way menu of a bare ``/mission``. The large chooser is seeded
        with the game's saved missions (cached from the mission form; DB
        history of successfully used names as fallback)."""
        if kind == "event":
            text = ("🎉 **Alliance event**: pick a schedule, then press "
                    "**Continue** for the location and event options.")
            saved_names: list[str] = []
        else:
            text = ("🚨 **Large scale alliance mission**: pick the schedule "
                    "and mission data, then press **Continue**.\n"
                    "-# 🛠️ Custom Own missions can't be requested through "
                    "Discord (its forms can't carry the unit values). Use "
                    "the in-game mission board, it has a copy-paste template.")
            saved_names = await self.service.saved_mission_names()
            if not saved_names:
                saved_names = await self.repo.previous_saved_names()
        await interaction.response.send_message(
            text, view=MissionChooserView(self, kind, saved_names), ephemeral=True,
        )

    async def submit_request(
        self,
        interaction: discord.Interaction,
        *,
        location: str,
        kind: str,
        schedule: str,
        source: str,
        preset: str | None = None,
        saved: str | None = None,
        event_type: str | None = None,
        area: str | None = None,
        shape: str | None = None,
        call_volume: str | None = None,
    ) -> None:
        try:
            spec = build_spec(
                location=location, kind=kind, schedule=schedule,
                preset=preset, saved=saved,
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
        location="Place name or maps link — leave empty to open the guided chooser (same as the panel)",
        kind="Alliance event, or a large scale alliance mission (default)",
        schedule="One-time, or recurring (adds it to the rotation list)",
        preset="Large scale: optional preset mission type",
        saved="Large scale: start a saved mission by its name (customs: in-game board only)",
        event_type="Event: which event (default Random picks a standard one)",
        area="Event: footprint size",
        shape="Event: footprint shape",
        call_volume="Event: mission call volume in seconds",
    )
    async def slash_mission(
        self,
        interaction: discord.Interaction,
        location: str | None = None,
        kind: Literal["large", "event"] = "large",
        schedule: Literal["once", "recurring"] = "once",
        preset: Literal["Major fire", "Unannounced demonstration", "Pile-up", "Bomb Explosion"] | None = None,
        saved: str | None = None,
        event_type: Literal[
            "Random", "Storm", "Civil Unrest", "Storm Surge", "Fall weather",
            "Winter weather", "Spring weather", "Summer weather", "Sports Event",
        ] = "Random",
        area: Literal["Small", "Medium", "Large"] = "Medium",
        shape: Literal["Rectangle", "Circle"] = "Rectangle",
        call_volume: Literal["30", "45", "60"] = "45",
    ) -> None:
        if not (location or "").strip():
            # A bare /mission opens the same two-way choice as the panel.
            await interaction.response.send_message(
                "What would you like to start?",
                view=MissionKindPickView(self),
                ephemeral=True,
            )
            return
        try:
            spec = build_spec(
                location=location, kind=kind, schedule=schedule,
                preset=preset, saved=saved,
                event_type=event_type, area=area, shape=shape, call_volume=call_volume,
            )
        except (ValueError, MissionSpecError) as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await self._enqueue_and_ack(interaction, spec)

    async def _enqueue_and_ack(
        self, interaction: discord.Interaction, spec: MissionSpec
    ) -> None:
        cfg = self.bot.cfg.automation
        min_rate = (
            cfg.events.min_contribution_rate if spec.kind == "event"
            else cfg.mission.min_contribution_rate
        )
        verdict = await contribution_gate(
            self.bot.db, interaction.user.id, min_rate,
            members_interval_minutes=self.bot.cfg.sync.members_interval,
        )
        if not verdict.ok:
            # The log entry: a terminal row, announced to the admin log only
            # (channel_id None) — the member gets the reason right here.
            await self.service.enqueue_discord(
                spec,
                requester_name=interaction.user.display_name,
                requester_mc_id=verdict.mc_user_id,
                discord_user_id=interaction.user.id,
                channel_id=None,
                status="cancelled", status_detail=verdict.log_detail,
            )
            noun = "event" if spec.kind == "event" else "mission"
            await self._respond(
                interaction,
                f"❌ Your {noun} request was not submitted — "
                f"{verdict.rejection_text}",
                ephemeral=True,
            )
            return
        mission_id = await self.service.enqueue_discord(
            spec,
            requester_name=interaction.user.display_name,
            requester_mc_id=verdict.mc_user_id,
            discord_user_id=interaction.user.id,
            channel_id=interaction.channel_id,
        )
        await self.bot.log_member_action(
            action=(
                "event_requested" if spec.kind == "event"
                else "mission_requested"
            ),
            detail=f"{spec.describe()} at {spec.location_text} "
                   f"(request #{mission_id})"
                   + (" — recurring" if spec.recurring else ""),
            discord_user_id=interaction.user.id,
            mc_user_id=verdict.mc_user_id,
            actor_name=interaction.user.display_name,
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
                "**🎉 Alliance event**\n"
                "Pick a schedule, then give the location and the event "
                "options (type, area, shape, call volume).\n\n"
                "**🚨 Large scale alliance mission**\n"
                "Pick the schedule and the mission data: a **preset**, or "
                "one of the game's **saved missions** (picked from a list). "
                "Then give the location. *Custom Own missions can only be "
                "requested on the in-game mission board (Discord's forms "
                "can't carry the unit values).*\n\n"
                "The bot queues your request and starts it at the next free "
                "slot. **/mission** does the same: bare it opens this menu, "
                "with options it queues directly. You need a verified "
                "account (`!verify`) with enough alliance contribution."
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
