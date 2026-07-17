"""Game-sync intake + hotspots.

Members run ``tools/fra-profile-sync.user.js`` (Greasemonkey/
Tampermonkey); it posts their own buildings/vehicles as a JSON file to
a Discord webhook in the PRIVATE intake channel
(``discord.channels.game_sync``). This cog validates each payload,
stores it, links it to the member via the verified link, and reacts
✅/⚠️ on the webhook message so the channel doubles as a sync log.

``!fra`` stays admin-only elsewhere; here: ``!hotspots`` (admins) shows
where the alliance's buildings cluster, from all synced coordinates.
"""

from __future__ import annotations

import json
import logging

import discord
from discord.ext import commands

from ..db.repos import GameSyncRepo, LinksRepo
from ..services.game_sync import (
    SyncPayloadError,
    cluster_hotspots,
    parse_sync_payload,
    render_hotspots,
    summarize_buildings,
)
from .admin import is_fra_admin

log = logging.getLogger(__name__)

#: Don't read absurdly large attachments (a real payload is a few KB).
MAX_ATTACHMENT_BYTES = 512 * 1024


class GameSyncCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.repo = GameSyncRepo(bot.db)

    def _intake_channel_id(self) -> int:
        return int(
            getattr(self.bot.cfg.discord.channels, "game_sync", 0) or 0
        )

    # -- webhook intake ------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        channel_id = self._intake_channel_id()
        if not channel_id or message.channel.id != channel_id:
            return
        # Only webhook posts count — humans chatting in the channel (or the
        # bot's own reactions/replies) are not payloads.
        if message.webhook_id is None:
            return
        raw = await self._payload_text(message)
        if raw is None:
            return
        try:
            payload = parse_sync_payload(raw)
        except SyncPayloadError as exc:
            log.warning("game sync: invalid payload rejected: %s", exc)
            await self._react(message, "⚠️")
            return
        link = await LinksRepo(self.bot.db).get_by_mc(payload.mc_user_id)
        discord_id = (
            int(link["discord_id"])
            if link is not None and link["status"] == "approved" else None
        )
        await self.repo.upsert(
            mc_user_id=payload.mc_user_id,
            discord_user_id=discord_id,
            mc_name=payload.mc_name,
            building_count=payload.building_count,
            vehicle_count=payload.vehicle_count,
            buildings_json=payload.buildings_json,
            vehicles_json=payload.vehicles_json,
        )
        await self.bot.log_member_action(
            action="game_synced",
            detail=(
                f"{payload.building_count} gebouwen, "
                f"{payload.vehicle_count} voertuigen"
            ),
            discord_user_id=discord_id,
            mc_user_id=payload.mc_user_id,
            actor_name=payload.mc_name,
        )
        await self._react(message, "✅")
        log.info(
            "game sync: %s (MC %s) — %d buildings, %d vehicles%s",
            payload.mc_name or "?", payload.mc_user_id,
            payload.building_count, payload.vehicle_count,
            "" if discord_id else " (no verified link)",
        )

    @staticmethod
    async def _payload_text(message: discord.Message) -> str | None:
        """The JSON body: a .json attachment (preferred; content caps at
        2000 chars) or the message content itself."""
        for attachment in message.attachments:
            if attachment.size > MAX_ATTACHMENT_BYTES:
                continue
            if attachment.filename.endswith(".json"):
                try:
                    return (await attachment.read()).decode("utf-8", "replace")
                except discord.HTTPException as exc:
                    log.warning("game sync: attachment read failed: %s", exc)
                    return None
        content = (message.content or "").strip()
        return content or None

    @staticmethod
    async def _react(message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    # -- hotspots (admins) -----------------------------------------------------

    @commands.command(name="hotspots")
    @is_fra_admin()
    async def hotspots(self, ctx: commands.Context, grid_km: int = 11) -> None:
        """Where the alliance's buildings cluster: `!hotspots [cel-km]`."""
        grid = max(1, min(int(grid_km), 200)) / 111.0  # ~degrees per km
        member_coords: dict[int, list[tuple[float, float]]] = {}
        building_total = 0
        for row in await self.repo.all_synced():
            try:
                data = json.loads(row["buildings_json"] or "{}")
            except ValueError:
                continue
            coords = [
                (float(pair[0]), float(pair[1]))
                for pair in data.get("coords") or []
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
            member_coords[int(row["mc_user_id"])] = coords
            building_total += int(row["building_count"] or 0)
        spots = cluster_hotspots(member_coords, grid=grid)
        await ctx.send(render_hotspots(
            spots, member_total=len(member_coords),
            building_total=building_total,
        ))

    # -- profile section (used by the profile embed) ---------------------------

    async def profile_line(self, discord_user_id: int) -> str | None:
        row = await self.repo.get_by_discord(discord_user_id)
        if row is None:
            return None
        try:
            by_type = json.loads(row["buildings_json"] or "{}").get("by_type") or {}
        except ValueError:
            by_type = {}
        summary = summarize_buildings(by_type)
        line = (
            f"{row['building_count']} gebouwen · "
            f"{row['vehicle_count']} voertuigen"
        )
        if summary:
            line += f"\n{summary}"
        return line + f"\n*gesynchroniseerd {str(row['synced_at'])[:16]}*"


async def setup(bot) -> None:
    await bot.add_cog(GameSyncCog(bot))
