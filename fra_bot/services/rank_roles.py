"""Credit rank roles — the old bot's RoleBasedCredits, ported.

Every linked alliance member carries exactly one Discord rank role based
on their MissionChief **earned credits** (Probie → … → Fire Commissioner,
the ladder below). The hourly sync:

* assigns the target rank role and removes any other rank role;
* announces **promotions** in the promotion channel — only after the very
  first sync has established a baseline (no congratulations spam on
  rollout), never for a member's first assignment (unless configured),
  and only for members carrying the verified role, exactly like the old
  bot;
* strips all rank roles from linked members who left the alliance (with
  the same roster-health gate the membersync prune uses).

Role changes themselves are NOT gated on the verified role or dry_run —
they mirror the roster, like the verified role itself does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import discord

from ..config import Config
from ..db.database import Database
from ..db.repos import LinksRepo, MembersRepo, StateRepo
from .membersync import MIN_SAFE_ROSTER_COUNT

log = logging.getLogger(__name__)

STATE_LAST_RANKS = "rank_roles:last"        # JSON: {discord_id: rank_key}
STATE_BASELINE = "rank_roles:baseline"      # set after the first full sync


@dataclass(frozen=True)
class CreditRank:
    key: str
    name: str
    min_credits: int


# The old bot's ladder, verbatim.
CREDIT_RANKS: tuple[CreditRank, ...] = (
    CreditRank("probie", "Probie", 0),
    CreditRank("firefighter", "Firefighter", 200),
    CreditRank("senior_firefighter", "Senior Firefighter", 10_000),
    CreditRank("fire_apparatus_operator", "Fire Apparatus Operator", 100_000),
    CreditRank("lieutenant", "Lieutenant", 1_000_000),
    CreditRank("captain", "Captain", 5_000_000),
    CreditRank("staff_captain", "Staff Captain", 20_000_000),
    CreditRank("battalion_chief", "Battalion Chief", 50_000_000),
    CreditRank("division_chief", "Division Chief", 1_000_000_000),
    CreditRank("deputy_chief", "Deputy Chief", 2_000_000_000),
    CreditRank("fire_chief", "Fire Chief", 5_000_000_000),
    CreditRank("fire_commissioner", "Fire Commissioner", 10_000_000_000),
)

RANKS_BY_KEY = {rank.key: rank for rank in CREDIT_RANKS}
_RANK_INDEX = {rank.key: index for index, rank in enumerate(CREDIT_RANKS)}

# The old bot's role ids (config.yaml can override per rank).
DEFAULT_RANK_ROLE_IDS: dict[str, int] = {
    "probie": 669488072911618048,
    "firefighter": 669488631811014657,
    "senior_firefighter": 669488681639346187,
    "fire_apparatus_operator": 669488729060147202,
    "lieutenant": 669488786480300062,
    "captain": 669488849780473856,
    "staff_captain": 669488888468733981,
    "battalion_chief": 669488934140641290,
    "division_chief": 669488982199107595,
    "deputy_chief": 669489030202916884,
    "fire_chief": 669489070166114314,
    "fire_commissioner": 1437513734364069940,
}


def rank_for_credits(credits: int) -> CreditRank:
    selected = CREDIT_RANKS[0]
    for rank in CREDIT_RANKS:
        if credits >= rank.min_credits:
            selected = rank
        else:
            break
    return selected


def is_promotion(previous_key: str | None, next_key: str) -> bool:
    if not previous_key or previous_key not in _RANK_INDEX:
        return False
    if next_key not in _RANK_INDEX:
        return False
    return _RANK_INDEX[next_key] > _RANK_INDEX[previous_key]


def promotion_text(member, rank: CreditRank) -> str:
    """The old bot's announcement, verbatim."""
    return f"Congratulations to {member.mention}.\nPromoted to **{rank.name}**."


class RankRolesService:
    # Small pause between members whose roles change: the first sync
    # touches everyone, and role edits are individually rate-limited.
    edit_delay = 1.0

    def __init__(self, cfg: Config, db: Database, bot) -> None:
        self._cfg = cfg
        self._bot = bot
        self._members = MembersRepo(db)
        self._links = LinksRepo(db)
        self._state = StateRepo(db)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _guild(self):
        guild_id = self._cfg.discord.guild_id
        if guild_id:
            return self._bot.get_guild(guild_id)
        guilds = getattr(self._bot, "guilds", [])
        return guilds[0] if guilds else None

    def _role_map(self, guild) -> dict[str, object]:
        """rank key → live Role object, for every configured id that
        actually exists in the guild."""
        configured = dict(DEFAULT_RANK_ROLE_IDS)
        configured.update(self._cfg.automation.rank_roles.role_ids or {})
        roles = {}
        for key, role_id in configured.items():
            role = guild.get_role(int(role_id)) if role_id else None
            if role is not None and key in RANKS_BY_KEY:
                roles[key] = role
        return roles

    async def _last_ranks(self) -> dict[str, str]:
        raw = await self._state.get(STATE_LAST_RANKS)
        if not raw:
            return {}
        try:
            return {str(k): str(v) for k, v in json.loads(raw).items()}
        except ValueError:
            return {}

    async def _save_last_ranks(self, last: dict[str, str]) -> None:
        await self._state.set(STATE_LAST_RANKS, json.dumps(last))

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    async def sync(self, *, dry_run: bool = False) -> dict:
        guild = self._guild()
        if guild is None:
            return self._summary(error="No guild available.")
        role_map = self._role_map(guild)
        if not role_map:
            return self._summary(
                error="None of the configured rank roles exist in this server."
            )
        rank_role_ids = {role.id for role in role_map.values()}

        active = await self._members.active_members()
        if len(active) < MIN_SAFE_ROSTER_COUNT:
            return self._summary(
                error=f"Roster looks unhealthy ({len(active)} active members) "
                      "— rank sync deferred."
            )
        links = await self._links.all_approved()
        baseline = await self._state.get(STATE_BASELINE) is not None
        announce_first = self._cfg.automation.rank_roles.announce_first_assignment
        verified_role_id = self._cfg.discord.verified_role_id
        last = await self._last_ranks()

        updated = skipped = promotions = departures = 0
        for link in links:
            discord_id = link["discord_id"]
            member = guild.get_member(int(discord_id))
            if member is None or getattr(member, "bot", False):
                skipped += 1
                continue

            roster_row = active.get(link["mc_user_id"])
            if roster_row is None:
                # Left the alliance: all rank roles go (the verified-role
                # prune handles the verified role separately).
                rank_roles = [r for r in member.roles if r.id in rank_role_ids]
                departures += 1
                if rank_roles and not dry_run:
                    try:
                        await member.remove_roles(
                            *rank_roles,
                            reason="Rank roles: member left the alliance",
                        )
                        last.pop(str(discord_id), None)
                        await asyncio.sleep(self.edit_delay)
                    except discord.HTTPException:
                        log.exception("Could not strip rank roles from %s", member)
                continue

            try:
                credits = int(roster_row["earned_credits"] or 0)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if credits < 0:
                skipped += 1
                continue

            target = rank_for_credits(credits)
            target_role = role_map.get(target.key)
            if target_role is None:
                skipped += 1
                continue

            current_key = self._current_rank_key(member, role_map)
            previous_key = last.get(str(discord_id)) or current_key
            to_remove = [
                r for r in member.roles
                if r.id in rank_role_ids and r.id != target_role.id
            ]
            to_add = [] if target_role in member.roles else [target_role]
            first_assignment = previous_key is None and current_key is None
            announce = (
                baseline
                and not (first_assignment and not announce_first)
                and is_promotion(previous_key, target.key)
                and (
                    not verified_role_id
                    or any(r.id == verified_role_id for r in member.roles)
                )
            )

            if dry_run:
                if to_add or to_remove:
                    updated += 1
                if announce:
                    promotions += 1
                continue

            try:
                if to_remove:
                    await member.remove_roles(
                        *to_remove,
                        reason="Rank roles: rank changed from MissionChief credits",
                    )
                if to_add:
                    await member.add_roles(
                        *to_add,
                        reason="Rank roles: rank reached from MissionChief credits",
                    )
                if to_add or to_remove:
                    updated += 1
                    await asyncio.sleep(self.edit_delay)
                if announce:
                    await self._announce(member, target)
                    promotions += 1
                last[str(discord_id)] = target.key
            except discord.HTTPException:
                log.exception("Could not update rank role for %s", member)
                skipped += 1

        if not dry_run:
            await self._save_last_ranks(last)
            if not baseline:
                # First full pass done: from now on changes are real
                # promotions and may be congratulated.
                await self._state.set(STATE_BASELINE, "1")
        return self._summary(
            updated=updated, skipped=skipped, promotions=promotions,
            departures=departures, dry_run=dry_run,
        )

    @staticmethod
    def _current_rank_key(member, role_map: dict[str, object]) -> str | None:
        member_role_ids = {r.id for r in getattr(member, "roles", [])}
        selected = None
        for rank in CREDIT_RANKS:
            role = role_map.get(rank.key)
            if role is not None and role.id in member_role_ids:
                selected = rank.key
        return selected

    async def _announce(self, member, rank: CreditRank) -> None:
        channel_id = self._cfg.automation.rank_roles.promotion_channel_id
        channel = self._bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(promotion_text(member, rank))
        except discord.HTTPException as exc:
            log.warning("Could not announce promotion for %s: %s", member, exc)

    @staticmethod
    def _summary(*, error: str | None = None, **counts) -> dict:
        if error:
            return {"error": error, "lines": [error], "changed": False}
        prefix = "[dry-run] " if counts.get("dry_run") else ""
        lines = [
            f"{prefix}{counts.get('updated', 0)} role change(s), "
            f"{counts.get('promotions', 0)} promotion(s) announced, "
            f"{counts.get('departures', 0)} departure(s), "
            f"{counts.get('skipped', 0)} skipped"
        ]
        return {
            **counts, "error": None, "lines": lines,
            "changed": bool(counts.get("updated") or counts.get("promotions")),
        }
