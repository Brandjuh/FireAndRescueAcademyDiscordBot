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

import discord
from discord.ext import commands, tasks

from ..db.repos import ApplicationsRepo, LogsRepo, MembersRepo
from ..mc.parsers.logs import ACTION_PATTERNS
from .display import (
    ACTION_DISPLAY,
    FALLBACK_DISPLAY,
    MEMBER_EVENT_DISPLAY,
    affected_url,
    profile_url,
)

log = logging.getLogger(__name__)

_POST_PAUSE_SECONDS = 1.2
_BATCH_LIMIT = 20


def _event_unix(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso_ts).timestamp())
    except ValueError:
        return None


class NotificationsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._members = MembersRepo(bot.db)
        self._apps = ApplicationsRepo(bot.db)
        self._logs = LogsRepo(bot.db)
        self._lock = asyncio.Lock()

        missing = {key for _, key in ACTION_PATTERNS} - set(ACTION_DISPLAY)
        if missing:
            log.warning(
                "Log actions without display mapping (will use fallback): %s",
                ", ".join(sorted(missing)),
            )

        self.publish_loop.start()

    def cog_unload(self) -> None:
        self.publish_loop.cancel()

    @tasks.loop(minutes=2)
    async def publish_loop(self) -> None:
        async with self._lock:
            try:
                await self._publish_applications()
                await self._publish_member_events()
                await self._publish_alliance_logs()
            except Exception:
                log.exception("Publisher iteration failed")

    @publish_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------

    async def _publish_applications(self) -> None:
        channel = self.bot.channel_for("applications")
        if channel is None:
            return
        for row in await self._apps.pending_announcements():
            embed = discord.Embed(
                title="📥 New alliance application",
                colour=discord.Colour.green(),
                description=(
                    f"**{row['applicant_name']}** wants to join the alliance.\n"
                    "Review it on the [applications page]"
                    "(https://www.missionchief.com/verband/bewerbungen)."
                ),
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            url = profile_url(row["mc_user_id"])
            if url:
                embed.add_field(name="Profile", value=url, inline=False)
            await channel.send(embed=embed)
            await self._apps.mark_posted(row["application_id"])
            await asyncio.sleep(_POST_PAUSE_SECONDS)

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
                title=f"{emoji} {title}",
                colour=colour,
                description="\n".join(lines),
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            await channel.send(embed=embed)
            await self._members.mark_event_posted(row["id"])
            await asyncio.sleep(_POST_PAUSE_SECONDS)

    async def _publish_alliance_logs(self) -> None:
        channel = self.bot.channel_for("alliance_logs")
        if channel is None:
            return
        for row in await self._logs.pending_posts(limit=_BATCH_LIMIT):
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
                lines.append(row["description"])
            if row["contribution_amount"]:
                lines.append(f"**Contribution:** {row['contribution_amount']:+,} credits")
            unix = _event_unix(row["event_at"])
            if unix is not None:
                lines.append(f"<t:{unix}:f> (<t:{unix}:R>)")
            else:
                lines.append(f"`{row['raw_timestamp']}`")
            embed = discord.Embed(
                title=f"{emoji} {title}",
                colour=colour,
                description="\n".join(lines) or "—",
            )
            await channel.send(embed=embed)
            await self._logs.mark_posted(row["id"])
            await asyncio.sleep(_POST_PAUSE_SECONDS)
