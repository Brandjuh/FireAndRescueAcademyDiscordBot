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

#: The intake webhook URL. Stored in STATE, never in config/git: the repo
#: is public and `!fra settings` prints config values unmasked — a webhook
#: URL is a write-credential. Set via `!fra syncwebhook`, or discovered.
WEBHOOK_URL_KEY = "game_sync:webhook_url"
#: Name of the auto-discovered/created webhook on the intake channel.
WEBHOOK_NAME = "FRA Profile Sync"
#: The shareable install link — a raw .user.js URL opens straight in
#: Tampermonkey, and doubles as the script's auto-update source.
RAW_INSTALL_URL = (
    "https://raw.githubusercontent.com/Brandjuh/"
    "FireAndRescueAcademyDiscordBot/main/tools/fra-profile-sync.user.js"
)
#: Stable custom_ids so the panel's buttons survive restarts.
_CUSTOM_ID = {
    "link": "fra:gamesync:link",
    "how": "fra:gamesync:how",
    "delete": "fra:gamesync:delete",
}

_HOW_IT_WORKS = (
    "**ℹ️ How the profile sync works, in detail**\n\n"
    "**When you press 🔄 Sync to FRA in the game** the script reads two "
    "pages of YOUR OWN account with your own browser session — "
    "`/api/buildings` and `/api/vehicles`, the same data you see in the "
    "game — and builds a summary:\n"
    "• your MC user id and name\n"
    "• building count per type, plus each building's coordinates rounded "
    "to ~100 m\n"
    "• vehicle count per type\n"
    "It shows you that exact summary first; nothing is sent until you "
    "confirm. It never reads or sends passwords, cookies, sessions, "
    "credits, chat, or anyone else's data.\n\n"
    "**After your first sync** the script refreshes automatically about "
    "once a day while you have MissionChief open — same data, no popups. "
    "The 🔄 button always forces a fresh sync on the spot.\n\n"
    "**Where it goes:** into a private intake channel of the FRA bot, "
    "which stores one record per MC account (a new sync overwrites the "
    "old one). It powers your `/profile` card and the alliance-wide "
    "hotspot map, fleet card and infographic — always aggregated, never "
    "listing your individual bases.\n\n"
    "**Your control:** 🗑️ *Delete my data* on this panel removes your "
    "record instantly, no admin needed. Removing the userscript from "
    "Tampermonkey stops all future syncs (deleting alone does not — the "
    "daily auto-sync would simply re-add the data)."
)


class GameSyncPanelView(discord.ui.View):
    """Persistent panel; re-registered at startup so its buttons survive
    restarts (``timeout=None`` + a stable ``custom_id`` per button)."""

    def __init__(self, cog: "GameSyncCog") -> None:
        super().__init__(timeout=None)
        self._cog = cog
        # Link buttons carry no custom_id and are never dispatched — the
        # URL opens client-side, which is exactly what an install needs.
        self.add_item(discord.ui.Button(
            label="Install userscript", emoji="📥",
            style=discord.ButtonStyle.link, url=RAW_INSTALL_URL,
        ))

    @discord.ui.button(label="Get sync link", emoji="🔑",
                       style=discord.ButtonStyle.primary,
                       custom_id=_CUSTOM_ID["link"])
    async def sync_link(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        await self._cog.send_sync_link(interaction)

    @discord.ui.button(label="How it works", emoji="ℹ️",
                       style=discord.ButtonStyle.secondary,
                       custom_id=_CUSTOM_ID["how"])
    async def how_it_works(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        await interaction.response.send_message(_HOW_IT_WORKS, ephemeral=True)

    @discord.ui.button(label="Delete my data", emoji="🗑️",
                       style=discord.ButtonStyle.danger,
                       custom_id=_CUSTOM_ID["delete"])
    async def delete_data(self, interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "This removes everything the profile sync has stored about "
            "your account. Are you sure?",
            view=_DeleteConfirmView(self._cog), ephemeral=True,
        )


class _DeleteConfirmView(discord.ui.View):
    """Ephemeral confirm step — a one-click destructive button on a
    persistent panel invites accidental taps. Short-lived, so it needs no
    ``add_view`` registration and no custom_ids."""

    def __init__(self, cog: "GameSyncCog") -> None:
        super().__init__(timeout=60)
        self._cog = cog

    @discord.ui.button(label="Yes, delete my data",
                       style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        await self._cog.perform_delete(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Nothing was deleted.", ephemeral=True
        )


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

    # -- the member panel (install / explain / delete) -----------------------

    async def webhook_url(self) -> str | None:
        """The sync webhook URL: the stored override first, else discover
        (or create) a webhook named ``WEBHOOK_NAME`` on the intake channel
        and cache it. None when neither path works — the caller tells the
        member to ask an admin."""
        from ..db.repos import StateRepo

        state = StateRepo(self.bot.db)
        stored = await state.get(WEBHOOK_URL_KEY)
        if stored:
            return stored
        channel = self.bot.get_channel(self._intake_channel_id())
        if channel is None:
            return None
        try:
            webhooks = await channel.webhooks()
            hook = next(
                (w for w in webhooks if w.name == WEBHOOK_NAME and w.url), None
            )
            if hook is None:
                hook = await channel.create_webhook(
                    name=WEBHOOK_NAME,
                    reason="Profile-sync intake (panel sync link)",
                )
        except discord.HTTPException as exc:
            log.warning("game sync: webhook discovery failed: %s", exc)
            return None
        await state.set(WEBHOOK_URL_KEY, hook.url)
        return hook.url

    async def send_sync_link(self, interaction: discord.Interaction) -> None:
        url = await self.webhook_url()
        if url is None:
            await interaction.response.send_message(
                "⚠️ The sync link isn't configured yet — please ping an "
                "admin.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "**Your FRA sync link** — the script asks for it once, on "
            "your first sync:\n"
            f"```{url}```\n"
            "**Setup:**\n"
            "1. Install the userscript with the 📥 button on the panel "
            "(Tampermonkey/Greasemonkey needed).\n"
            "2. Open the missionchief.com **main page** and click "
            "**🔄 Sync to FRA** in the navbar.\n"
            "3. Paste the link above when the script asks, check the "
            "summary, confirm.\n"
            "4. After that first sync the script refreshes automatically "
            "about once a day.\n\n"
            "-# Run `!verify` first so the data attaches to your Discord "
            "profile. Please keep the link within the alliance.",
            ephemeral=True,
        )

    async def perform_delete(self, interaction: discord.Interaction) -> None:
        """The confirm button's action: remove every game-sync row that is
        provably this member's — attached to their Discord id, or owned by
        the MC account of their APPROVED verify link (which also catches a
        row synced before they verified, stored with a NULL discord id)."""
        user = interaction.user
        mc_ids: set[int] = set()
        row = await self.repo.get_by_discord(user.id)
        if row is not None:
            mc_ids.add(int(row["mc_user_id"]))
        link = await LinksRepo(self.bot.db).get_by_discord(user.id)
        if link is not None and link["status"] == "approved":
            mc_ids.add(int(link["mc_user_id"]))
        deleted = 0
        for mc_id in mc_ids:
            if await self.repo.delete(mc_id):
                deleted += 1
        deleted += await self.repo.delete_by_discord(user.id)
        if not deleted:
            await interaction.response.send_message(
                "I have no synced game data stored for you. (Data only "
                "attaches to your Discord account after `!verify` — if "
                "you synced without verifying, ask an admin to remove it "
                "by MC account.)",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "🗑️ Your synced game data has been deleted. Note: the "
            "userscript syncs again automatically — remove it from "
            "Tampermonkey to stop future syncs.",
            ephemeral=True,
        )
        first_mc = next(iter(mc_ids), None)
        await self.bot.log_member_action(
            action="game_sync_deleted",
            detail=f"self-service via panel (MC {sorted(mc_ids) or '?'})",
            discord_user_id=user.id,
            mc_user_id=first_mc,
            actor_name=user.display_name,
        )

    def panel_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🔄 MissionChief Profile Sync",
            colour=discord.Colour.blue(),
            description=(
                "Share your own buildings and vehicles with the FRA bot — "
                "they power your `/profile` card and the alliance's "
                "hotspot map, fleet card and infographic.\n\n"
                "**What is collected**\n"
                "Only your own game data, read with your own browser "
                "session: building counts per type with coordinates "
                "rounded to ~100 m, vehicle counts per type, and your MC "
                "id + name. **Never** passwords, cookies, sessions or "
                "other players' data — the script shows you the exact "
                "summary before your first send. After your first manual "
                "sync it refreshes automatically about once a day while "
                "you play.\n\n"
                "**What it's used for**\n"
                "Your personal `/profile` card, and aggregated alliance "
                "statistics (hotspots, fleet, infographic). Individual "
                "bases are never listed publicly.\n\n"
                "**Your data, your control**\n"
                "🗑️ **Delete my data** below removes everything stored, "
                "instantly, no admin needed. Removing the userscript from "
                "Tampermonkey stops future syncs.\n\n"
                "**Get started**\n"
                "📥 install the script → 🔑 get your sync link → open "
                "missionchief.com and click **🔄 Sync to FRA**."
            ),
        )

    def panel_view(self) -> discord.ui.View:
        return GameSyncPanelView(self)

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
