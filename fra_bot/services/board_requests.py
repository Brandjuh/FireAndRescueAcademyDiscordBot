"""Shared machinery for board-driven request automation.

Each concrete service (trainings, buildings, events) polls one alliance
board thread. Processing is split into two phases so a crash can never
strand a member's request:

1. **Detect** (``parse_request``, side-effect free): decide whether a
   post is a request of our kind. The post's seen-state and the created
   request row commit *together* in one transaction, so we can never
   mark a post seen and then lose its request.
2. **Execute** (``execute_request``): drive a request to a terminal (or
   ``waiting``) state. Before executing, a request is atomically claimed
   ``pending/waiting -> processing`` so two concurrent polls can't run
   the same non-idempotent MissionChief action twice. A request left
   ``processing`` (crash mid-action) is flagged for manual review at
   startup rather than blindly retried.

The base also handles: baseline on first contact (history recorded but
not executed), dedup via ``board_posts``, and skipping our own posts.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..db.repos import AutomationRepo, BoardRepo, MembersRepo, RunsRepo
from ..mc.board import REPLY_MARKER, BoardClient
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost

log = logging.getLogger(__name__)

# Give up on a request after this many transient-error retries. Condition
# waits (funds/cooldown) don't bump ``attempts``, so they wait as long as
# needed and are not affected by this cap.
MAX_ATTEMPTS = 12


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

    async def parse_request(self, post: BoardPost) -> dict | None:
        """Decide if *post* is a request of our kind. NO side effects.

        Return a dict with keys ``payload`` (a dict), ``requester_name``
        and ``requester_mc_id`` when it is a request, else ``None``.
        """
        raise NotImplementedError

    async def execute_request(self, request: aiosqlite.Row, *, announce: bool) -> None:
        """Drive a claimed request to a terminal or 'waiting' state.

        ``announce`` is True for the first attempt and False for retries,
        so a still-waiting request isn't re-announced every poll.
        """
        raise NotImplementedError

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

            detected = 0
            for post in sorted(fresh_posts, key=lambda p: p.post_id):
                try:
                    # One bad post must not abort the whole poll (which
                    # would skip execution and leave the run unfinished).
                    is_own = (
                        page.current_user_id is not None
                        and post.author_mc_id == page.current_user_id
                    )
                    is_bot_reply = post.content.startswith(REPLY_MARKER)

                    request = None
                    if not baseline and not is_own and not is_bot_reply:
                        request = await self.parse_request(post)

                    # Atomic: mark the post seen and (if it's a request)
                    # create its 'pending' row together.
                    _, request_id = await self.requests.record_post_and_request(
                        self.thread_id,
                        {
                            "post_id": post.post_id,
                            "author_name": post.author_name,
                            "author_mc_id": post.author_mc_id,
                            "raw_timestamp": post.raw_timestamp,
                            "content": post.content,
                        },
                        self.kind,
                        request,
                    )
                    if request_id is not None:
                        detected += 1
                except Exception:
                    log.exception(
                        "%s: error detecting post %s (left unrecorded, will retry)",
                        self.kind, post.post_id,
                    )

            if baseline:
                log.info(
                    "%s: thread %s baseline set (%d posts recorded, none processed)",
                    self.kind, self.thread_id, len(fresh_posts),
                )

            executed = await self._execute_ready()

            await self.runs.finish(
                run_id, status="success", pages=1,
                rows_parsed=len(fresh_posts), rows_new=detected + executed,
            )
        except MissionChiefError as exc:
            await self.runs.finish(run_id, status="failed", message=str(exc))
            raise

    async def _execute_ready(self) -> int:
        """Execute all claimable (pending + due-waiting) requests."""
        executed = 0
        for request in await self.requests.claimable(self.kind):
            # Give up on a request that keeps hitting transient errors, so
            # it can't be re-claimed forever. Legitimate condition-waits
            # (funds/cooldown) don't bump ``attempts``, so this only ends
            # persistently-erroring requests.
            if request["attempts"] >= MAX_ATTEMPTS:
                if await self.requests.claim(request["id"]):
                    await self.requests.set_status(
                        request["id"], "failed",
                        f"gave up after {request['attempts']} failed attempts",
                    )
                continue
            if not await self.requests.claim(request["id"]):
                continue  # another poll won the claim
            first_attempt = request["status"] == "pending"
            executed += 1
            try:
                await self.execute_request(request, announce=first_attempt)
            except MissionChiefError as exc:
                # The action may or may not have landed; put it back to
                # 'waiting' so it retries next poll rather than sitting in
                # 'processing' (which the startup sweep would fail).
                await self.requests.set_status(
                    request["id"], "waiting",
                    f"MissionChief error ({exc}); will retry",
                    bump_attempts=True, announce=False,
                )
            except Exception:
                log.exception(
                    "%s: unexpected error executing request %s",
                    self.kind, request["id"],
                )
                # Only fail it if execute_request didn't already reach a
                # terminal state — never clobber a committed 'done'.
                current = await self.requests.get(request["id"])
                if current is not None and current["status"] == "processing":
                    await self.requests.set_status(
                        request["id"], "failed", "internal error while processing",
                    )
        return executed

    # -- helpers for subclasses -----------------------------------------

    @staticmethod
    def request_data(
        post: BoardPost, payload: dict
    ) -> dict:
        return {
            "payload": json.dumps(payload),
            "requester_name": post.author_name,
            "requester_mc_id": post.author_mc_id,
        }

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
