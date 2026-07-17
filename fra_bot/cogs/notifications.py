"""Publisher cog: announces pending database rows to Discord.

Everything Discord-facing is driven by ``posted_at IS NULL`` rows, so a
crash between "post" and "mark" can at worst repeat ONE message — it can
never silently skip entries (the failure mode of the old watermark
design). Posting is paced to stay far away from Discord rate limits.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

import discord
from discord.ext import commands, tasks

from ..core import log_routes
from ..db.repos import (
    ApplicationsRepo,
    LogsRepo,
    MemberActionsRepo,
    MembersRepo,
    StateRepo,
)
from ..mc.errors import MissionChiefError
from ..mc.parsers.logs import ACTION_PATTERNS
from .display import (
    ACTION_DISPLAY,
    FALLBACK_DISPLAY,
    MEMBER_EVENT_DISPLAY,
    affected_url,
    format_log_description,
    profile_url,
)

log = logging.getLogger(__name__)

_POST_PAUSE_SECONDS = 1.2
_BATCH_LIMIT = 20
# The route (mirror) pass fans each row out to N channels; cap the TOTAL
# sends per tick (not rows) so a wide "all" route can't monopolise the
# 2-minute loop and starve the main feed. Remaining rows drain next tick.
_ROUTE_SEND_BUDGET = 40

# Discord embed limits (a value over the limit is a permanent 400).
_TITLE_LIMIT = 256
_DESC_LIMIT = 4096
_FIELD_LIMIT = 1024


def _event_unix(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso_ts).timestamp())
    except ValueError:
        return None


class ApplicationAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:app:accept:(?P<aid>[0-9]+)",
):
    """Persistent Accept button on an application announcement (the manual
    backup for auto-accept; the only path when auto-accept is off)."""

    def __init__(self, application_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"fra:app:accept:{application_id}",
        ))
        self.application_id = application_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("NotificationsCog")
        if cog is not None:
            await cog.handle_application_action(
                interaction, self.application_id, "accept"
            )


class ApplicationDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fra:app:deny:(?P<aid>[0-9]+)",
):
    def __init__(self, application_id: int) -> None:
        super().__init__(discord.ui.Button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"fra:app:deny:{application_id}",
        ))
        self.application_id = application_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("NotificationsCog")
        if cog is not None:
            await cog.handle_application_action(
                interaction, self.application_id, "deny"
            )


def _application_view(application_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApplicationAcceptButton(application_id))
    view.add_item(ApplicationDenyButton(application_id))
    return view


class NotificationsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._members = MembersRepo(bot.db)
        self._apps = ApplicationsRepo(bot.db)
        self._logs = LogsRepo(bot.db)
        self._state = StateRepo(bot.db)
        self._lock = asyncio.Lock()

        missing = {key for _, key in ACTION_PATTERNS} - set(ACTION_DISPLAY)
        if missing:
            log.warning(
                "Log actions without display mapping (will use fallback): %s",
                ", ".join(sorted(missing)),
            )
        drift = log_routes.group_drift()
        if drift:
            log.warning(
                "Log-route groups reference unknown action keys: %s",
                "; ".join(f"{g}: {sorted(k)}" for g, k in drift.items()),
            )

        # Accept/Deny buttons resolve their application id from the
        # custom_id, so they keep working across restarts.
        bot.add_dynamic_items(ApplicationAcceptButton, ApplicationDenyButton)
        self.publish_loop.start()

    def cog_unload(self) -> None:
        self.publish_loop.cancel()

    @tasks.loop(minutes=2)
    async def publish_loop(self) -> None:
        async with self._lock:
            # Each publisher is isolated: a failure in one channel must
            # not suppress the others.
            for publisher in (
                self._publish_applications,
                self._publish_member_events,
                self._publish_alliance_logs,
                self._publish_log_routes,
                self._publish_member_actions,
            ):
                try:
                    await publisher()
                except Exception:
                    log.exception("Publisher %s failed", publisher.__name__)

    @publish_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------

    async def _send_or_skip(
        self, channel, embed, mark_posted, *, label: str, view=None
    ) -> str:
        """Send one embed and mark the row posted.

        Returns 'ok', 'skip' (permanent 4xx — dropped so it can't block
        the queue forever) or 'retry' (transient — leave unmarked, stop
        the batch and try again next tick, preserving order).
        """
        try:
            if view is not None:
                await channel.send(embed=embed, view=view)
            else:
                await channel.send(embed=embed)
        except discord.HTTPException as exc:
            status = getattr(exc, "status", None)
            if status is not None and 400 <= status < 500:
                log.error("Dropping unpostable %s (HTTP %s): %s", label, status, exc)
                await mark_posted()
                return "skip"
            log.warning(
                "Transient failure posting %s (HTTP %s); retrying next tick",
                label, status,
            )
            return "retry"
        await mark_posted()
        return "ok"

    async def _publish_applications(self) -> None:
        """Announce new applications — auto-accepting them first when the
        switch is on (reference bot: newmembernotify). Auto-accept failures
        fall back to the manual Accept/Deny buttons, so an application can
        never go unhandled silently."""
        channel = self.bot.channel_for("applications")
        if channel is None:
            return
        auto = (
            self.bot.cfg.automation.applications.auto_accept
            and not self.bot.cfg.automation.dry_run
        )
        for row in await self._apps.pending_announcements():
            name = row["applicant_name"]
            view = None
            if row["resolved_at"]:
                # Vanished from the page (or accepted by us on a previous
                # tick whose Discord send failed) before we could announce
                # it — never re-fire the game action, just log it.
                embed = discord.Embed(
                    title="📥 Alliance application already handled",
                    colour=discord.Colour.light_grey(),
                    description=(
                        f"**{name}** applied; the application was already "
                        "handled (auto-accepted on an earlier pass, or "
                        "decided in-game) before this announcement."
                    )[:_DESC_LIMIT],
                    timestamp=dt.datetime.now(dt.timezone.utc),
                )
            elif auto:
                try:
                    await self.bot.applications_sync.accept(row["application_id"])
                    embed = discord.Embed(
                        title="✅ Alliance application auto-accepted",
                        colour=discord.Colour.green(),
                        description=(
                            f"**{name}** applied and was accepted "
                            "automatically. Welcome them in!"
                        )[:_DESC_LIMIT],
                        timestamp=dt.datetime.now(dt.timezone.utc),
                    )
                except MissionChiefError as exc:
                    embed = discord.Embed(
                        title="⚠️ Auto-accept failed — decide manually",
                        colour=discord.Colour.orange(),
                        description=(
                            f"**{name}** wants to join, but accepting "
                            f"automatically failed: {exc}"
                        )[:_DESC_LIMIT],
                        timestamp=dt.datetime.now(dt.timezone.utc),
                    )
                    view = _application_view(row["application_id"])
            else:
                note = (
                    "\n*(dry-run is on — the buttons will only report what "
                    "they would do)*"
                    if self.bot.cfg.automation.dry_run
                    and self.bot.cfg.automation.applications.auto_accept
                    else ""
                )
                embed = discord.Embed(
                    title="📥 New alliance application",
                    colour=discord.Colour.blue(),
                    description=(
                        f"**{name}** wants to join the alliance.{note}"
                    )[:_DESC_LIMIT],
                    timestamp=dt.datetime.now(dt.timezone.utc),
                )
                view = _application_view(row["application_id"])
            url = profile_url(row["mc_user_id"])
            if url:
                embed.add_field(name="Profile", value=url[:_FIELD_LIMIT], inline=False)
            embed.set_footer(text=f"Application ID: {row['application_id']}")
            outcome = await self._send_or_skip(
                channel, embed,
                lambda r=row: self._apps.mark_posted(r["application_id"]),
                label="application",
                view=view,
            )
            if outcome == "retry":
                return
            await asyncio.sleep(_POST_PAUSE_SECONDS)

    async def _publish_member_actions(self) -> None:
        """The admin feed: every member's bot-side action, in order, to
        the member_actions channel (0 = feed off; rows still record for
        the per-member history)."""
        channel_id = int(
            getattr(self.bot.cfg.discord.channels, "member_actions", 0) or 0
        )
        repo = MemberActionsRepo(self.bot.db)
        if not channel_id:
            # Feed OFF by config: suppress instead of queueing, or enabling
            # the feed later would flood it with all history. The
            # per-member view reads the rows regardless.
            for row in await repo.pending_feed(limit=100):
                await repo.mark_posted(row["id"])
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            # Configured but momentarily unreachable (gateway re-identify,
            # cache warm-up): KEEP the rows queued — suppressing here would
            # silently discard feed entries.
            return
        for row in await repo.pending_feed():
            who = row["actor_name"] or (
                f"<@{row['discord_user_id']}>" if row["discord_user_id"] else "?"
            )
            embed = discord.Embed(
                colour=discord.Colour.dark_teal(),
                description=(
                    f"**{who}** — {row['action'].replace('_', ' ')}"
                    + (f"\n{row['detail']}" if row["detail"] else "")
                )[:_DESC_LIMIT],
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            embed.set_footer(text=f"Action #{row['id']}")
            outcome = await self._send_or_skip(
                channel, embed,
                lambda r=row: repo.mark_posted(r["id"]),
                label="member action",
            )
            if outcome == "retry":
                return
            await asyncio.sleep(_POST_PAUSE_SECONDS)

    async def handle_application_action(
        self, interaction: discord.Interaction, application_id: int, action: str
    ) -> None:
        """Accept/Deny button click on an application announcement."""
        from .automation import _is_admin_interaction

        if not _is_admin_interaction(interaction):
            await interaction.response.send_message(
                "You don't have permission to do this.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        row = await self._apps.get(application_id)
        name = row["applicant_name"] if row else f"application #{application_id}"
        if self.bot.cfg.automation.dry_run:
            await interaction.followup.send(
                f"[dry-run] would **{action}** {name} — no game action taken.",
                ephemeral=True,
            )
            return
        try:
            if action == "accept":
                await self.bot.applications_sync.accept(application_id)
            else:
                await self.bot.applications_sync.deny(application_id)
        except MissionChiefError as exc:
            await interaction.followup.send(
                f"❌ Could not {action} **{name}**: {exc}", ephemeral=True
            )
            return
        accepted = action == "accept"
        verb = "Accepted" if accepted else "Denied"
        try:
            message = interaction.message
            embed = message.embeds[0] if message and message.embeds else None
            if message is not None and embed is not None:
                embed.colour = (
                    discord.Colour.green() if accepted else discord.Colour.red()
                )
                embed.set_footer(
                    text=f"{verb} by {interaction.user.display_name} — "
                    f"application ID: {application_id}"
                )
                await message.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("Could not update application embed %s: %s",
                        application_id, exc)
        await interaction.followup.send(f"✅ {verb} **{name}**.", ephemeral=True)
        icon = "✅" if accepted else "❌"
        await self.bot.notify_admin(
            f"{icon} Alliance application of **{name}** {verb.lower()} by "
            f"{interaction.user.display_name}."
        )

    async def _publish_member_events(self) -> None:
        channel = self.bot.channel_for("member_events")
        if channel is None:
            return
        for row in await self._members.pending_events(limit=_BATCH_LIMIT):
            title, colour, emoji = MEMBER_EVENT_DISPLAY.get(
                row["event_type"], ("Member update", discord.Colour.light_grey(), "ℹ️")
            )
            lines = [f"**{row['name']}**"]
            if row["event_type"] == "joined" and row["new_value"]:
                lines.append(f"Role: {row['new_value']}")
            elif row["old_value"] or row["new_value"]:
                lines.append(f"{row['old_value'] or '—'} → {row['new_value'] or '—'}")
            url = profile_url(row["mc_user_id"])
            if url:
                lines.append(f"[MissionChief profile]({url})")
            embed = discord.Embed(
                title=f"{emoji} {title}"[:_TITLE_LIMIT],
                colour=colour,
                description="\n".join(lines)[:_DESC_LIMIT],
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            outcome = await self._send_or_skip(
                channel, embed,
                lambda r=row: self._members.mark_event_posted(r["id"]),
                label="member event",
            )
            if outcome == "retry":
                return
            await asyncio.sleep(_POST_PAUSE_SECONDS)

    @staticmethod
    def _alliance_log_embed(row) -> discord.Embed:
        """The one embed builder for an alliance-log row, shared by the main
        feed and the route (mirror) pass so a routed copy is byte-identical
        to the canonical post."""
        title, colour, emoji = ACTION_DISPLAY.get(row["action_key"], FALLBACK_DISPLAY)
        lines = []
        if row["executed_name"]:
            executed = row["executed_name"]
            url = profile_url(row["executed_mc_id"])
            lines.append(f"**By:** [{executed}]({url})" if url else f"**By:** {executed}")
        if row["affected_name"] and row["affected_name"] != row["executed_name"]:
            affected = row["affected_name"]
            url = affected_url(row["affected_type"], row["affected_mc_id"])
            lines.append(
                f"**Affected:** [{affected}]({url})" if url else f"**Affected:** {affected}"
            )
        if row["description"]:
            desc = format_log_description(row["action_key"], row["description"])
            if desc:  # expansion logs can reduce to nothing (title says it all)
                lines.append(desc)
        if row["contribution_amount"]:
            lines.append(f"**Contribution:** {row['contribution_amount']:+,} credits")
        unix = _event_unix(row["event_at"])
        if unix is not None:
            lines.append(f"<t:{unix}:f> (<t:{unix}:R>)")
        else:
            lines.append(f"`{row['raw_timestamp']}`")
        return discord.Embed(
            title=f"{emoji} {title}"[:_TITLE_LIMIT],
            colour=colour,
            description=("\n".join(lines) or "—")[:_DESC_LIMIT],
        )

    async def _publish_alliance_logs(self) -> None:
        channel = self.bot.channel_for("alliance_logs")
        if channel is None:
            return
        for row in await self._logs.pending_posts(limit=_BATCH_LIMIT):
            embed = self._alliance_log_embed(row)
            outcome = await self._send_or_skip(
                channel, embed,
                lambda r=row: self._logs.mark_posted(r["id"]),
                label="alliance log",
            )
            if outcome == "retry":
                return
            await asyncio.sleep(_POST_PAUSE_SECONDS)

    async def _publish_log_routes(self) -> None:
        """Mirror each already-posted log to the channels that subscribed to
        its type (``!fra logroutes``). Best-effort by design: the main
        ``alliance_logs`` channel is the source of truth, so a routed copy
        that hits a transient error is dropped (logged) rather than retried —
        retrying a multi-channel fan-out would re-deliver to the channels
        that already succeeded. Each row is marked routed unconditionally
        once every target channel has been attempted, so a deleted or broken
        route channel can never wedge the queue.

        Rows are ALWAYS drained (marked routed), even when they match no route
        or no routes exist at all: 'routed' means the routing decision has
        been made, not that a copy was sent. Without this a long stretch with
        no routes would pile up posted-but-unrouted rows, and the first route
        an admin adds would replay that whole backlog into the new channel —
        the very flood the feature exists to prevent."""
        routes = await log_routes.load(self._state)
        main = self.bot.channel_for("alliance_logs")
        main_id = main.id if main is not None else None

        sends = 0
        for row in await self._logs.pending_routes(limit=_BATCH_LIMIT):
            targets = (
                log_routes.channels_for(routes, row["action_key"], exclude=main_id)
                if routes else []
            )
            if targets:
                embed = self._alliance_log_embed(row)
                for channel_id in targets:
                    channel = self.bot.get_channel(channel_id)
                    if channel is None:
                        # Deleted, uncached or cross-guild — drop this copy,
                        # never block the row on it.
                        log.warning(
                            "log route channel %s is unreachable; skipping",
                            channel_id,
                        )
                        continue
                    try:
                        await channel.send(embed=embed)
                    except discord.HTTPException as exc:
                        log.warning(
                            "Dropping routed log to channel %s (HTTP %s)",
                            channel_id, getattr(exc, "status", None),
                        )
                    sends += 1
                    await asyncio.sleep(_POST_PAUSE_SECONDS)
            await self._logs.mark_routed(row["id"])
            if sends >= _ROUTE_SEND_BUDGET:
                return  # rest drains next tick — don't monopolise the loop
