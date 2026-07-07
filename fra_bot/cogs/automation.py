"""Announces board-automation request outcomes to Discord.

Like the other publishers, this is driven by ``posted_at IS NULL`` rows
in ``automation_requests`` — a crash can at worst repeat one embed, and
enabling/disabling automation never floods old history.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

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


class AutomationCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._requests = AutomationRepo(bot.db)
        self._lock = asyncio.Lock()
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
        channel = self.bot.channel_for("admin_log")
        if channel is None:
            return
        for row in await self._requests.pending_announcements():
            label = _KIND_LABEL.get(row["kind"], row["kind"])
            icon = _STATUS_ICON.get(row["status"], "•")
            embed = discord.Embed(
                title=f"{label} request — {icon} {row['status']}"[:_TITLE_LIMIT],
                colour=_STATUS_COLOUR.get(row["status"], discord.Colour.blurple()),
                description=(row["status_detail"] or "")[:_DESC_LIMIT],
                timestamp=dt.datetime.now(dt.timezone.utc),
            )
            if row["requester_name"]:
                embed.add_field(name="Requester", value=row["requester_name"][:_FIELD_LIMIT])
            embed.add_field(
                name="Board post",
                value=(
                    f"[#{row['post_id']}]"
                    f"(https://www.missionchief.com/alliance_threads/"
                    f"{row['thread_id']})"
                )[:_FIELD_LIMIT],
            )
            details = self._payload_summary(row["payload"])
            if details:
                embed.add_field(name="Details", value=details[:_FIELD_LIMIT], inline=False)
            if self.bot.cfg.automation.dry_run:
                embed.set_footer(text="DRY-RUN — no MissionChief action was taken")
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
            await asyncio.sleep(1.0)

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
            parts.append("Trainings: " + ", ".join(data["trainings"]))
        if data.get("building_type"):
            parts.append(f"Type: {data['building_type']}")
        if data.get("address"):
            parts.append(f"Location: {data['address']}")
        if data.get("location") and "address" not in data:
            parts.append(f"Location: {data['location']}")
        if data.get("building_id"):
            parts.append(f"Building: {data['building_id']}")
        return "\n".join(parts) if parts else None
