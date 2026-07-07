"""Shared machinery for board-driven request automation.

Each concrete service (trainings, buildings, events) polls one alliance
board thread. This base class owns the safe parts:

* baseline on first contact with a thread — history is stored but
  never processed, so enabling automation can't replay old requests,
* dedup through the ``board_posts`` table (survives restarts),
* skipping our own posts and ``[FRA]``-marked bot posts,
* retrying requests that are ``waiting`` (funds, cooldowns).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..db.repos import AutomationRepo, BoardRepo, MembersRepo, RunsRepo
from ..mc.board import REPLY_MARKER, BoardClient
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost

log = logging.getLogger(__name__)


class BoardRequestService:
    kind: str = ""

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self.board = BoardClient(client)
        self.board_repo = BoardRepo(db)
        self.requests = AutomationRepo(db)
        self.members = MembersRepo(db)
        self.runs = RunsRepo(db)

    # -- to be provided by subclasses -----------------------------------

    @property
    def thread_id(self) -> int:
        raise NotImplementedError

    async def handle_post(self, post: BoardPost) -> None:
        """Process one new member post (create + execute a request)."""
        raise NotImplementedError

    async def retry_waiting(self, request: aiosqlite.Row) -> None:
        """Retry one 'waiting' request. Default: nothing."""

    # -- shared plumbing -------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    async def poll(self) -> None:
        run_id = await self.runs.start(f"board_{self.kind}")
        try:
            last_seen = await self.board_repo.last_seen_post_id(self.thread_id)
            baseline = last_seen is None
            page, fresh_posts = await self.board.fetch_new_posts(
                self.thread_id, last_seen
            )
            new_ids = set(
                await self.board_repo.record_posts(
                    self.thread_id,
                    [
                        {
                            "post_id": p.post_id,
                            "author_name": p.author_name,
                            "author_mc_id": p.author_mc_id,
                            "raw_timestamp": p.raw_timestamp,
                            "content": p.content,
                        }
                        for p in fresh_posts
                    ],
                )
            )

            handled = 0
            if baseline:
                log.info(
                    "%s: thread %s baseline set (%d posts recorded, none processed)",
                    self.kind, self.thread_id, len(new_ids),
                )
            else:
                for post in sorted(fresh_posts, key=lambda p: p.post_id):
                    if post.post_id not in new_ids:
                        continue  # already recorded/processed on an earlier poll
                    if (
                        page.current_user_id is not None
                        and post.author_mc_id == page.current_user_id
                    ):
                        continue
                    if post.content.startswith(REPLY_MARKER):
                        continue
                    handled += 1
                    try:
                        await self.handle_post(post)
                    except MissionChiefError:
                        raise
                    except Exception:
                        log.exception(
                            "%s: unexpected error handling post %s",
                            self.kind, post.post_id,
                        )

            for request in await self.requests.waiting_requests(self.kind):
                try:
                    await self.retry_waiting(request)
                except MissionChiefError:
                    raise
                except Exception:
                    log.exception(
                        "%s: unexpected error retrying request %s",
                        self.kind, request["id"],
                    )

            await self.runs.finish(
                run_id, status="success", pages=1,
                rows_parsed=len(fresh_posts), rows_new=handled,
            )
        except MissionChiefError as exc:
            await self.runs.finish(run_id, status="failed", message=str(exc))
            raise

    async def create_request(
        self, post: BoardPost, payload: dict[str, Any] | None = None
    ) -> int:
        return await self.requests.create(
            kind=self.kind,
            thread_id=self.thread_id,
            post_id=post.post_id,
            requester_name=post.author_name,
            requester_mc_id=post.author_mc_id,
            payload=json.dumps(payload) if payload else None,
        )

    async def reply(self, content: str) -> None:
        """Post a board reply (skipped in dry-run / when disabled)."""
        if not self.cfg.automation.reply_to_board:
            return
        if self.dry_run:
            log.info("%s DRY-RUN board reply:\n%s", self.kind, content)
            return
        try:
            await self.board.post_reply(self.thread_id, content)
        except MissionChiefError as exc:
            log.warning("%s: board reply failed: %s", self.kind, exc)

    async def contribution_rate(self, mc_user_id: int | None) -> float | None:
        """Requester's alliance contribution rate from the roster."""
        if mc_user_id is None:
            return None
        active = await self.members.active_members()
        row = active.get(mc_user_id)
        return row["contribution_rate"] if row is not None else None
