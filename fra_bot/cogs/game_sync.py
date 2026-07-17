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

import io
import json
import logging
from dataclasses import replace

import discord
from discord.ext import commands

from ..db.repos import GameSyncRepo, LinksRepo
from ..services.game_sync import (
    SyncPayloadError,
    cluster_hotspots,
    merge_by_place,
    parse_sync_payload,
    place_name,
    render_hotspots,
    summarize_buildings,
)
from .admin import is_fra_admin

log = logging.getLogger(__name__)

#: Don't read absurdly large attachments (a real payload is a few KB).
MAX_ATTACHMENT_BYTES = 512 * 1024

#: State cache for the LSSM vehicle id → name map (!fleet, !infographic).
VEHICLE_NAMES_KEY = "game_sync:vehicle_names"
VEHICLE_NAMES_MAX_AGE_DAYS = 7


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
                f"{payload.building_count} buildings, "
                f"{payload.vehicle_count} vehicles"
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

    async def _sync_stats(self):
        """Aggregate every synced row: coordinates per member, totals and
        the per-type building/vehicle counts (for the bar charts)."""
        member_coords: dict[int, list[tuple[float, float]]] = {}
        building_dicts: list[dict] = []
        vehicle_dicts: list[dict] = []
        building_total = vehicle_total = 0
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
            if isinstance(data.get("by_type"), dict):
                building_dicts.append(data["by_type"])
            try:
                vehicles = json.loads(row["vehicles_json"] or "{}")
            except ValueError:
                vehicles = {}
            if isinstance(vehicles.get("by_type"), dict):
                vehicle_dicts.append(vehicles["by_type"])
            building_total += int(row["building_count"] or 0)
            vehicle_total += int(row["vehicle_count"] or 0)
        return (member_coords, building_dicts, vehicle_dicts,
                building_total, vehicle_total)

    async def _vehicle_names(self) -> dict[int, str]:
        """The LSSM vehicle id → name map, state-cached for a week. Any
        fetch problem falls back to the cache (however old), then to {} —
        unknown ids render as "type N", so this can never break a command."""
        import datetime as dt

        from ..db.repos import StateRepo

        state = StateRepo(self.bot.db)
        cached: dict[int, str] = {}
        fetched_at = None
        raw = await state.get(VEHICLE_NAMES_KEY)
        if raw:
            try:
                data = json.loads(raw)
                cached = {
                    int(k): str(v) for k, v in (data.get("names") or {}).items()
                }
                fetched_at = dt.datetime.fromisoformat(data["fetched_at"])
            except (ValueError, KeyError, TypeError):
                cached, fetched_at = {}, None
        now = dt.datetime.now(dt.timezone.utc)
        try:
            fresh = (
                cached and fetched_at is not None
                and (now - fetched_at).days < VEHICLE_NAMES_MAX_AGE_DAYS
            )
        except TypeError:  # naive timestamp from an old write
            fresh = False
        if fresh:
            return cached
        try:
            import aiohttp

            from ..mc.vehicles_catalog import fetch_catalog

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                catalog = await fetch_catalog(session)
            names = {int(v["id"]): str(v["name"]) for v in catalog}
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "game sync: vehicle catalog unavailable (%s); using %d cached",
                exc, len(cached),
            )
            return cached
        await state.set(VEHICLE_NAMES_KEY, json.dumps({
            "fetched_at": now.isoformat(),
            "names": {str(k): v for k, v in names.items()},
        }))
        return names

    @commands.command(name="hotspots")
    @is_fra_admin()
    async def hotspots(self, ctx: commands.Context, grid_km: int = 11) -> None:
        """Where the alliance's buildings cluster: `!hotspots [cell-km]`."""
        grid = max(1, min(int(grid_km), 200)) / 111.0  # ~degrees per km
        member_coords, _, _, building_total, _ = await self._sync_stats()
        # Naming (≤24 Nominatim lookups at 1 req/s, cached forever per
        # cell) and tile fetching can take ~30 s on a cold cache. Cluster
        # twice as wide as the list, then merge same-place cells so one
        # metro area doesn't fill the whole top-12.
        async with ctx.typing():
            spots = merge_by_place(await self._named(
                cluster_hotspots(member_coords, grid=grid, top=24)
            ))
            text = render_hotspots(
                spots, member_total=len(member_coords),
                building_total=building_total,
            )
            image = await self._map_image(spots)
        if image is not None:
            await ctx.send(
                text, file=discord.File(io.BytesIO(image), "hotspots.png")
            )
        else:
            await ctx.send(text)

    @commands.command(name="infographic")
    @is_fra_admin()
    async def infographic(self, ctx: commands.Context, grid_km: int = 11) -> None:
        """The alliance snapshot card: `!infographic [cell-km]`."""
        import datetime as dt

        from ..services.game_sync import top_building_types, top_vehicle_types
        from ..services.infographic import AllianceSnapshot, render_infographic

        grid = max(1, min(int(grid_km), 200)) / 111.0
        (member_coords, building_dicts, vehicle_dicts,
         building_total, vehicle_total) = await self._sync_stats()
        if not member_coords:
            await ctx.send(render_hotspots([], member_total=0, building_total=0))
            return
        async with ctx.typing():
            spots = merge_by_place(await self._named(
                cluster_hotspots(member_coords, grid=grid, top=24)
            ))
            snapshot = AllianceSnapshot(
                title="Fire & Rescue Academy",
                date_label=dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y"),
                members_synced=len(member_coords),
                building_total=building_total,
                vehicle_total=vehicle_total,
                top_types=top_building_types(building_dicts),
                top_vehicle_types=top_vehicle_types(
                    vehicle_dicts, await self._vehicle_names()
                ),
                spots=spots,
                map_png=await self._map_image(spots),
            )
            card = render_infographic(snapshot)
        if card is not None:
            await ctx.send(file=discord.File(io.BytesIO(card), "alliance-snapshot.png"))
        else:  # Pillow missing — at least give the text list
            await ctx.send(render_hotspots(
                spots, member_total=len(member_coords),
                building_total=building_total,
            ))

    @commands.command(name="fleet")
    @is_fra_admin()
    async def fleet(self, ctx: commands.Context) -> None:
        """The alliance fleet card: `!fleet`."""
        import datetime as dt

        from ..services.game_sync import top_vehicle_types
        from ..services.infographic import render_fleet_card

        member_coords, _, vehicle_dicts, _, vehicle_total = (
            await self._sync_stats()
        )
        if not member_coords:
            await ctx.send(render_hotspots([], member_total=0, building_total=0))
            return
        async with ctx.typing():
            rows = top_vehicle_types(
                vehicle_dicts, await self._vehicle_names(), top=10
            )
            type_ids = set()
            for by_type in vehicle_dicts:
                for key in by_type:
                    try:
                        type_ids.add(int(key))
                    except (TypeError, ValueError):
                        continue
            card = render_fleet_card(
                title="Fire & Rescue Academy",
                date_label=dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y"),
                members_synced=len(member_coords),
                vehicle_total=vehicle_total,
                type_count=len(type_ids),
                top_vehicle_types=rows,
            )
        if card is not None:
            await ctx.send(
                file=discord.File(io.BytesIO(card), "alliance-fleet.png")
            )
        else:  # Pillow missing — at least give the numbers
            lines = [
                f"🚒 **Alliance fleet** — {vehicle_total:,} vehicles from "
                f"{len(member_coords)} synced member(s):"
            ] + [
                f"{rank}. **{name.capitalize()}** — {count:,}"
                for rank, (name, count) in enumerate(rows, 1)
            ]
            await ctx.send("\n".join(lines)[:1900])

    async def _named(self, spots):
        """Each hotspot with its reverse-geocoded place name; the names are
        decoration, so a geocoder problem never breaks the command."""
        named = []
        for spot in spots:
            place = None
            try:
                details = await self.bot.geocoder.reverse_details(
                    spot.latitude, spot.longitude
                )
                place = place_name(details)
            except Exception as exc:  # noqa: BLE001
                log.warning("hotspots: reverse geocode failed: %s", exc)
            named.append(replace(spot, place=place))
        return named

    async def _map_image(self, spots) -> bytes | None:
        if not spots:
            return None
        try:
            from ..geo.static_map import render_map

            return await render_map(
                [(s.latitude, s.longitude, s.buildings) for s in spots]
            )
        except Exception:  # noqa: BLE001 — the map is optional decoration
            log.warning("hotspots: map render failed", exc_info=True)
            return None

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
            f"{row['building_count']} buildings · "
            f"{row['vehicle_count']} vehicles"
        )
        if summary:
            line += f"\n{summary}"
        return line + f"\n*synced {str(row['synced_at'])[:16]}*"


async def setup(bot) -> None:
    await bot.add_cog(GameSyncCog(bot))
