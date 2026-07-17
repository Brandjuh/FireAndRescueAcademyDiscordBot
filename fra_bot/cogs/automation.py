"""Announces board-automation request outcomes to Discord.

Like the other publishers, this is driven by ``posted_at IS NULL`` rows
in ``automation_requests`` — a crash can at worst repeat one embed, and
enabling/disabling automation never floods old history.

Besides the admin-log embed, a published row can trigger (both mirror the
reference bot's behaviour):

* a **requester DM** when the request came from the Discord panel
  (``payload.discord_user_id``) — success and sent-to-admins texts. When
  the Discord DM can't be delivered (DMs closed), the fallback is an
  **in-game PM** (mirrored into the DM forum) — never a mention in the
  request channel;
* an **approve/deny decision embed** in the ``admin_approvals`` channel
  when a training/building request fails — Approve re-queues the request
  for a fresh attempt, Deny closes it with a reason that is posted back to
  the board and DM'd to a Discord requester. The buttons are restart-safe:
  the request id lives in the ``custom_id``, so no in-memory state is
  needed (the reference bot's one weakness here).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re

import discord
from discord.ext import commands, tasks

from ..db.repos import AutomationRepo

log = logging.getLogger(__name__)

_TITLE_LIMIT = 256
_DESC_LIMIT = 4096
_FIELD_LIMIT = 1024

_KIND_LABEL = {"training": "🎓 Training", "building": "🏗️ Building", "event": "🚨 Event"}
_STATUS_COLOUR = {
    "done": discord.Colour.green(),
    "failed": discord.Colour.red(),
    "skipped": discord.Colour.light_grey(),
    "waiting": discord.Colour.orange(),
}
_STATUS_ICON = {"done": "✅", "failed": "❌", "skipped": "⏭️", "waiting": "⏳"}

# The reference bot's "how to join" block, Discord-markdown flavour.
_JOIN_INSTRUCTIONS_MD = (
    "\n\n**How to add people to the course**\n"
    "**Browser/Desktop:** Open MissionChief in your browser, open the "
    "academy building or active training course, choose the course, then "
    "use the participant/personnel option to add the required people.\n"
    "**Phone:** Open MissionChief in your mobile browser, open the same "
    "academy building or active training course, then use the course "
    "participant/personnel option to add the required people."
)


def _requester_dm_text(kind: str, status: str, detail: str, data: dict) -> str | None:
    """The reference bot's requester DM for a published outcome, or None
    when this outcome doesn't DM."""
    if kind == "training":
        if status == "done":
            opened = [
                r for r in data.get("results", [])
                if r.get("outcome") == "opened"
            ]
            if not opened:
                return None
            # A multi-class request opens the same course several times;
            # group those into one line instead of repeating it.
            grouped: dict[str, list] = {}
            for r in opened:
                grouped.setdefault(r["training"], []).append(r.get("building_id"))
            lines = []
            for training, buildings in grouped.items():
                n = len(buildings)
                classes = "1 class" if n == 1 else f"{n} classes"
                lines.append(
                    "Your training has been started automatically: "
                    f"**{training}** ({classes}, free)."
                )
                for building_id in dict.fromkeys(b for b in buildings if b):
                    lines.append(
                        "Academy: https://www.missionchief.com/buildings/"
                        f"{building_id}"
                    )
            return "\n".join(lines) + _JOIN_INSTRUCTIONS_MD
        if status == "failed":
            return (
                "Your training request could not be opened automatically "
                f"and has been sent to admins for manual start.\nReason: {detail}"
            )
    if kind == "building":
        if status == "done":
            emoji = {"hospital": "🏥", "prison": "🔒"}.get(
                data.get("building_type"), "🏢"
            )
            name = (data.get("address") or "your location").split(",")[0]
            lines = [
                "✅ Your building request has been **APPROVED**.",
                "",
                f"{emoji} **{data.get('building_type', 'building')}**: {name}",
            ]
            if data.get("latitude") is not None:
                lines.append(
                    f"📍 Coordinates: {data['latitude']:.5f}, {data['longitude']:.5f}"
                )
            if data.get("address"):
                lines.append(f"📫 Address: {data['address']}")
            if data.get("building_id"):
                lines.append(
                    "Building: https://www.missionchief.com/buildings/"
                    f"{data['building_id']}"
                )
            return "\n".join(lines)
        if status == "failed":
            return (
                "Your building request could not be completed automatically "
                f"and has been sent to admins for follow-up.\nReason: {detail}"
            )
    return None


def _is_admin_interaction(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    allowed = set(interaction.client.cfg.discord.admin_role_ids)
    return any(role.id in allowed for role in member.roles)


class ApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:appr:approve:(?P<rid>[0-9]+)",
):
    def __init__(self, request_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Approve — retry",
            style=discord.ButtonStyle.success,
            custom_id=f"fra:appr:approve:{request_id}",
        ))
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("AutomationCog")
        if cog is not None:
            await cog.handle_approve(interaction, self.request_id)


class DenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:appr:deny:(?P<rid>[0-9]+)",
):
    def __init__(self, request_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"fra:appr:deny:{request_id}",
        ))
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin_interaction(interaction):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DenyModal(self.request_id))


class DenyModal(discord.ui.Modal, title="Deny request"):
    reason = discord.ui.TextInput(
        label="Reason (sent to the requester)",
        style=discord.TextStyle.paragraph,
        max_length=400,
        required=True,
    )

    def __init__(self, request_id: int) -> None:
        super().__init__()
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("AutomationCog")
        if cog is not None:
            await cog.handle_deny(interaction, self.request_id, str(self.reason))


class AutomationCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._requests = AutomationRepo(bot.db)
        self._lock = asyncio.Lock()
        self._warned_no_admin_log = False
        # Approve/deny buttons resolve their request id from the custom_id,
        # so they keep working across restarts.
        bot.add_dynamic_items(ApproveButton, DenyButton)
        self.publish_loop.start()

    def cog_unload(self) -> None:
        self.publish_loop.cancel()

    @tasks.loop(minutes=2)
    async def publish_loop(self) -> None:
        async with self._lock:
            try:
                await self._publish()
            except Exception:
                log.exception("Automation publisher failed")

    @publish_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _publish(self) -> None:
        # An unresolved admin_log channel must not black out the WHOLE
        # outcome layer: the admin embeds are skipped, but requester DMs
        # still go out and rows still drain (they used to queue forever,
        # so members never heard about their request either).
        channel = self.bot.channel_for("admin_log")
        if channel is None and not self._warned_no_admin_log:
            log.warning(
                "automation publisher: admin_log channel unresolved — "
                "outcomes reach requester DMs only"
            )
            self._warned_no_admin_log = True
        for row in await self._requests.pending_announcements():
            label = _KIND_LABEL.get(row["kind"], row["kind"])
            icon = _STATUS_ICON.get(row["status"], "•")
            description = row["status_detail"] or ""
            # "built prison #5561931" becomes a clickable building link.
            built = re.match(r"built (\w+) #(\d+)\b", description)
            if built:
                description = (
                    f"**[Built {built.group(1)} #{built.group(2)}]"
                    f"(https://www.missionchief.com/buildings/{built.group(2)})**"
                    + description[built.end():]
                )
            embed = discord.Embed(
                title=f"{label} request — {icon} {row['status']}"[:_TITLE_LIMIT],
                colour=_STATUS_COLOUR.get(row["status"], discord.Colour.blurple()),
                description=description[:_DESC_LIMIT],
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            if row["requester_name"]:
                embed.add_field(name="Requester", value=row["requester_name"][:_FIELD_LIMIT])
            if row["thread_id"]:
                embed.add_field(
                    name="Board post",
                    value=(
                        f"[#{row['post_id']}]"
                        f"(https://www.missionchief.com/alliance_threads/"
                        f"{row['thread_id']})"
                    )[:_FIELD_LIMIT],
                )
            else:
                embed.add_field(name="Source", value="Discord panel / slash command")
            details = self._payload_summary(row["payload"])
            if details:
                embed.add_field(name="Details", value=details[:_FIELD_LIMIT], inline=False)
            if self.bot.cfg.automation.dry_run:
                embed.set_footer(text="DRY-RUN — no MissionChief action was taken")
            if channel is not None:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException as exc:
                    status = getattr(exc, "status", None)
                    if status is not None and 400 <= status < 500:
                        log.error("Dropping unpostable automation embed (HTTP %s): %s", status, exc)
                        await self._requests.mark_posted(row["id"])
                        continue
                    log.warning("Transient failure posting automation embed (HTTP %s)", status)
                    return  # retry the rest next tick, preserving order
            await self._requests.mark_posted(row["id"])
            # The requester DM happens regardless of the admin channel AND
            # in dry-run (clearly marked): a member whose request was
            # simulated used to hear nothing at all and assume a real class.
            try:
                await self._notify_requester(row)
            except Exception:
                log.exception("requester DM for request %s failed", row["id"])
            if not self.bot.cfg.automation.dry_run:
                if row["status"] == "failed" and row["kind"] in ("training", "building"):
                    try:
                        await self._post_approval(row)
                    except Exception:
                        log.exception("approval embed for request %s failed", row["id"])
            await asyncio.sleep(1.0)

    # -- requester DM (Discord-panel requests only) ----------------------

    async def _notify_requester(self, row) -> None:
        data = self._payload_dict(row["payload"])
        user_id = data.get("discord_user_id")
        if not user_id:
            return  # board requests are notified in-game by the services
        text = _requester_dm_text(
            row["kind"], row["status"], row["status_detail"] or "", data
        )
        if not text:
            return
        if self.bot.cfg.automation.dry_run:
            text = (
                "🧪 **Dry-run simulation** — the bot is in test mode, no "
                "real action was taken in the game.\n" + text
            )
        user = self.bot.get_user(int(user_id))
        try:
            if user is None:
                user = await self.bot.fetch_user(int(user_id))
            await user.send(text[:1900])
            return
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        # Discord DMs closed: fall back to an in-game PM — never a mention
        # in the request channel (public outcome pings are just noise).
        await self._notify_ingame_fallback(row, text)

    async def _notify_ingame_fallback(self, row, text: str) -> None:
        """Deliver an undeliverable requester DM as an in-game PM instead,
        mirrored into the DM forum like every outgoing message. The MC name
        resolves from the request's verified identity (``requester_mc_id``
        → roster); older rows fall back to the stored requester name."""
        from ..db.repos import MembersRepo

        name = None
        if row["requester_mc_id"]:
            roster = await MembersRepo(self.bot.db).active_members()
            member = roster.get(int(row["requester_mc_id"]))
            if member is not None:
                name = member["name"]
        name = name or row["requester_name"]
        if not name:
            log.warning(
                "request %s: DMs closed and no MC identity — outcome only in "
                "the admin log", row["id"],
            )
            return
        plain = text.replace("**", "").replace("`", "")
        try:
            result = await self.bot.dm_mirror.send_new(
                name, f"{str(row['kind']).title()} request", plain[:2000]
            )
        except Exception:
            log.exception("request %s: in-game PM fallback errored", row["id"])
            return
        if not result.get("ok"):
            log.warning(
                "request %s: in-game PM fallback to %s failed: %s",
                row["id"], name, result.get("detail"),
            )

    # -- admin approve/deny ----------------------------------------------

    async def _post_approval(self, row) -> None:
        channel = (
            self.bot.channel_for("admin_approvals")
            or self.bot.channel_for("admin_log")
        )
        if channel is None:
            return
        label = _KIND_LABEL.get(row["kind"], row["kind"])
        embed = discord.Embed(
            title=f"{label} request needs a decision"[:_TITLE_LIMIT],
            colour=discord.Colour.yellow(),
            description=(row["status_detail"] or "")[:_DESC_LIMIT],
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        if row["requester_name"]:
            embed.add_field(name="Requester", value=row["requester_name"][:_FIELD_LIMIT])
        if row["thread_id"]:
            embed.add_field(
                name="Board post",
                value=(
                    f"[#{row['post_id']}](https://www.missionchief.com/"
                    f"alliance_threads/{row['thread_id']})"
                )[:_FIELD_LIMIT],
            )
        details = self._payload_summary(row["payload"])
        if details:
            embed.add_field(name="Details", value=details[:_FIELD_LIMIT], inline=False)
        embed.set_footer(text=f"Request ID: {row['id']} — use the buttons to decide.")
        view = discord.ui.View(timeout=None)
        view.add_item(ApproveButton(row["id"]))
        view.add_item(DenyButton(row["id"]))
        await channel.send(embed=embed, view=view)

    async def handle_approve(
        self, interaction: discord.Interaction, request_id: int
    ) -> None:
        if not _is_admin_interaction(interaction):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        row = await self._requests.get(request_id)
        if row is None:
            await interaction.response.send_message(
                "This request no longer exists.", ephemeral=True
            )
            return
        # A fresh attempt is intentional here — clear the verify-only flag
        # so the service really retries the action.
        data = self._payload_dict(row["payload"])
        data.pop("pending_confirm", None)
        ok = await self._requests.requeue(request_id, payload=json.dumps(data))
        if not ok:
            await interaction.response.send_message(
                f"Request {request_id} is {row['status']} — nothing to re-queue.",
                ephemeral=True,
            )
            return
        await self._decide_message(
            interaction, discord.Colour.green(),
            f"Approved by {interaction.user} — re-queued for another attempt.",
        )
        await interaction.response.send_message(
            f"✅ Request {request_id} re-queued.", ephemeral=True
        )

    async def handle_deny(
        self, interaction: discord.Interaction, request_id: int, reason: str
    ) -> None:
        row = await self._requests.get(request_id)
        if row is None:
            await interaction.response.send_message(
                "This request no longer exists.", ephemeral=True
            )
            return
        if row["status"] not in ("failed", "skipped"):
            await interaction.response.send_message(
                f"Request {request_id} is already {row['status']} — "
                "someone may have re-queued it.", ephemeral=True,
            )
            return
        await self._requests.set_status(
            request_id, "failed", f"denied by admin: {reason}", announce=False,
        )
        await self._decide_message(
            interaction, discord.Colour.red(),
            f"Denied by {interaction.user} — {reason}",
        )
        # Board reply + Discord DM, the reference bot's denial texts.
        service = {
            "training": getattr(self.bot, "trainings", None),
            "building": getattr(self.bot, "buildings", None),
        }.get(row["kind"])
        requester = row["requester_name"] or "member"
        if service is not None and row["thread_id"]:
            try:
                if row["kind"] == "building":
                    await service.reply(
                        f"Building request rejected for {requester}.\n\n"
                        f"Request ID: {request_id}\nReason: {reason}\n\n"
                        "Please correct this before submitting another "
                        "building request."
                    )
                else:
                    await service.reply(
                        f"Training request could not be processed for "
                        f"{requester}.\n\nReason: denied by admin — {reason}"
                    )
            except Exception:
                log.exception("deny board reply for request %s failed", request_id)
        data = self._payload_dict(row["payload"])
        if data.get("discord_user_id"):
            user = self.bot.get_user(int(data["discord_user_id"]))
            if user is not None:
                try:
                    await user.send(
                        f"❌ Your {row['kind']} request has been **DENIED**.\n\n"
                        f"**Reason**: {reason}"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.response.send_message(
            f"❌ Request {request_id} denied.", ephemeral=True
        )

    @staticmethod
    async def _decide_message(
        interaction: discord.Interaction, colour: discord.Colour, footer: str
    ) -> None:
        """Recolour the decision embed and drop the buttons."""
        message = interaction.message
        if message is None or not message.embeds:
            return
        embed = message.embeds[0]
        embed.colour = colour
        embed.set_footer(text=footer[:2048])
        try:
            await message.edit(embed=embed, view=None)
        except discord.HTTPException:
            log.warning("could not edit decision message %s", message.id)

    @staticmethod
    def _payload_dict(payload: str | None) -> dict:
        try:
            return json.loads(payload or "{}")
        except ValueError:
            return {}

    @staticmethod
    def _payload_summary(payload: str | None) -> str | None:
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        parts = []
        if data.get("trainings"):
            names = [
                t.get("name", "?") if isinstance(t, dict) else str(t)
                for t in data["trainings"]
            ]
            parts.append("Trainings: " + ", ".join(names))
        if data.get("building_type"):
            parts.append(f"Type: {data['building_type']}")
        if data.get("address"):
            parts.append(f"Location: {data['address']}")
        if data.get("location") and "address" not in data:
            parts.append(f"Location: {data['location']}")
        if data.get("building_id"):
            parts.append(
                f"Building: [#{data['building_id']}]"
                f"(https://www.missionchief.com/buildings/{data['building_id']})"
            )
        return "\n".join(parts) if parts else None
