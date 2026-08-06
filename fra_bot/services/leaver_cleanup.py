"""Requests outlive the member who placed them.

Someone who has left the alliance can no longer be helped by the bot: it
would spend an alliance mission slot — or alliance credits — on a request
from a player who is gone, and a *recurring* request would keep doing that
forever, because the rotation list has no idea its author walked out.

The hourly roster sweep is the only place that learns about a leave, so
the clean-up rides along with it: it runs right after a SUCCESSFUL sync,
against a roster that is fresh by definition.

Deliberately narrow, because this deletes member work:

* only OPEN requests (``pending``/``waiting``). A ``processing`` row is
  inside the start engine right now and cancelling it would race a real
  MissionChief action;
* only requesters the stored roster knows as a FORMER member — positive
  proof. A name that is merely absent from the roster could be a rename,
  a typo, or a stranger posting on the board;
* only while the roster looks healthy (:data:`MIN_SAFE_ROSTER_COUNT`), so
  a half-scraped members page can never empty the queue;
* rotation entries are matched the same way, which is what keeps
  admin-created entries safe: an admin is an ACTIVE member, so their name
  never resolves to a former one.
"""

from __future__ import annotations

import logging

from ..db.database import Database
from ..db.repos import AutomationRepo, MembersRepo, MissionsRepo, RotationRepo
from .membersync import MIN_SAFE_ROSTER_COUNT

log = logging.getLogger(__name__)

_KIND_LABEL = {
    "large": "large scale mission",
    "event": "alliance event",
    "training": "training",
    "building": "building",
}

CANCEL_DETAIL = "requester left the alliance"


class LeaverCleanupService:
    """Drop open requests (and rotation entries) of members who left."""

    def __init__(self, db: Database, bot) -> None:
        self.members = MembersRepo(db)
        self.missions = MissionsRepo(db)
        self.rotation = RotationRepo(db)
        self.requests = AutomationRepo(db)
        self.bot = bot

    async def run(self) -> list[str]:
        """One pass. Returns the report lines (also sent to the admin
        channel); an empty list means there was nothing to clean up."""
        lines = await self.sweep()
        if lines:
            await self.bot.notify_admin(
                "🧹 **Left the alliance — requests removed**\n"
                + "\n".join(lines)[:1800]
            )
        return lines

    # -- the sweep itself, Discord-free so it is testable ----------------

    async def sweep(self) -> list[str]:
        active = await self.members.active_members()
        if len(active) < MIN_SAFE_ROSTER_COUNT:
            log.warning(
                "leaver cleanup skipped — only %d active members in the "
                "roster (safety floor %d); scrape problem?",
                len(active), MIN_SAFE_ROSTER_COUNT,
            )
            return []
        former = await self.members.former_members()
        active_names = {
            (row["name"] or "").strip().lower() for row in active.values()
        }
        active_names.discard("")
        former_names = {
            (row["name"] or "").strip().lower(): mc_id
            for mc_id, row in former.items()
            if (row["name"] or "").strip()
        }

        def left(mc_id, name) -> str | None:
            """The former member's name when this requester is gone."""
            if mc_id is not None:
                mc_id = int(mc_id)
                if mc_id in active:
                    return None
                row = former.get(mc_id)
                return (row["name"] or str(mc_id)) if row is not None else None
            key = (name or "").strip().lower()
            if not key or key in active_names:
                return None
            return name.strip() if key in former_names else None

        lines: list[str] = []
        lines += await self._sweep_missions(left)
        lines += await self._sweep_rotation(left)
        lines += await self._sweep_requests(left)
        return lines

    async def _sweep_missions(self, left) -> list[str]:
        lines = []
        for row in await self.missions.open_all():
            who = left(row["requester_mc_id"], row["requester_name"])
            if who is None:
                continue
            if not await self.missions.cancel(row["id"], CANCEL_DETAIL):
                continue  # it started between the read and the write
            label = _KIND_LABEL.get(row["kind"], row["kind"])
            where = row["address"] or row["location_text"] or "unknown location"
            lines.append(f"• queue #{row['id']} — {label} at {where} ({who})")
        return lines

    async def _sweep_rotation(self, left) -> list[str]:
        lines = []
        for entry in await self.rotation.list_all():
            who = left(None, entry["created_by"])
            if who is None:
                continue
            if not await self.rotation.remove(entry["id"]):
                continue
            label = _KIND_LABEL.get(entry["kind"], entry["kind"])
            where = entry["address"] or entry["location_text"] or "unknown location"
            lines.append(
                f"• rotation #{entry['id']} — recurring {label} at {where} ({who})"
            )
        return lines

    async def _sweep_requests(self, left) -> list[str]:
        lines = []
        for row in await self.requests.open_requests():
            who = left(row["requester_mc_id"], row["requester_name"])
            if who is None:
                continue
            if not await self.requests.cancel(row["id"], CANCEL_DETAIL):
                continue
            label = _KIND_LABEL.get(row["kind"], row["kind"])
            lines.append(f"• {label} request #{row['id']} ({who})")
        return lines
