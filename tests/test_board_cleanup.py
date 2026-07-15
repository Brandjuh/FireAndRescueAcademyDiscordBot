"""The 12h board tidy-up: the deletions repo, the sweep, and the terminal
populate hooks (mission + training)."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import BoardDeletionRepo
from fra_bot.services.board_cleanup import BoardCleanupService, deletion_due_at

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "cleanup.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _iso(**delta):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(**delta)).isoformat()


# -- repo -------------------------------------------------------------------

async def test_schedule_is_idempotent_and_keeps_earlier_due(db):
    repo = BoardDeletionRepo(db)
    await repo.schedule(15305, 500, due_at=_iso(hours=12), reason="first")
    # A repeat with a LATER due keeps the earlier one; reason is preserved.
    await repo.schedule(15305, 500, due_at=_iso(hours=48), reason="second")
    assert await repo.pending_count() == 1
    # Nothing is due yet (both in the future).
    assert await repo.due() == []


async def test_due_returns_only_arrived_soonest_first(db):
    repo = BoardDeletionRepo(db)
    await repo.schedule(1, 10, due_at=_iso(hours=-1))     # overdue
    await repo.schedule(1, 11, due_at=_iso(hours=-3))     # more overdue
    await repo.schedule(1, 12, due_at=_iso(hours=+5))     # not yet
    due = await repo.due()
    assert [r["post_id"] for r in due] == [11, 10]        # soonest-due first


async def test_bump_pushes_due_out_and_counts_attempt(db):
    repo = BoardDeletionRepo(db)
    await repo.schedule(1, 10, due_at=_iso(hours=-1))
    row = (await repo.due())[0]
    await repo.bump(row["id"], backoff_seconds=3600, error="HTTP 500")
    assert await repo.due() == []                         # pushed into the future
    async with db.conn.execute(
        "SELECT attempts, last_error FROM board_pending_deletions WHERE id=?",
        (row["id"],),
    ) as cur:
        after = await cur.fetchone()
    assert after["attempts"] == 1 and after["last_error"] == "HTTP 500"


# -- sweep ------------------------------------------------------------------

class _FakeBoard:
    def __init__(self, *, results=None):
        # results maps post_id -> bool (delete success); default all succeed.
        self._results = results or {}
        self.deleted: list[tuple[int, int]] = []

    async def delete_post(self, thread_id, post_id):
        self.deleted.append((int(thread_id), int(post_id)))
        return self._results.get(int(post_id), True)


def _cleanup(db, *, dry_run, board):
    svc = BoardCleanupService.__new__(BoardCleanupService)
    from fra_bot.db.repos import RunsRepo
    svc.cfg = SimpleNamespace(automation=SimpleNamespace(dry_run=dry_run))
    svc.board = board
    svc.deletions = BoardDeletionRepo(db)
    svc.runs = RunsRepo(db)
    return svc


async def test_sweep_is_noop_in_dry_run(db):
    board = _FakeBoard()
    svc = _cleanup(db, dry_run=True, board=board)
    await svc.deletions.schedule(1, 10, due_at=_iso(hours=-1))
    assert await svc.sweep() == 0
    assert board.deleted == []                            # never touched the board
    assert await svc.deletions.pending_count() == 1       # still queued


async def test_sweep_deletes_due_and_removes_rows(db):
    board = _FakeBoard()
    svc = _cleanup(db, dry_run=False, board=board)
    await svc.deletions.schedule(15305, 10, due_at=_iso(hours=-1))
    await svc.deletions.schedule(15305, 11, due_at=_iso(hours=+5))  # not due
    deleted = await svc.sweep()
    assert deleted == 1
    assert board.deleted == [(15305, 10)]
    assert await svc.deletions.pending_count() == 1        # only the future one left


async def test_sweep_backs_off_on_failure_then_drops(db):
    board = _FakeBoard(results={10: False})                # always fails
    svc = _cleanup(db, dry_run=False, board=board)
    await svc.deletions.schedule(1, 10, due_at=_iso(hours=-1))
    # Force it due each round and sweep MAX_ATTEMPTS times.
    for _ in range(BoardDeletionRepo.MAX_ATTEMPTS):
        await db.execute(
            "UPDATE board_pending_deletions SET due_at=? WHERE post_id=10",
            (_iso(hours=-1),),
        )
        await svc.sweep()
    assert await svc.deletions.pending_count() == 0        # dropped after the cap
    assert len(board.deleted) == BoardDeletionRepo.MAX_ATTEMPTS


# -- terminal populate: missions -------------------------------------------

async def test_mission_terminal_schedules_cleanup_live_only(db):
    from tests.test_missions import FakeClient, FakeGeo, _cfg
    from fra_bot.services.missions import MissionScheduler

    sched = MissionScheduler(_cfg(dry_run=False), FakeClient(), db, FakeGeo())
    mid = await sched.missions.create_from_board(
        15307, 900, {"kind": "large", "mission_source": "preset", "location_text": "NYC"},
        requester_name="Bob", requester_mc_id=7,
    )
    await sched.missions.set_status(mid, "done", "started")
    await sched._schedule_cleanup(mid)
    assert await sched.deletions.pending_count() == 1
    due = await sched.deletions.due()  # due_at is 12h out, so nothing yet
    assert due == []

    # In dry-run the same terminal state schedules nothing.
    sched_dry = MissionScheduler(_cfg(dry_run=True), FakeClient(), db, FakeGeo())
    mid2 = await sched_dry.missions.create_from_board(
        15307, 901, {"kind": "large", "mission_source": "preset", "location_text": "LA"},
        requester_name="Ann", requester_mc_id=8,
    )
    await sched_dry.missions.set_status(mid2, "done", "started")
    await sched_dry._schedule_cleanup(mid2)
    assert await sched_dry.deletions.pending_count() == 1   # unchanged from before


async def test_mission_discord_source_never_scheduled(db):
    from tests.test_missions import FakeClient, FakeGeo, _cfg
    from fra_bot.services.missions import MissionScheduler

    sched = MissionScheduler(_cfg(dry_run=False), FakeClient(), db, FakeGeo())
    mid = await sched.missions.create(
        source="discord", kind="large", mission_source="preset", location_text="NYC",
    )
    await sched.missions.set_status(mid, "done", "started")
    await sched._schedule_cleanup(mid)
    assert await sched.deletions.pending_count() == 0       # no board post to clean


# -- terminal populate: trainings ------------------------------------------

async def test_training_terminal_schedules_cleanup_live(db):
    from tests.test_trainings_flow import _service

    svc, _ = _service(db, dry_run=False)
    rid = await svc.requests.create(
        kind="training", thread_id=5935, post_id=321,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({"trainings": [], "ambiguous": []}),
    )
    await svc.requests.set_status(rid, "done", "opened")
    await svc._schedule_cleanup(rid)
    assert await svc.deletions.pending_count() == 1


# -- the bot's OWN reply gets the 12h tidy-up too ---------------------------

class _ReplyBoard:
    """Fake board: posting succeeds and the just-posted reply is found."""

    def __init__(self, *, found=777, fail_lookup=False):
        self.found = found
        self.fail_lookup = fail_lookup
        self.lookups = []
        self.replies = []

    async def post_reply(self, thread_id, content):
        self.replies.append((int(thread_id), content))
        return True

    async def find_bot_post(self, thread_id, marker, *, max_pages=None):
        if self.fail_lookup:
            from fra_bot.mc.errors import MissionChiefError
            raise MissionChiefError("boom")
        self.lookups.append((int(thread_id), marker, max_pages))
        return self.found


async def test_schedule_reply_cleanup_queues_own_post(db):
    from fra_bot.services.board_cleanup import schedule_reply_cleanup

    board = _ReplyBoard(found=777)
    repo = BoardDeletionRepo(db)
    await schedule_reply_cleanup(
        board, repo, 5935, "Training request processed for Alice.\nOpened: ...",
        kind="training", dry_run=False,
    )
    assert board.lookups == [(5935, "Training request processed for Alice.", 1)]
    async with db.conn.execute(
        "SELECT thread_id, post_id, reason FROM board_pending_deletions"
    ) as cur:
        rows = await cur.fetchall()
    assert [(r["thread_id"], r["post_id"], r["reason"]) for r in rows] == [
        (5935, 777, "bot training reply")
    ]
    assert await repo.due() == []  # 12h out, not due yet


async def test_schedule_reply_cleanup_dry_run_and_not_found(db):
    from fra_bot.services.board_cleanup import schedule_reply_cleanup

    repo = BoardDeletionRepo(db)
    await schedule_reply_cleanup(          # dry-run: never scheduled
        _ReplyBoard(), repo, 5935, "text", kind="training", dry_run=True,
    )
    await schedule_reply_cleanup(          # not found: best-effort skip
        _ReplyBoard(found=None), repo, 5935, "text", kind="training", dry_run=False,
    )
    await schedule_reply_cleanup(          # lookup error: swallowed
        _ReplyBoard(fail_lookup=True), repo, 5935, "text", kind="training", dry_run=False,
    )
    assert await repo.pending_count() == 0


async def test_training_reply_schedules_own_cleanup(db):
    from tests.test_trainings_flow import _service

    svc, _ = _service(db, dry_run=False)
    svc.cfg.automation.reply_to_board = True
    svc.board = _ReplyBoard(found=888)
    await svc.reply("Training request processed for Alice.")
    assert svc.board.replies                          # reply was posted
    async with db.conn.execute(
        "SELECT post_id, reason FROM board_pending_deletions"
    ) as cur:
        rows = await cur.fetchall()
    assert [(r["post_id"], r["reason"]) for r in rows] == [(888, "bot training reply")]
