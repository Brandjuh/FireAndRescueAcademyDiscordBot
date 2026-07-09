"""The member dossier: everything we know about one alliance member.

Modelled on the reference bot's MemberManager staff console, built on OUR
OWN data: the scraped roster (members + snapshots), the treasury income
snapshots (what a member contributed to the alliance), the request tables
(trainings / buildings / events-missions) and the MemberSync links.

Lookups work across BOTH identities: a Discord id (via the links table)
or any MissionChief name/id — including members who are not in Discord at
all. Former members resolve too (marked as left), so history stays
reachable after someone leaves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite

from ..db.database import Database
from ..db.repos import LinksRepo

log = logging.getLogger(__name__)

SEARCH_LIMIT = 5


@dataclass
class DossierCandidate:
    mc_user_id: int
    name: str
    discord_id: int | None
    is_active: bool
    score: float


@dataclass
class Dossier:
    mc_user_id: int
    name: str
    role: str | None
    is_active: bool
    left_at: str | None
    member_since: str | None
    first_seen_at: str | None
    earned_credits: int | None
    contribution_rate: float | None
    contributed_daily: int | None
    contributed_monthly: int | None
    discord_id: int | None
    link_status: str | None            # approved | denied | None
    requests: dict = field(default_factory=dict)   # kind -> {count, last_status, last_at}
    missions: dict = field(default_factory=dict)   # kind -> {count, last_status, last_at}


class DossierService:
    def __init__(self, db: Database) -> None:
        self._db = db
        self.links = LinksRepo(db)

    # -- search ------------------------------------------------------------

    async def search(self, query: str) -> list[DossierCandidate]:
        """Rank roster members against a query: exact MC id, exact name,
        substring, in that order. The caller merges Discord-side hits by
        resolving mentions/ids to a link before calling this."""
        query = (query or "").strip()
        if not query:
            return []
        rows: list[tuple[float, aiosqlite.Row]] = []
        if query.isdigit():
            async with self._db.conn.execute(
                "SELECT * FROM members WHERE mc_user_id = ?", (int(query),)
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                rows.append((1.0, row))
        async with self._db.conn.execute(
            "SELECT * FROM members WHERE lower(name) = lower(?) LIMIT 3", (query,)
        ) as cur:
            for row in await cur.fetchall():
                rows.append((1.0, row))
        async with self._db.conn.execute(
            "SELECT * FROM members WHERE name LIKE ? COLLATE NOCASE "
            "ORDER BY is_active DESC, name ASC LIMIT 10",
            (f"%{query}%",),
        ) as cur:
            for row in await cur.fetchall():
                rows.append((0.8, row))

        seen: set[int] = set()
        out: list[DossierCandidate] = []
        for score, row in sorted(rows, key=lambda pair: -pair[0]):
            if row["mc_user_id"] in seen:
                continue
            seen.add(row["mc_user_id"])
            link = await self.links.get_by_mc(row["mc_user_id"])
            out.append(DossierCandidate(
                mc_user_id=row["mc_user_id"],
                name=row["name"],
                discord_id=(
                    link["discord_id"]
                    if link is not None and link["status"] == "approved" else None
                ),
                is_active=bool(row["is_active"]),
                score=score,
            ))
            if len(out) >= SEARCH_LIMIT:
                break
        return out

    async def resolve_discord(self, discord_id: int) -> int | None:
        """MC id linked to a Discord account, if verified."""
        link = await self.links.get_by_discord(discord_id)
        if link is not None and link["status"] == "approved":
            return link["mc_user_id"]
        return None

    # -- the dossier itself --------------------------------------------------

    async def build(self, mc_user_id: int) -> Dossier | None:
        async with self._db.conn.execute(
            "SELECT * FROM members WHERE mc_user_id = ?", (mc_user_id,)
        ) as cur:
            member = await cur.fetchone()
        if member is None:
            return None

        link = await self.links.get_by_mc(mc_user_id)
        dossier = Dossier(
            mc_user_id=mc_user_id,
            name=member["name"],
            role=member["role"],
            is_active=bool(member["is_active"]),
            left_at=member["left_at"],
            member_since=member["raw_member_since"],
            first_seen_at=member["first_seen_at"],
            earned_credits=member["earned_credits"],
            contribution_rate=member["contribution_rate"],
            contributed_daily=await self._income(mc_user_id, "daily"),
            contributed_monthly=await self._income(mc_user_id, "monthly"),
            discord_id=link["discord_id"] if link is not None else None,
            link_status=link["status"] if link is not None else None,
        )
        dossier.requests = await self._request_summary(
            "automation_requests", "kind", member["name"], mc_user_id
        )
        dossier.missions = await self._request_summary(
            "scheduled_missions", "kind", member["name"], mc_user_id
        )
        return dossier

    async def _income(self, mc_user_id: int, period: str) -> int | None:
        """The member's latest treasury-income snapshot amount (their
        contribution to the alliance funds this day/month)."""
        try:
            async with self._db.conn.execute(
                "SELECT amount FROM income_snapshots "
                "WHERE period = ? AND mc_user_id = ? "
                "ORDER BY period_key DESC, taken_at DESC LIMIT 1",
                (period, mc_user_id),
            ) as cur:
                row = await cur.fetchone()
            return row["amount"] if row is not None else None
        except aiosqlite.Error:
            return None

    async def _request_summary(
        self, table: str, kind_col: str, name: str, mc_user_id: int
    ) -> dict:
        """Per-kind counts + most recent status for one requester, matched
        on MC id when recorded, else on the requester name."""
        summary: dict = {}
        try:
            async with self._db.conn.execute(
                f"SELECT {kind_col} AS kind, COUNT(*) AS n, MAX(created_at) AS last_at "
                f"FROM {table} WHERE requester_mc_id = ? OR requester_name = ? "
                f"GROUP BY {kind_col}",
                (mc_user_id, name),
            ) as cur:
                for row in await cur.fetchall():
                    summary[row["kind"]] = {
                        "count": row["n"], "last_at": row["last_at"],
                    }
            async with self._db.conn.execute(
                f"SELECT {kind_col} AS kind, status FROM {table} "
                "WHERE requester_mc_id = ? OR requester_name = ? "
                "ORDER BY id DESC LIMIT 25",
                (mc_user_id, name),
            ) as cur:
                for row in await cur.fetchall():
                    entry = summary.get(row["kind"])
                    if entry is not None and "last_status" not in entry:
                        entry["last_status"] = row["status"]
        except aiosqlite.Error as exc:
            log.warning("dossier: request summary from %s failed: %s", table, exc)
        return summary
