"""Vehicles-database forum: every vehicle from the LSSM catalog as a post.

A close mirror of :mod:`fra_bot.services.missions_forum`: one tagged forum
post per vehicle, fully rendered in English, edited in place when the data
changes (no bump, no duplicate). The bot owns the forum — it creates the tag
set, turns ``require_tag`` on, posts new vehicles and re-adopts its own posts
from the thread titles after a database loss.

Duplicate prevention is the ``vehicles_forum_posts`` table (vehicle_key →
thread_id + content hash). The catalog is fetched from GitHub (raw LSSM
``vehicles.ts``), NOT from MissionChief, so a plain aiohttp session is used
and this sync is safe regardless of ``dry_run`` (it never touches the game).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re

import discord

from ..config import Config
from ..db.database import Database
from ..db.repos import StateRepo, VehiclesForumRepo
from ..mc import vehicles_catalog as catalog
from .missions_forum import _is_active_thread_cap

log = logging.getLogger(__name__)

STATE_LAST_SYNC = "vehicles_forum:last_sync"
# Set once a sync has covered the whole catalog without hitting the post cap.
# Announcements only fire after that: the multi-run initial backfill must
# never ping the announcement channel, not even its later runs.
STATE_BACKFILL_DONE = "vehicles_forum:backfill_done"
# Last-known building id → name map. buildings.ts is a slowly-changing
# secondary lookup fetched separately from vehicles.ts; caching it means a
# transient buildings.ts failure reuses the real names instead of degrading
# every label to "Building N" — which, because names are part of the hashed
# record, would otherwise flip all ~114 hashes and fake a mass "vehicle
# updated" storm (notes + a false announcement), then flip back on recovery.
STATE_BUILDING_NAMES = "vehicles_forum:building_names"

# Thread titles end in " · #<key>" (key = "veh-<id>") — the recovery marker
# that lets the bot re-adopt its own posts after a database loss.
_TITLE_KEY_RE = re.compile(r"·\s*#(\S+)\s*$")

_CATEGORY_COLOURS = {
    "Fire": 0xE74C3C,
    "EMS": 0xE67E22,
    "Police": 0x3498DB,
    "Water Rescue": 0x1ABC9C,
}
_DEFAULT_COLOUR = 0x95A5A6  # grey, for vehicles without a mapped category


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def vehicle_name(vehicle: dict) -> str:
    return str(vehicle.get("name") or catalog.vehicle_key(vehicle))


def thread_title(name: str, key: str) -> str:
    """``"{name} · #{key}"`` within Discord's 100-char thread-name limit."""
    suffix = f" · #{key}"
    room = max(1, 100 - len(suffix))
    return _truncate(f"{_truncate(name, room)}{suffix}", 100)


def thread_key(title: str) -> str | None:
    """Parse the vehicle key back out of a thread title (adopt/recovery)."""
    match = _TITLE_KEY_RE.search(title or "")
    return match.group(1) if match else None


def _price_text(vehicle: dict) -> str:
    credits = vehicle.get("credits")
    coins = vehicle.get("coins")
    parts = []
    if credits is not None:
        parts.append(f"{credits:,} credits")
    if coins is not None:
        parts.append(f"{coins:,} coins")
    return " · ".join(parts) or "—"


def _crew_text(vehicle: dict) -> str:
    low, high = vehicle.get("staff_min"), vehicle.get("staff_max")
    if low is None and high is None:
        return "—"
    if low == high or high is None:
        return str(low if low is not None else high)
    if low is None:
        return f"up to {high}"
    return f"{low}–{high}"


def _tank_text(vehicle: dict) -> str | None:
    parts = []
    if vehicle.get("water_tank"):
        parts.append(f"💧 Water: {vehicle['water_tank']:,} gal")
    if vehicle.get("foam_tank"):
        parts.append(f"🧴 Foam: {vehicle['foam_tank']:,} gal")
    if vehicle.get("pump_capacity"):
        parts.append(f"⚙️ Pump: {vehicle['pump_capacity']:,} gpm")
    elif vehicle.get("pump_type"):
        parts.append(f"⚙️ Pump: {vehicle['pump_type']}")
    return "\n".join(parts) or None


def _category_of(vehicle: dict) -> str | None:
    for tag in catalog.infer_tags(vehicle):
        if tag in _CATEGORY_COLOURS:
            return tag
    return None


def build_vehicle_embed(vehicle: dict, *, updated: str | None = None) -> discord.Embed:
    """The full vehicle card. Empty sections are omitted and every field is
    trimmed to Discord's limits."""
    key = catalog.vehicle_key(vehicle)
    name = vehicle_name(vehicle)
    colour = _CATEGORY_COLOURS.get(_category_of(vehicle) or "", _DEFAULT_COLOUR)

    fields: list[tuple[str, str, bool]] = [
        ("💰 Price", _price_text(vehicle), True),
        ("👥 Crew", _crew_text(vehicle), True),
    ]
    if vehicle.get("equipment_capacity"):
        fields.append(("🧰 Equipment slots", str(vehicle["equipment_capacity"]), True))
    buildings = vehicle.get("buildings") or []
    if buildings:
        fields.append(
            ("🏢 Available at", _truncate(", ".join(buildings), 1024), False)
        )
    tanks = _tank_text(vehicle)
    if tanks:
        fields.append(("🚰 Tanks & pump", tanks, False))
    trainings = vehicle.get("trainings") or []
    if trainings:
        fields.append(
            ("🎓 Trainings required", _truncate("\n".join(trainings), 1024), False)
        )
    if vehicle.get("is_trailer"):
        fields.append(("🚚 Trailer", "Towed — needs a prime mover to deploy.", False))
    if vehicle.get("special"):
        fields.append(("ℹ️ Notes", _truncate(str(vehicle["special"]), 1024), False))

    stamp = updated or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    footer = f"Vehicle #{key} · source: LSS-Manager · updated {stamp}"

    for field_limit in (1024, 512, 256):
        embed = discord.Embed(title=_truncate(name, 256), colour=discord.Colour(colour))
        for field_name, value, inline in fields:
            embed.add_field(
                name=field_name, value=_truncate(value, field_limit), inline=inline
            )
        embed.set_footer(text=footer)
        if len(embed) <= 5900:
            break
    return embed


class VehiclesForumService:
    """Syncs the LSSM vehicle catalog into the configured Discord forum."""

    # Pacing between Discord writes on top of the library's 429 handling.
    post_delay = 2.0
    batch_size = 10
    batch_delay = 10.0

    def __init__(self, cfg: Config, db: Database, bot) -> None:
        self._cfg = cfg
        self._bot = bot
        self._repo = VehiclesForumRepo(db)
        self._state = StateRepo(db)
        # `!fra vehiclesforum stop` raises this; the running loop checks it
        # between posts and bows out cleanly.
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    # Channel plumbing
    # ------------------------------------------------------------------

    def forum(self) -> discord.ForumChannel | None:
        channel_id = self._cfg.discord.channels.vehicles_forum
        if not channel_id:
            return None
        channel = self._bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "create_thread"):
            return None
        return channel

    async def _get_thread(self, thread_id: int):
        """A thread by id, archived ones included; None when it's GONE.

        Only a definitive NotFound maps to None — None makes the caller
        delete the mapping and repost. A transient error (rate limit, 5xx)
        re-raises instead, so the per-vehicle handler in the sync loop logs
        it and the mapping survives for the next pass (no duplicate post)."""
        thread = self._bot.get_channel(thread_id)
        if thread is not None:
            return thread
        try:
            return await self._bot.fetch_channel(thread_id)
        except discord.NotFound:
            return None

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    async def ensure_tags(self, forum) -> tuple[list[str], object]:
        """Create any missing forum tags (one bulk edit) and make sure posts
        require a tag. Returns (created tag names, fresh forum).

        The require_tag flag goes in a SEPARATE edit after the tags exist:
        Discord validates the flag against the forum's pre-edit tag list, so
        combining it with tag creation fails on a fresh forum with 40066."""
        def norm(name) -> str:
            return " ".join(str(name).split()).casefold()

        current = list(forum.available_tags)
        names = {norm(tag.name) for tag in current}
        renamed = []
        for old, new in catalog.RENAMED_TAGS.items():
            old_n, new_n = norm(old), norm(new)
            if old_n not in names:
                continue
            if new_n in names:
                current = [t for t in current if norm(t.name) != old_n]
                names.discard(old_n)
                renamed.append(f"removed stale tag '{old}'")
                continue
            for tag in current:
                if norm(tag.name) == old_n:
                    tag.name = new
                    renamed.append(f"{old} → {new}")
                    names = (names - {old_n}) | {new_n}
                    break
        missing = [
            name for name in catalog.FORUM_TAG_EMOJI if norm(name) not in names
        ]
        # The fallback tag has priority: it keeps a tag-less vehicle postable
        # once require_tag is on.
        missing.sort(key=lambda name: name != catalog.FALLBACK_TAG)
        room = max(0, 20 - len(current))  # Discord allows 20 tags per forum.
        if len(missing) > room:
            log.warning(
                "Forum has only %d free tag slots; skipping tags: %s",
                room, ", ".join(missing[room:]),
            )
            missing = missing[:room]
        if missing or renamed:
            try:
                updated = await forum.edit(
                    available_tags=current + [
                        discord.ForumTag(name=name, emoji=catalog.FORUM_TAG_EMOJI[name])
                        for name in missing
                    ],
                    reason="Vehicles database tags",
                )
                forum = updated or forum
                for line in renamed:
                    log.info("Vehicles-forum tag renamed: %s", line)
            except discord.HTTPException as exc:
                # No Manage Channels? Keep posting (untagged posts are allowed
                # while require_tag is off) instead of zero-progressing daily.
                log.warning("Could not create vehicles-forum tags: %s", exc)
                return [], forum
        names = {tag.name for tag in forum.available_tags}
        require_tag = bool(getattr(getattr(forum, "flags", None), "require_tag", False))
        if catalog.FALLBACK_TAG in names and not require_tag:
            try:
                updated = await forum.edit(require_tag=True)
                forum = updated or forum
            except discord.HTTPException as exc:
                log.warning(
                    "Could not enable require_tag on the vehicles forum: %s", exc
                )
        return missing + renamed, forum

    def _applied_tags(self, forum, vehicle: dict) -> list:
        by_name = {tag.name: tag for tag in forum.available_tags}
        wanted = catalog.infer_tags(vehicle)
        tags = [by_name[name] for name in wanted if name in by_name][:5]
        if not tags and catalog.FALLBACK_TAG in by_name:
            tags = [by_name[catalog.FALLBACK_TAG]]  # require_tag refuses tag-less
        return tags

    # ------------------------------------------------------------------
    # Adopt (DB-loss recovery)
    # ------------------------------------------------------------------

    async def adopt(self, forum) -> int:
        """Rebuild the vehicle→thread mapping from thread titles, so an empty
        database never causes a duplicate flood. Adopted rows get an empty
        content hash — the next sync refreshes their content."""
        adopted = 0
        threads = list(getattr(forum, "threads", []) or [])
        try:
            async for thread in forum.archived_threads(limit=None):
                threads.append(thread)
        except discord.HTTPException as exc:
            log.warning("Could not list archived vehicle threads: %s", exc)
        for thread in threads:
            key = thread_key(getattr(thread, "name", ""))
            if not key or await self._repo.get(key) is not None:
                continue
            await self._repo.record(key, thread.id, "", getattr(thread, "name", ""))
            adopted += 1
        return adopted

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    async def _fetch_catalog(self) -> list[dict]:
        """Fetch + parse the LSSM vehicle catalog over a fresh session (the
        catalog lives on GitHub, not MissionChief)."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            names = await self._building_names(session)
            return await catalog.fetch_catalog(session, building_names=names)

    async def _building_names(self, session) -> dict[int, str]:
        """The building id → name map, fetched fresh and cached in state. On a
        transient buildings.ts failure we reuse the last-known map rather than
        degrading every label to "Building N" (which would flip every vehicle
        hash and fake a mass "updated"). An empty fetch is treated as a failure
        — it's indistinguishable from a bad response — so the cache wins."""
        try:
            names = await catalog.fetch_building_names(session)
        except ValueError as exc:
            cached = await self._cached_building_names()
            log.warning(
                "vehicles forum: building names unavailable (%s); reusing %d cached",
                exc, len(cached),
            )
            return cached
        if not names:
            return await self._cached_building_names()
        await self._state.set(
            STATE_BUILDING_NAMES,
            json.dumps({str(k): v for k, v in names.items()}),
        )
        return names

    async def _cached_building_names(self) -> dict[int, str]:
        raw = await self._state.get(STATE_BUILDING_NAMES)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        out: dict[int, str] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    out[int(key)] = str(value)
                except (TypeError, ValueError):
                    continue
        return out

    async def sync(self, *, limit: int | None = None, force: bool = False) -> dict:
        """One full pass: fetch the catalog, ensure tags, post new vehicles,
        edit changed ones. Returns a summary dict with ``lines`` for humans.

        ``limit`` caps posts+edits this run (default: config
        ``max_posts_per_run``); ``force`` re-renders even unchanged posts."""
        self._stop = False
        forum = self.forum()
        if forum is None:
            return self._summary(
                error="Vehicles forum is not configured — set a forum channel "
                      "id with `!fra set vehicles_forum <id>`."
            )

        try:
            vehicles = await self._fetch_catalog()
        except ValueError as exc:
            return self._summary(error=f"LSSM vehicle catalog is unusable: {exc}")
        if not vehicles:
            return self._summary(error="LSSM vehicle catalog returned no vehicles.")

        tags_created, forum = await self.ensure_tags(forum)

        # Empty mapping + existing posts = the DB was lost; re-adopt instead
        # of reposting the whole catalog as duplicates.
        adopted = 0
        if await self._repo.count() == 0:
            adopted = await self.adopt(forum)

        backfill_done = await self._state.get(STATE_BACKFILL_DONE) is not None
        announce = (
            self._cfg.automation.vehicles_forum.announce_new and backfill_done
        )

        # Untracked ACTIVE threads with our title marker (a crash between post
        # and bookkeeping happens before the archive step) — reclaim instead
        # of duplicating.
        orphans: dict[str, object] = {}
        for thread in getattr(forum, "threads", []) or []:
            if getattr(thread, "archived", False):
                continue
            orphan_key = thread_key(getattr(thread, "name", ""))
            if orphan_key:
                orphans[orphan_key] = thread

        cap = limit or self._cfg.automation.vehicles_forum.max_posts_per_run
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

        created = updated = skipped = failed = announced = 0
        capped = stopped = hit_active_cap = False
        seen_unchanged: list[str] = []
        seen_keys: set[str] = set()
        data_updates: list[dict] = []
        writes = 0

        # Self-heal: re-archive stray active posts we own (a prior archive
        # failure leaves a post ACTIVE, counting toward Discord's 1000
        # active-thread guild cap and stalling the backfill). No-op once every
        # post is archived.
        healed = 0
        for stray_key, stray_thread in orphans.items():
            if healed >= cap:
                break
            if await self._repo.get(stray_key) is not None:
                await self._archive(stray_thread)
                healed += 1
                await self._pause(healed)

        vehicles.sort(key=lambda v: v["id"])
        for vehicle in vehicles:
            if self._stop:
                stopped = True
                break
            key = catalog.vehicle_key(vehicle)
            if key in seen_keys:
                log.warning("Duplicate vehicle key %s in catalog; skipped", key)
                continue
            seen_keys.add(key)
            digest = catalog.content_hash(vehicle)
            row = await self._repo.get(key)
            if row is None and key in orphans:
                orphan = orphans[key]
                await self._repo.record(key, orphan.id, "", vehicle_name(vehicle))
                row = await self._repo.get(key)
            if row is not None and row["content_hash"] == digest and not force:
                seen_unchanged.append(key)
                skipped += 1
                continue
            if writes >= cap:
                capped = True
                break
            try:
                if row is None:
                    thread = await self._create_post(forum, vehicle, digest, stamp)
                    created += 1
                    if announce and thread is not None:
                        announced += await self._announce(vehicle, thread)
                else:
                    outcome, changed_note = await self._update_post(
                        forum, vehicle, row, digest, stamp
                    )
                    if outcome == "recreated":
                        created += 1
                    else:
                        updated += 1
                    if changed_note:
                        data_updates.append(changed_note)
            except discord.HTTPException as exc:
                if _is_active_thread_cap(exc):
                    # Guild-wide wall: every further create fails alike. Stop
                    # rather than hammer the API through the rest of the run.
                    hit_active_cap = True
                    log.warning(
                        "Vehicles forum: Discord's 1000 active-thread guild cap "
                        "is reached; stopping this run at %d created. Free "
                        "active threads (archived posts don't count) to resume.",
                        created,
                    )
                    break
                failed += 1
                log.exception("Vehicle forum post %s failed", key)
                continue
            except Exception:  # noqa: BLE001 — one bad vehicle must never abort
                failed += 1
                log.exception("Vehicle forum post %s failed", key)
                continue
            writes += 1
            await self._pause(writes)

        if announce and data_updates:
            announced += await self._announce_updates(data_updates)

        await self._repo.touch_seen(seen_unchanged)
        if not capped and not failed and not stopped and not hit_active_cap:
            await self._state.set(STATE_BACKFILL_DONE, stamp)
        summary = self._summary(
            created=created, updated=updated, skipped=skipped, failed=failed,
            announced=announced, adopted=adopted, tags_created=tags_created,
            capped=capped, cap=cap, total=len(vehicles), stopped=stopped,
            hit_active_cap=hit_active_cap,
        )
        await self._state.set(
            STATE_LAST_SYNC,
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            + " — " + summary["lines"][0],
        )
        return summary

    async def _create_post(self, forum, vehicle: dict, digest: str, stamp: str):
        key = catalog.vehicle_key(vehicle)
        name = vehicle_name(vehicle)
        embed = build_vehicle_embed(vehicle, updated=stamp)
        result = await forum.create_thread(
            name=thread_title(name, key),
            embed=embed,
            applied_tags=self._applied_tags(forum, vehicle),
            reason="Vehicles database sync",
        )
        thread = getattr(result, "thread", result)
        await self._repo.record(
            key, thread.id, digest, name, data_hash=catalog.data_hash(vehicle)
        )
        await self._archive(thread)
        return thread

    async def _archive(self, thread) -> None:
        """Archive a vehicle post right away: Discord caps a guild at 1000
        ACTIVE threads. Archived forum posts stay visible and searchable; a
        member's reply re-opens one automatically."""
        try:
            await thread.edit(archived=True)
        except discord.HTTPException as exc:
            # Loud, not debug: an unarchived post keeps counting toward the
            # 1000 active-thread guild cap and eventually stalls the backfill.
            log.warning(
                "Could not archive vehicle thread %s (%s); it stays ACTIVE. If "
                "this persists, grant the bot 'Manage Threads' in the forum.",
                thread.id, exc,
            )

    async def _update_post(
        self, forum, vehicle: dict, row, digest: str, stamp: str
    ) -> tuple[str, dict | None]:
        """Edit an existing post in place; repost if its thread is gone."""
        key = catalog.vehicle_key(vehicle)
        name = vehicle_name(vehicle)
        thread = await self._get_thread(int(row["thread_id"]))
        if thread is None:
            await self._repo.delete(key)
            await self._create_post(forum, vehicle, digest, stamp)
            return "recreated", None
        if getattr(thread, "archived", False):
            await thread.edit(archived=False)
        embed = build_vehicle_embed(vehicle, updated=stamp)
        try:
            starter = await thread.fetch_message(thread.id)
        except discord.NotFound:
            log.warning(
                "Starter message of vehicle %s (thread %s) is gone; reposting",
                key, thread.id,
            )
            await self._repo.delete(key)
            await self._create_post(forum, vehicle, digest, stamp)
            return "recreated", None
        await starter.edit(embed=embed)
        title = thread_title(name, key)
        wanted_tags = self._applied_tags(forum, vehicle)
        current_tags = {
            getattr(t, "name", None) for t in getattr(thread, "applied_tags", [])
        }
        edits: dict = {}
        if getattr(thread, "name", None) != title:
            edits["name"] = title
        if {t.name for t in wanted_tags} != current_tags:
            edits["applied_tags"] = wanted_tags
        if edits:
            await thread.edit(**edits)
        # A REAL data change also gets a message in the thread (the full card),
        # so the change history stays readable and the post bumps. A bot-side
        # re-render (format bump) or a legacy row without a stored data hash
        # stays silent — a format bump must never spam every thread.
        changed_note = None
        new_data_hash = catalog.data_hash(vehicle)
        old_data_hash = _row_value(row, "data_hash")
        if old_data_hash is not None and old_data_hash != new_data_hash:
            try:
                await thread.send(
                    content=f"🔄 **Vehicle updated** — current data below ({stamp}):",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                log.warning("Could not post update note for %s: %s", key, exc)
            changed_note = {
                "name": name,
                "jump_url": getattr(thread, "jump_url", ""),
            }
        await self._repo.record(key, thread.id, digest, name, data_hash=new_data_hash)
        await self._archive(thread)
        return "updated", changed_note

    async def wipe(self) -> dict:
        """Delete EVERY vehicle post (tracked rows plus any thread carrying
        our title marker) and forget the mapping — a clean slate for a full
        repost. The backfill flag resets too."""
        self._stop = False
        forum = self.forum()
        if forum is None:
            return self._summary(
                error="Vehicles forum is not configured — nothing to wipe."
            )
        targets: dict[int, str | None] = {}
        for row in await self._repo.all():
            targets[int(row["thread_id"])] = row["vehicle_key"]
        threads = list(getattr(forum, "threads", []) or [])
        try:
            async for thread in forum.archived_threads(limit=None):
                threads.append(thread)
        except discord.HTTPException as exc:
            log.warning("Could not list archived vehicle threads: %s", exc)
        for thread in threads:
            if thread_key(getattr(thread, "name", "")):
                targets.setdefault(thread.id, None)

        deleted = failed = 0
        stopped = False
        for thread_id, vehicle_key in targets.items():
            if self._stop:
                stopped = True
                break
            try:
                thread = await self._get_thread(thread_id)
            except discord.HTTPException as exc:
                # Transient fetch error: skip this thread, KEEP its mapping
                # so a re-run can still delete it — never abort the wipe.
                failed += 1
                log.warning("Could not fetch vehicle thread %s during wipe: %s",
                            thread_id, exc)
                continue
            if thread is None:
                if vehicle_key:
                    await self._repo.delete(vehicle_key)
                continue
            try:
                await thread.delete()
            except discord.HTTPException as exc:
                failed += 1
                log.error("Could not delete vehicle thread %s: %s", thread_id, exc)
                continue
            if vehicle_key:
                await self._repo.delete(vehicle_key)
            deleted += 1
            await self._pause(deleted)

        await self._state.delete(STATE_BACKFILL_DONE)
        lines = [f"deleted {deleted} post(s), {failed} failed"]
        if stopped:
            lines.append("⏹️ stopped by admin — run wipe again for the rest")
        remaining = await self._repo.count()
        if remaining:
            lines.append(f"{remaining} mapping row(s) left (their threads remain)")
        return {
            "error": None, "deleted": deleted, "failed": failed,
            "stopped": stopped, "lines": lines, "changed": bool(deleted or failed),
        }

    # ------------------------------------------------------------------
    # Announcements
    # ------------------------------------------------------------------

    def _announce_channel(self):
        channel_id = self._cfg.discord.channels.vehicle_announce
        return self._bot.get_channel(channel_id) if channel_id else None

    def _announce_role_prefix(self):
        """(ping text, allowed_mentions) for the optional new-vehicle role."""
        role_id = getattr(self._cfg.discord, "vehicle_announce_role_id", 0)
        if role_id:
            return (
                f"<@&{role_id}> ",
                discord.AllowedMentions(roles=[discord.Object(id=role_id)]),
            )
        return "", discord.AllowedMentions.none()

    async def _announce(self, vehicle: dict, thread) -> int:
        channel = self._announce_channel()
        if channel is None:
            return 0
        name = vehicle_name(vehicle)
        jump = getattr(thread, "jump_url", "") or getattr(thread, "mention", "")
        ping, mentions = self._announce_role_prefix()
        try:
            await channel.send(
                f"{ping}🆕 **New vehicle in MissionChief:** {name}\n➡️ {jump}",
                allowed_mentions=mentions,
            )
            return 1
        except discord.HTTPException as exc:
            log.warning("Could not announce new vehicle %s: %s", name, exc)
            return 0

    async def _announce_updates(self, updates: list[dict]) -> int:
        """One bundled announcement for all vehicles that changed this run."""
        channel = self._announce_channel()
        if channel is None:
            return 0
        shown = updates[:10]
        lines = [
            f"• **{note['name']}** — {note['jump_url']}".rstrip(" —")
            for note in shown
        ]
        if len(updates) > len(shown):
            lines.append(f"… and {len(updates) - len(shown)} more (see the forum)")
        header = (
            "🔄 **Vehicle updated in MissionChief:**"
            if len(updates) == 1
            else f"🔄 **{len(updates)} vehicles updated in MissionChief:**"
        )
        try:
            await channel.send(
                f"{header}\n" + "\n".join(lines)[:1800],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return 1
        except discord.HTTPException as exc:
            log.warning("Could not announce vehicle updates: %s", exc)
            return 0

    async def _pause(self, writes: int) -> None:
        if self.post_delay:
            await asyncio.sleep(self.post_delay)
        if self.batch_delay and writes % self.batch_size == 0:
            await asyncio.sleep(self.batch_delay)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def status_lines(self) -> list[str]:
        auto = self._cfg.automation.vehicles_forum
        channels = self._cfg.discord.channels
        forum = self.forum()
        lines = [
            "forum: "
            + (
                f"<#{channels.vehicles_forum}>"
                + ("" if forum else " (⚠️ not reachable / not a forum)")
                if channels.vehicles_forum
                else "not set (`!fra set vehicles_forum <id>`)"
            ),
            f"tracked posts: {await self._repo.count()}",
            f"daily sync: {'on, ' + auto.sync_time if auto.enabled else 'OFF'}"
            f" · post cap {auto.max_posts_per_run}/run",
            "new-vehicle ping: "
            + (
                f"on → <#{channels.vehicle_announce}>"
                if auto.announce_new and channels.vehicle_announce else "off"
            ),
        ]
        backfill = await self._state.get(STATE_BACKFILL_DONE)
        lines.append(
            f"backfill: ✅ complete ({backfill})" if backfill
            else "backfill: ⏳ in progress — the hourly catch-up keeps posting"
        )
        last = await self._state.get(STATE_LAST_SYNC)
        lines.append(f"last sync: {last or 'never'}")
        return lines

    @staticmethod
    def _summary(*, error: str | None = None, **counts) -> dict:
        if error:
            return {"error": error, "lines": [error], "changed": False}
        created = counts.get("created", 0)
        updated = counts.get("updated", 0)
        lines = [
            f"created {created}, updated {updated}, "
            f"unchanged {counts.get('skipped', 0)}, failed {counts.get('failed', 0)} "
            f"(of {counts.get('total', 0)} vehicles)"
        ]
        if counts.get("tags_created"):
            lines.append("tag changes: " + ", ".join(counts["tags_created"]))
        if counts.get("adopted"):
            lines.append(
                f"re-adopted {counts['adopted']} existing post(s) from the forum"
            )
        if counts.get("announced"):
            lines.append(
                f"sent {counts['announced']} announcement(s) (new/updated vehicles)"
            )
        if counts.get("stopped"):
            lines.append("⏹️ stopped by admin — the rest follows on the next sync")
        elif counts.get("capped"):
            lines.append(
                f"post cap reached ({counts.get('cap')}/run) — "
                "the rest follows on the next sync"
            )
        if counts.get("hit_active_cap"):
            lines.append(
                "⛔ Discord's 1000 active-thread guild limit was hit — no new "
                "post can be created until active threads are freed. Archived "
                "posts don't count; grant the bot 'Manage Threads' so it can "
                "archive, then the backfill resumes."
            )
        changed = bool(
            created or updated or counts.get("failed", 0)
            or counts.get("tags_created") or counts.get("adopted")
            or counts.get("hit_active_cap")
        )
        return {**counts, "error": None, "lines": lines, "changed": changed}


def _row_value(row, column: str):
    """A column from an sqlite row, None when the column doesn't exist."""
    try:
        return row[column]
    except (KeyError, IndexError):
        return None
