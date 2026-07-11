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

import hashlib
import json
import logging

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..db.repos import (
    AutomationRepo,
    BoardDeletionRepo,
    BoardRepo,
    MembersRepo,
    RunsRepo,
    StateRepo,
)
from ..mc.board import (
    REPLY_MARKER,
    BoardClient,
    ensure_guide_post,
    guide_now,
    guide_updated_line,
)
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost
from .board_cleanup import deletion_due_at

log = logging.getLogger(__name__)

# Give up on a request after this many transient-error retries. Condition
# waits (funds/cooldown) don't bump ``attempts``, so they wait as long as
# needed and are not affected by this cap.
MAX_ATTEMPTS = 12

# A request is done with the board once it reaches one of these.
_TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})


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
        self.state = StateRepo(db)
        self.deletions = BoardDeletionRepo(db)

    # -- to be provided by subclasses -----------------------------------

    @property
    def thread_id(self) -> int:
        raise NotImplementedError

    #: First line marker of this service's how-to-request guide post. Leave
    #: ``None`` to disable guide maintenance for the service.
    guide_marker: str | None = None

    def guide_body(self) -> str | None:
        """The STABLE how-to-request guide text (first line starts with
        :attr:`guide_marker`). Return ``None`` to skip posting a guide. The
        base appends a "last updated" line; subclasses that want live sections
        (e.g. availability) override :meth:`guide_content` instead."""
        return None

    async def guide_content(self, now_epoch: float) -> str:
        """The FULL guide post: stable body plus volatile bits (timestamp,
        availability, …). May be expensive — it is only invoked once the
        throttle in :func:`ensure_guide_post` has decided a write will happen,
        never on the quiet polls in between. The refresh throttle keys off the
        stable ``guide_body`` alone."""
        return f"{self.guide_body()}\n\n{guide_updated_line(now_epoch)}"

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

    def _guide_id_key(self) -> str:
        return f"board_guide_id:{self.kind}:{self.thread_id}"

    def _guide_hash_key(self) -> str:
        return f"board_guide_hash:{self.kind}:{self.thread_id}"

    def _guide_refreshed_key(self) -> str:
        return f"board_guide_refreshed:{self.kind}:{self.thread_id}"

    async def _ensure_guide(self) -> None:
        """Maintain this board's how-to-request guide (find-or-edit, never
        duplicate). Gated only by ``reply_to_board`` — a guide is an
        informational forum post, so it's kept current even in dry-run. The
        board is only re-written when the instructions change (or hourly, to
        freshen the "last updated" line and any live sections)."""
        if not self.cfg.automation.reply_to_board or not self.guide_marker:
            return
        body = self.guide_body()
        if not body:
            return
        # The throttle keys off the cheap stable text; the full post (which
        # may need many rate-limited fetches, e.g. classroom availability) is
        # only built lazily when a write is actually going to happen.
        signature = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        now = guide_now()
        try:
            await ensure_guide_post(
                self.board, self.state, self.thread_id,
                id_key=self._guide_id_key(), hash_key=self._guide_hash_key(),
                refreshed_key=self._guide_refreshed_key(),
                marker=self.guide_marker,
                desired=lambda: self.guide_content(now),
                signature=signature, now_epoch=now,
            )
        except MissionChiefError as exc:
            log.warning(
                "%s: could not maintain guide on %s: %s",
                self.kind, self.thread_id, exc,
            )

    async def force_guide(self, *, repost: bool = False) -> str:
        """Sync this board's guide RIGHT NOW (bypassing the hourly throttle)
        and report what happened — for the ``!fra guides`` command.

        ``repost`` deletes the existing guide first and creates a fresh one,
        so a guide buried under newer posts lands back at the bottom of the
        thread where members actually see it."""
        label = f"{self.kind} (thread {self.thread_id})"
        if not self.guide_marker or not self.guide_body():
            return f"➖ {label}: no guide defined"
        if not self.cfg.automation.reply_to_board:
            return f"➖ {label}: reply_to_board is off"
        try:
            if repost:
                stored = await self.state.get(self._guide_id_key())
                target = int(stored) if stored else await self.board.find_bot_post(
                    self.thread_id, self.guide_marker
                )
                if target:
                    await self.board.delete_post(self.thread_id, int(target))
                await self.state.delete(self._guide_id_key())
            # Clear the throttle + signature so _ensure_guide writes now.
            await self.state.delete(self._guide_hash_key())
            await self.state.delete(self._guide_refreshed_key())
            await self._ensure_guide()
        except MissionChiefError as exc:
            return f"❌ {label}: {exc}"
        post_id = await self.state.get(self._guide_id_key())
        if post_id:
            url = self.client.url(f"/alliance_threads/{self.thread_id}")
            return f"✅ {label}: guide is post #{post_id} — {url}"
        reason = getattr(self.board, "last_error", None) or "see the log"
        return f"❌ {label}: could not create or edit the guide — {reason}"

    async def poll(self) -> None:
        run_id = await self.runs.start(f"board_{self.kind}")
        try:
            # Member requests come FIRST: scan for new posts, run the queue,
            # and only then do guide upkeep — a guide refresh can be slow
            # (e.g. the trainings availability walk) and must never make a
            # member wait for their class/building/mission.
            #
            # The board scan must NEVER starve the queue either: a broken or
            # unreachable thread would otherwise abort the poll before
            # _execute_ready(), leaving Discord-sourced (and previously
            # detected) requests pending forever. Scan errors are recorded,
            # the queue still runs.
            detected = 0
            fresh_count = 0
            scan_error: str | None = None
            try:
                detected, fresh_count = await self._scan_board_posts()
            except MissionChiefError as exc:
                scan_error = str(exc)
                log.warning(
                    "%s: board scan of thread %s failed (%s) — still "
                    "executing the queue", self.kind, self.thread_id, exc,
                )

            executed = await self._execute_ready()
            await self._ensure_guide()

            await self.runs.finish(
                run_id,
                status="success" if scan_error is None else "partial",
                pages=1, rows_parsed=fresh_count, rows_new=detected + executed,
                message=scan_error,
            )
        except MissionChiefError as exc:
            await self.runs.finish(run_id, status="failed", message=str(exc))
            raise

    async def _scan_board_posts(self) -> tuple[int, int]:
        """Fetch + record new board posts. Returns (detected, fresh_count)."""
        last_seen = await self.board_repo.last_seen_post_id(self.thread_id)
        # First contact records history without acting on it. An EMPTY
        # thread records nothing, so a bare `last_seen is None` would stay
        # baseline forever and silently swallow the first real request —
        # remember explicitly that the baseline pass has happened.
        baseline_key = f"board_baseline_done:{self.kind}:{self.thread_id}"
        baseline = (
            last_seen is None
            and await self.state.get(baseline_key) is None
        )
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
            await self.state.set(baseline_key, "1")
            log.info(
                "%s: thread %s baseline set (%d posts recorded, none processed)",
                self.kind, self.thread_id, len(fresh_posts),
            )
        return detected, len(fresh_posts)

    async def execute_queue_now(self) -> int:
        """Run the claimable request queue RIGHT NOW — no board scan, no
        guide upkeep. The Discord intake kicks this the moment a member's
        request is created, so it never waits for the next scheduled pass.
        Callers hold the job's shared lock to stay out of the poll's way."""
        return await self._execute_ready()

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
                    await self._schedule_cleanup(request["id"])
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
            await self._schedule_cleanup(request["id"])
        return executed

    async def _schedule_cleanup(self, request_id: int) -> None:
        """Queue a handled request's original post for the 12h board tidy-up
        once it reaches a terminal state — live mode only."""
        if self.dry_run:
            return
        request = await self.requests.get(request_id)
        if request is None or request["status"] not in _TERMINAL_STATUSES:
            return
        if not request["post_id"]:
            return
        await self.deletions.schedule(
            int(request["thread_id"] or self.thread_id), int(request["post_id"]),
            due_at=deletion_due_at(), reason=f"handled {self.kind} request",
        )

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
        """Post a board reply (when ``reply_to_board`` is on).

        Replies post in dry-run too: they're informational forum posts on
        OUR dedicated request topics, not game actions — members need the
        feedback ("got it", "couldn't use that link") regardless of whether
        real starts are enabled. Action messages carry their own [dry-run]
        markers where relevant."""
        if not self.cfg.automation.reply_to_board:
            return
        try:
            await self.board.post_reply(self.thread_id, content)
        except MissionChiefError as exc:
            log.warning("%s: board reply failed: %s", self.kind, exc)

    @staticmethod
    def is_discord_request(request: aiosqlite.Row) -> bool:
        """Discord-sourced requests carry thread_id 0 (no board post). Their
        feedback goes through the Discord publisher, never to the board."""
        return not request["thread_id"]

    async def reply_for(self, request: aiosqlite.Row, content: str) -> None:
        """Board reply for a BOARD request; silent for a Discord request."""
        if self.is_discord_request(request):
            return
        await self.reply(content)

    async def contribution_rate(self, mc_user_id: int | None) -> float | None:
        """Requester's alliance contribution rate from the roster."""
        if mc_user_id is None:
            return None
        active = await self.members.active_members()
        row = active.get(mc_user_id)
        return row["contribution_rate"] if row is not None else None
