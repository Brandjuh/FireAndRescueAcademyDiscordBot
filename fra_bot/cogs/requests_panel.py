"""Discord front-end for training and building requests.

A persistent panel gives members two buttons (the ``/training`` and
``/building`` slash commands open the exact same flows):

* **Request a training** — pick the academy type, pick the course, optionally
  toggle a reminder, submit. The request lands in ``automation_requests``
  exactly like a board post would, and the trainings poller opens the class
  at its next pass.
* **Request a building** — paste a Google Maps link to a real hospital or
  prison. The pin is checked IMMEDIATELY (location resolves, type is a
  hospital/prison); a good request queues for the funds-gated build, a bad
  one is rejected on the spot with the reason.

Every intake runs the contribution gate (approved ``!verify`` link → roster
contribution rate vs the feature's minimum) BEFORE queueing, and every
rejection still writes an ``automation_requests`` row (status ``skipped``)
so there is always a log entry — the admin-log publisher announces it.

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
from discord import app_commands
from discord.ext import commands, tasks

from ..db.repos import AutomationRepo, RemindersRepo, StateRepo
from ..geo.geocoder import GeocodeError
from ..geo.maps_links import find_maps_links
from ..mc.trainings_catalog import DISCIPLINES
from ..services.buildings import detect_building_type
from ..services.intake import INTAKE_REJECTED_FLAG, contribution_gate
from ..services.trainings import (
    AVAILABILITY_STATE_KEY,
    CLASS_CAPACITY,
    MAX_CLASSES_PER_REQUEST,
    clamp_class_count,
    merged_course_catalog,
)

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
    """Ephemeral, per-click chooser: academy type → course → number of
    classes → submit."""

    #: Courses per select page; the 25th slot is the page-flip option.
    PAGE_SIZE = 24

    def __init__(self, cog: "RequestsCog") -> None:
        super().__init__(timeout=300)
        self._cog = cog
        self.discipline: str | None = None
        self.training: str | None = None
        self.count = 1
        self.remind = False
        self._courses: list[tuple[str, int]] = []
        self._page = 0

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
        self.c_select = discord.ui.Select(
            placeholder=f"3️⃣ How many classes? (1 class = {CLASS_CAPACITY} people)",
            options=[
                discord.SelectOption(
                    label=(
                        f"{n} class — {n * CLASS_CAPACITY} people" if n == 1
                        else f"{n} classes — {n * CLASS_CAPACITY} people"
                    ),
                    value=str(n),
                    default=(n == 1),
                )
                for n in range(1, MAX_CLASSES_PER_REQUEST + 1)
            ],
            row=2,
        )
        self.c_select.callback = self._pick_count
        self.remind_btn = discord.ui.Button(
            label="🔕 Remind me when it's done: off",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        self.remind_btn.callback = self._toggle_remind
        self.go_btn = discord.ui.Button(
            label="Request training",
            style=discord.ButtonStyle.success,
            emoji="🎓",
            row=3,
        )
        self.go_btn.callback = self._submit
        for item in (self.d_select, self.t_select, self.c_select,
                     self.remind_btn, self.go_btn):
            self.add_item(item)

    async def _pick_discipline(self, interaction: discord.Interaction) -> None:
        self.discipline = self.d_select.values[0]
        self.training = None
        self._courses = await self._cog.courses_for(self.discipline)
        self._page = 0
        self._apply_course_page()
        self.t_select.disabled = False
        for option in self.d_select.options:
            option.default = option.value == self.discipline
        await interaction.response.edit_message(view=self)

    def _apply_course_page(self) -> None:
        """Fill the course select with the current page. Discord caps a
        select at 25 options; the live-harvested course lists can exceed
        that, so page 24 at a time with a flip option in the 25th slot."""
        pages = max(1, -(-len(self._courses) // self.PAGE_SIZE))
        self._page %= pages
        start = self._page * self.PAGE_SIZE
        chunk = self._courses[start:start + self.PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=(f"{name} ({days}d)" if days else name)[:100],
                value=name[:100],
                default=name == self.training,
            )
            for name, days in chunk
        ]
        if pages > 1:
            nxt = (self._page + 1) % pages + 1
            options.append(discord.SelectOption(
                label=f"➡️ More courses (to page {nxt}/{pages})"[:100],
                value="_page",
                description="flip to the next page of courses",
            ))
            self.t_select.placeholder = (
                f"2️⃣ Pick the course (page {self._page + 1}/{pages})"
            )
        else:
            self.t_select.placeholder = "2️⃣ Pick the course"
        self.t_select.options = options

    async def _pick_training(self, interaction: discord.Interaction) -> None:
        value = self.t_select.values[0]
        if value == "_page":
            self._page += 1
            self._apply_course_page()
            await interaction.response.edit_message(view=self)
            return
        self.training = value
        for option in self.t_select.options:
            option.default = option.value == self.training
        await interaction.response.edit_message(view=self)

    async def _pick_count(self, interaction: discord.Interaction) -> None:
        self.count = int(self.c_select.values[0])
        for option in self.c_select.options:
            option.default = option.value == self.c_select.values[0]
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
                interaction, self.discipline, self.training,
                remind=self.remind, count=self.count,
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
        await self._cog.open_training_chooser(interaction)

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
    remind: bool, count: int = 1, days: int | None = None,
) -> dict:
    """The automation_requests payload for a Discord training request —
    the same shape the board parser produces, plus the Discord flags.
    ``count`` asks for several copies of the same class (each holds
    :data:`CLASS_CAPACITY` people), capped at
    :data:`MAX_CLASSES_PER_REQUEST` per run. ``days`` overrides the
    built-in catalog duration (the chooser knows the live-harvested
    one)."""
    if days is None:
        days = DISCIPLINES.get(discipline, {}).get(training, 0)
    return {
        "trainings": [
            {
                "discipline": discipline, "name": training, "duration": days,
                "count": clamp_class_count(count),
            }
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
        self._kick_tasks: set[asyncio.Task] = set()
        self.reminder_loop.start()

    def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    # -- intake -----------------------------------------------------------

    async def _log_rejection(
        self, interaction: discord.Interaction, kind: str, payload: dict,
        detail: str, mc_user_id: int | None,
    ) -> int:
        """Every rejection still gets its automation_requests row (the log
        entry) — inserted terminal so the poller can never execute it. The
        payload flag keeps the publisher from DM-ing the member a second
        time; the admin-log embed still posts."""
        payload = dict(payload)
        payload[INTAKE_REJECTED_FLAG] = True
        rid = await self.requests.create(
            kind=kind, thread_id=DISCORD_THREAD, post_id=interaction.id,
            requester_name=interaction.user.display_name,
            requester_mc_id=mc_user_id,
            payload=json.dumps(payload), status="skipped", status_detail=detail,
        )
        log.info("%s request #%s from %s: %s",
                 kind, rid, interaction.user.display_name, detail)
        return rid

    async def submit_training(
        self, interaction: discord.Interaction, discipline: str, training: str,
        *, remind: bool, count: int = 1,
    ) -> None:
        count = clamp_class_count(count)
        payload = training_request_payload(
            discipline, training,
            user_id=interaction.user.id, channel_id=interaction.channel_id,
            remind=remind, count=count,
            days=await self.course_days(discipline, training),
        )
        verdict = await contribution_gate(
            self.bot.db, interaction.user.id,
            self.bot.cfg.automation.training.min_contribution_rate,
            members_interval_minutes=self.bot.cfg.sync.members_interval,
        )
        if not verdict.ok:
            await self._log_rejection(
                interaction, "training", payload, verdict.log_detail,
                verdict.mc_user_id,
            )
            await _send(
                interaction,
                f"❌ Your training request was not submitted — "
                f"{verdict.rejection_text}",
            )
            return
        rid = await self.requests.create(
            kind="training", thread_id=DISCORD_THREAD, post_id=interaction.id,
            requester_name=interaction.user.display_name,
            requester_mc_id=verdict.mc_user_id,
            payload=json.dumps(payload),
        )
        await self.bot.log_member_action(
            action="training_requested",
            detail=f"{training} ×{count} (request #{rid})",
            discord_user_id=interaction.user.id,
            mc_user_id=verdict.mc_user_id,
            actor_name=interaction.user.display_name,
        )
        days = payload["trainings"][0]["duration"]
        classes = (
            "a **free class**" if count == 1
            else f"**{count} free classes** ({count * CLASS_CAPACITY} seats)"
        )
        notes = []
        if remind:
            notes.append(f"🔔 I'll ping you in ~{days} day(s) when it should be done")
        if self.bot.cfg.automation.training.enabled:
            # Opening a training is FIRST priority: run the queue right now
            # instead of letting the member wait for the next scheduled pass.
            self._kick_training_queue()
            timing = f"I'm opening {classes} right now"
        else:
            timing = f"I'll open {classes} once automation is back on"
            notes.append(
                "⚠️ training automation is currently OFF — an admin must enable it"
            )
        note = ("\n" + " · ".join(notes)) if notes else ""
        await _send(
            interaction,
            f"✅ Request **#{rid}** — **{training}** "
            f"({_DISCIPLINE_LABEL.get(discipline, discipline)}). {timing}; "
            f"you'll get the result as a DM.{note}",
        )

    def _kick_training_queue(self) -> None:
        """Execute the training queue immediately in the background, sharing
        the scheduled poll's job lock so the two can never overlap. Any
        failure is retried by the normal poll — the request row is already
        committed."""
        async def _run() -> None:
            lock = self.bot.job_lock("board-trainings")
            try:
                # Bounded wait: if a poll holds the lock for minutes, give
                # up — the scheduled poll will process the row anyway. An
                # unbounded acquire could wedge behind a dead holder.
                await asyncio.wait_for(lock.acquire(), timeout=120.0)
            except asyncio.TimeoutError:
                log.warning(
                    "immediate training kick skipped: board-trainings lock "
                    "busy >120s (the scheduled poll will pick the row up)"
                )
                return
            try:
                await self.bot.trainings.execute_queue_now()
            except Exception:
                log.exception("immediate training execution failed")
            finally:
                lock.release()

        # Strong reference: an unreferenced task can be garbage-collected
        # mid-flight, silently dropping the immediate first attempt.
        task = asyncio.get_running_loop().create_task(_run())
        self._kick_tasks.add(task)
        task.add_done_callback(self._kick_tasks.discard)

    async def submit_building(
        self, interaction: discord.Interaction, link: str
    ) -> None:
        payload = building_request_payload(
            link, user_id=interaction.user.id, channel_id=interaction.channel_id
        )
        if payload is None:
            await self._log_rejection(
                interaction, "building",
                {
                    "link_raw": link[:400],
                    "discord_user_id": interaction.user.id,
                    "channel_id": interaction.channel_id,
                },
                "rejected at intake: not a Google Maps link", None,
            )
            await _send(
                interaction,
                "❌ Rejected — that doesn't look like a Google Maps link. Copy "
                "the share link of a real hospital or prison and try again.",
            )
            return

        verdict = await contribution_gate(
            self.bot.db, interaction.user.id,
            self.bot.cfg.automation.building.min_contribution_rate,
            members_interval_minutes=self.bot.cfg.sync.members_interval,
        )
        if not verdict.ok:
            await self._log_rejection(
                interaction, "building", payload, verdict.log_detail,
                verdict.mc_user_id,
            )
            await _send(
                interaction,
                f"❌ Your building request was not submitted — "
                f"{verdict.rejection_text}",
            )
            return

        # Location + type verdict RIGHT NOW, while the member is looking:
        # resolve the pin and detect hospital/prison before queueing. The
        # resolved coordinates travel in the payload, so the executor skips
        # its own geocode pass and goes straight to the funds gate.
        try:
            location = await self.bot.geocoder.resolve_maps_link(payload["link"])
        except GeocodeError as exc:
            if getattr(exc, "transient", False):
                # Geocoder hiccup, not the member's fault: queue as-is; the
                # poller resolves and validates the pin at its next pass.
                rid = await self.requests.create(
                    kind="building", thread_id=DISCORD_THREAD,
                    post_id=interaction.id,
                    requester_name=interaction.user.display_name,
                    requester_mc_id=verdict.mc_user_id,
                    payload=json.dumps(payload),
                )
                await _send(
                    interaction,
                    f"✅ Request **#{rid}** queued — I couldn't reach the "
                    "geocoder just now, so the pin check happens at the next "
                    "pass (~5 min). You'll be notified of the outcome.",
                )
                return
            await self._log_rejection(
                interaction, "building", payload,
                f"rejected at intake: geocoding failed: {exc}",
                verdict.mc_user_id,
            )
            await _send(
                interaction,
                "❌ Rejected — the location could not be resolved to GPS "
                "coordinates. Please use a Google Maps place link with a "
                f"visible marker. ({exc})",
            )
            return

        building_type = detect_building_type(
            location.address, location.place_text, location.place_type
        )
        payload.update({
            "latitude": location.latitude,
            "longitude": location.longitude,
            "address": location.address,
            "building_type": building_type,
        })
        if building_type is None:
            await self._log_rejection(
                interaction, "building", payload,
                "rejected at intake: location is not a hospital or prison",
                verdict.mc_user_id,
            )
            await _send(
                interaction,
                f"❌ Rejected — **{location.place_text or location.address or 'the pin'}** "
                "was not detected as a hospital or a prison. Only hospitals "
                "and prisons are built automatically.",
            )
            return

        rid = await self.requests.create(
            kind="building", thread_id=DISCORD_THREAD, post_id=interaction.id,
            requester_name=interaction.user.display_name,
            requester_mc_id=verdict.mc_user_id,
            payload=json.dumps(payload),
        )
        await self.bot.log_member_action(
            action="building_requested",
            detail=f"{building_type} at {location.address or 'pin'} "
                   f"(request #{rid})",
            discord_user_id=interaction.user.id,
            mc_user_id=verdict.mc_user_id,
            actor_name=interaction.user.display_name,
        )
        notes = []
        if self.bot.cfg.automation.dry_run:
            notes.append("🧪 dry-run is on: I'll report what I *would* build")
        if not self.bot.cfg.automation.building.enabled:
            notes.append(
                "⚠️ building automation is currently OFF — an admin must enable it"
            )
        note = ("\n" + " · ".join(notes)) if notes else ""
        emoji = "🏥" if building_type == "hospital" else "🔒"
        name = location.address.split(",")[0] if location.address else building_type
        await _send(
            interaction,
            f"✅ Request **#{rid}** accepted — {emoji} **{building_type}** "
            f"“{name}” at {location.latitude:.5f}, {location.longitude:.5f}. "
            f"Alliance funds are checked next; it builds at the next pass "
            f"(~5 min) or waits until funds allow.{note}",
        )

    # -- slash commands (same flows as the panel buttons) -----------------

    async def open_training_chooser(self, interaction: discord.Interaction) -> None:
        """The training flow behind the panel button and ``/training``,
        headed by the cached free-class counts so members see availability
        at a glance (the hourly guide walk keeps the cache fresh — walking
        every academy per click would hammer the game)."""
        text = ("Pick the academy type and the course, then press "
                "**Request training**:")
        line = await self._availability_line()
        if line:
            text = f"{line}\n\n{text}"
        await interaction.response.send_message(
            text, view=TrainingChooserView(self), ephemeral=True,
        )

    async def courses_for(self, discipline: str) -> list[tuple[str, int]]:
        """(name, days) choices for the course select: the live-harvested
        academy course list, built-in catalog as bootstrap fallback."""
        catalog = await merged_course_catalog(StateRepo(self.bot.db))
        return sorted(catalog.get(discipline, {}).items())

    async def course_days(self, discipline: str, training: str) -> int:
        """Course duration for the reminder estimate (live first)."""
        catalog = await merged_course_catalog(StateRepo(self.bot.db))
        days = catalog.get(discipline, {}).get(training)
        if days:
            return int(days)
        return DISCIPLINES.get(discipline, {}).get(training, 0)

    async def _availability_line(self) -> str | None:
        """Cached free classrooms per agency, or None when never collected."""
        raw = await StateRepo(self.bot.db).get(AVAILABILITY_STATE_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        counts = data.get("counts")
        if not isinstance(counts, dict) or not counts:
            return None
        parts = [
            f"{_DISCIPLINE_LABEL.get(key, key)} **{int(count)}**"
            for key, count in counts.items()
        ]
        line = "📊 Free classes: " + " · ".join(parts)
        if data.get("at"):
            line += f" (as of <t:{int(data['at'])}:R>)"
        return line

    @app_commands.command(
        name="training",
        description="Request a training — same flow as the request panel",
    )
    async def slash_training(self, interaction: discord.Interaction) -> None:
        await self.open_training_chooser(interaction)

    @app_commands.command(
        name="building",
        description="Request a building — Google Maps link to a real hospital or prison",
    )
    async def slash_building(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BuildingRequestModal(self))

    # -- panel (posted/maintained by the panel keeper) -----------------------

    def panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🚒 Fire & Rescue Academy — requests",
            colour=discord.Colour.red(),
            description=(
                "**🎓 Request a training** (or `/training`)\n"
                "Pick the academy type, the course and how many classes "
                f"(up to {MAX_CLASSES_PER_REQUEST}; each class holds "
                f"{CLASS_CAPACITY} people). Classes are **free** and open to "
                "the whole alliance for 1 hour to join. Optional: a reminder "
                "when the course should be finished.\n\n"
                "**🏥 Request a building** (or `/building`)\n"
                "Paste a Google Maps link to a **real hospital or prison** - "
                "the pin is checked on the spot and you're told immediately "
                "whether it's accepted. Clinics, police stations and the like "
                "are refused.\n\n"
                "_You need a verified account (`!verify`) with enough alliance "
                "contribution. Accepted requests run within ~5 minutes; results "
                "are announced in the log channel._"
            ),
        )

    def panel_view(self) -> discord.ui.View:
        return RequestPanelView(self)

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
