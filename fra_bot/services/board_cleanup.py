"""The 12-hour board tidy-up.

When a board request has been handled, its original post is scheduled for
deletion after a grace period so the request boards don't fill up with
stale posts. This module owns the periodic sweep that carries those
deletions out, plus the small helper the mission/training services use to
schedule one.

Deletion is a destructive, board-facing action, so it is **live-only**: it
never runs in dry-run, where the other bot still owns the board. Scheduling
happens only in live mode too (see the services), so in dry-run the table
stays empty and the sweep is a no-op.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import Config
from ..db.database import Database
from ..db.repos import BoardDeletionRepo, RunsRepo
from ..mc.board import BoardClient
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError

log = logging.getLogger(__name__)

# How long a handled request post lingers before it's removed.
GRACE_SECONDS = 12 * 60 * 60
# Failed deletes back off linearly, capped so a stuck post retries hourly.
_RETRY_STEP_SECONDS = 10 * 60
_RETRY_CAP_SECONDS = 60 * 60


def deletion_due_at() -> str:
    """ISO timestamp GRACE_SECONDS from now — when a handled post may go."""
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=GRACE_SECONDS)
    ).isoformat()


class BoardCleanupService:
    """Periodically deletes board request posts whose grace period has passed."""

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.board = BoardClient(client)
        self.deletions = BoardDeletionRepo(db)
        self.runs = RunsRepo(db)

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    async def sweep(self) -> int:
        """Delete every post whose deletion time has arrived. Returns the
        number removed. A no-op in dry-run (nothing is ever scheduled there,
        and we refuse to touch the board destructively while the other bot
        owns it)."""
        if self.dry_run:
            return 0
        due = await self.deletions.due()
        if not due:
            return 0

        run_id = await self.runs.start("board_cleanup")
        deleted = 0
        try:
            for row in due:
                thread_id, post_id = row["thread_id"], row["post_id"]
                error: str | None = None
                ok = False
                try:
                    ok = await self.board.delete_post(int(thread_id), int(post_id))
                except MissionChiefError as exc:
                    error = str(exc)
                if ok:
                    await self.deletions.remove(row["id"])
                    deleted += 1
                    continue
                attempts = row["attempts"] + 1
                if attempts >= BoardDeletionRepo.MAX_ATTEMPTS:
                    log.warning(
                        "board cleanup: giving up on post %s (thread %s) after "
                        "%d attempts: %s",
                        post_id, thread_id, attempts, error or "delete rejected",
                    )
                    await self.deletions.remove(row["id"])
                else:
                    backoff = min(_RETRY_CAP_SECONDS, _RETRY_STEP_SECONDS * attempts)
                    await self.deletions.bump(
                        row["id"], backoff_seconds=backoff,
                        error=error or "delete rejected",
                    )
            await self.runs.finish(
                run_id, status="success", rows_parsed=len(due), rows_new=deleted
            )
        except MissionChiefError as exc:
            await self.runs.finish(run_id, status="failed", message=str(exc))
            raise
        return deleted
