"""Role pings when the bot starts an alliance mission or event.

The reference bot's eventpinger listened for MissionChief announcement
posts in a channel; we ARE the one starting missions, so the scheduler
records every real start in the ``event_pings`` outbox and this cog
delivers them — no channel dependency. The ping logic itself is the
reference bot's, verbatim:

* the Notify-Event role is always pinged;
* the address is resolved to a region role — "New York (NY)", the
  Bermuda role, or a country role like "Germany (DE)" — via the
  geocoder first (authoritative, on the mission's actual coordinates),
  then text heuristics (ZIP codes, place aliases, state names);
* an unresolved region pings Notify-Event only;
* the embed shows what started, where, the region, and what's next in
  the queue/rotation with its expected start time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

from ..db.repos import EventPingsRepo
from ..geo.geocoder import GeocodeError
from ..geo.regions import (
    RegionMatch,
    find_region_role,
    region_from_address_details,
    resolve_region,
)
from ..mc.parsers.events import EVENT_KINDS

log = logging.getLogger(__name__)

# A ping that sat unposted this long (channel misconfigured, bot down) is
# stale — the mission is well underway; posting it late is just noise.
MAX_PING_AGE_HOURS = 24


def announcement_label(kind: str) -> str:
    return "Alliance Mission" if kind == "large" else "Alliance Event"


def discord_timestamp(value) -> str:
    if not value:
        return "Unknown"
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return f"<t:{int(parsed.timestamp())}:F>"


def format_notification_mentions(
    notify_role_mention: str, region_role_mention: str | None
) -> str:
    mentions = [notify_role_mention]
    if region_role_mention:
        mentions.append(region_role_mention)
    return " ".join(mentions)


def build_notification_embed(
    kind: str,
    name: str | None,
    address: str | None,
    region: RegionMatch | None,
    next_details: dict | None = None,
) -> discord.Embed:
    """The reference bot's notification layout."""
    label = announcement_label(kind)
    embed = discord.Embed(
        title=f"MissionChief {label}",
        color=discord.Color.orange() if kind == "event" else discord.Color.blue(),
    )
    embed.add_field(name=label, value=name or "Unknown", inline=False)
    embed.add_field(name="Location", value=address or "Unknown", inline=False)
    embed.add_field(
        name="Region",
        value=region.name if region else "Unresolved, Notify-Event only",
        inline=False,
    )

    if next_details:
        embed.add_field(
            name=f"Next {label}",
            value="\n".join(
                [
                    f"Location: {next_details.get('location') or 'Unknown'}",
                    f"Type: {next_details.get('type') or 'Unknown'}",
                    f"Scheduled time: {discord_timestamp(next_details.get('scheduled_at'))}",
                ]
            ),
            inline=False,
        )
    return embed


class EventPingerCog(commands.Cog):
    """Delivers queued start pings to the event-pings channel."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.repo = EventPingsRepo(bot.db)
        self._lock = asyncio.Lock()
        self.ping_loop.start()

    def cog_unload(self) -> None:
        self.ping_loop.cancel()

    # -- delivery loop ----------------------------------------------------

    @tasks.loop(seconds=30)
    async def ping_loop(self) -> None:
        async with self._lock:
            try:
                await self._deliver_pending()
            except Exception:
                log.exception("event ping delivery failed")

    @ping_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver_pending(self) -> None:
        rows = await self.repo.unposted()
        if not rows:
            return
        channel = self.bot.channel_for("event_pings")
        for row in rows:
            if self._is_stale(row):
                log.warning("dropping stale event ping #%s (%s)", row["id"], row["name"])
                await self.repo.mark_posted(row["id"])
                continue
            if channel is None:
                log.warning(
                    "event ping #%s waiting — channels.event_pings not reachable",
                    row["id"],
                )
                return  # retry next tick; config fix makes these flow again
            try:
                await self._send_ping(channel, row)
            except discord.HTTPException as exc:
                status = getattr(exc, "status", None)
                if status is not None and 400 <= status < 500:
                    log.error("dropping unpostable event ping (HTTP %s)", status)
                    await self.repo.mark_posted(row["id"])
                    continue
                log.warning("transient failure posting event ping; retry next tick")
                return
            await self.repo.mark_posted(row["id"])

    @staticmethod
    def _is_stale(row) -> bool:
        try:
            created = dt.datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        age = dt.datetime.now(dt.timezone.utc) - created
        return age > dt.timedelta(hours=MAX_PING_AGE_HOURS)

    # -- one ping ----------------------------------------------------------

    async def _send_ping(self, channel, row) -> None:
        guild = getattr(channel, "guild", None)
        region = await self._resolve_region(row)

        notify_role_id = self.bot.cfg.discord.notify_event_role_id
        notify_role = guild.get_role(notify_role_id) if guild else None
        region_role = find_region_role(guild, region)

        notify_mention = getattr(notify_role, "mention", f"<@&{notify_role_id}>")
        region_mention = getattr(region_role, "mention", None)
        content = format_notification_mentions(notify_mention, region_mention)
        embed = build_notification_embed(
            row["kind"], row["name"], row["address"], region,
            await self._next_details(row),
        )
        await channel.send(
            content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False
            ),
        )

    async def _resolve_region(self, row) -> RegionMatch | None:
        """Reference order: geocode first (authoritative — and we have the
        mission's actual coordinates), then text heuristics."""
        lat, lng = row["latitude"], row["longitude"]
        if lat is not None and lng is not None:
            try:
                details = await self.bot.geocoder.reverse_details(lat, lng)
            except GeocodeError as exc:
                log.warning("region reverse geocode failed: %s", exc)
                details = None
            except Exception:
                log.exception("region reverse geocode failed")
                details = None
            if details:
                match = region_from_address_details(details)
                if match:
                    return match
        return resolve_region(row["address"] or "")

    async def _next_details(self, row) -> dict | None:
        """What starts next for this kind: the head of the member queue,
        else the rotation entry whose turn is next. The expected time is
        this start plus the kind's free-slot interval."""
        scheduler = getattr(self.bot, "missions_service", None)
        if scheduler is None:
            return None
        kind = row["kind"]
        nxt = None
        try:
            queued = await scheduler.missions.open_for_kind(kind, limit=1)
            if queued:
                nxt = queued[0]
            else:
                entries = [
                    e for e in await scheduler.rotation.list_all(active_only=True)
                    if e["kind"] == kind
                ]
                entries.sort(
                    key=lambda e: (e["last_started_at"] is not None,
                                   e["last_started_at"] or "", e["id"])
                )
                nxt = entries[0] if entries else None
        except Exception:
            log.exception("could not determine next %s for the ping", kind)
            return None
        if nxt is None:
            return None

        interval_days = EVENT_KINDS.get(kind, {}).get("free_interval_days", 1)
        try:
            started = dt.datetime.fromisoformat(row["created_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            started = dt.datetime.now(dt.timezone.utc)
        return {
            "location": nxt["address"] or nxt["location_text"] or "Unknown",
            "type": scheduler._ping_name(nxt),
            "scheduled_at": started + dt.timedelta(days=interval_days),
        }
