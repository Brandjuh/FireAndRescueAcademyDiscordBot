"""H1 regression: the board-request state machine must never strand a
member's request, and must not double-execute non-idempotent actions.

These drive the shared BoardRequestService with a tiny in-memory fake
service so the detect/execute/claim/sweep flow is exercised end-to-end
against a real database — no network.
"""

import json

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo, BoardRepo
from fra_bot.mc.parsers.board import BoardPost

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "sm.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _post(pid, content="event: Kansas City", author=42):
    return BoardPost(
        post_id=pid, author_name="Alice", author_mc_id=author,
        raw_timestamp="Mon, 06 Jul 2026 14:00", content=content,
    )


async def test_record_post_and_request_is_atomic(db):
    repo = AutomationRepo(db)
    post = {"post_id": 1, "author_name": "A", "author_mc_id": 9,
            "raw_timestamp": "t", "content": "c"}
    new, rid = await repo.record_post_and_request(
        555, post, "event",
        {"payload": json.dumps({"x": 1}), "requester_name": "A", "requester_mc_id": 9},
    )
    assert new is True and rid is not None

    # The post is now seen AND the request exists together.
    assert await BoardRepo(db).last_seen_post_id(555) == 1
    row = await repo.get(rid)
    assert row["status"] == "pending"
    assert row["kind"] == "event"


async def test_already_seen_post_creates_no_duplicate_request(db):
    repo = AutomationRepo(db)
    post = {"post_id": 1, "author_name": "A", "author_mc_id": 9,
            "raw_timestamp": "t", "content": "c"}
    req = {"payload": "{}", "requester_name": "A", "requester_mc_id": 9}
    await repo.record_post_and_request(555, post, "event", req)
    new, rid = await repo.record_post_and_request(555, post, "event", req)
    assert new is False and rid is None  # second poll doesn't re-create


async def test_non_request_post_records_seen_only(db):
    repo = AutomationRepo(db)
    post = {"post_id": 7, "author_name": "A", "author_mc_id": 9,
            "raw_timestamp": "t", "content": "thanks!"}
    new, rid = await repo.record_post_and_request(555, post, "event", None)
    assert new is True and rid is None
    assert await BoardRepo(db).last_seen_post_id(555) == 7


async def test_claim_is_exclusive(db):
    repo = AutomationRepo(db)
    rid = await repo.create(
        kind="event", thread_id=1, post_id=1,
        requester_name="A", requester_mc_id=9, payload="{}",
    )
    assert await repo.claim(rid) is True   # first wins
    assert await repo.claim(rid) is False  # already processing
    row = await repo.get(rid)
    assert row["status"] == "processing"


async def test_sweep_flags_interrupted_processing(db):
    repo = AutomationRepo(db)
    rid = await repo.create(
        kind="event", thread_id=1, post_id=1,
        requester_name="A", requester_mc_id=9, payload="{}",
    )
    await repo.claim(rid)  # now 'processing'
    swept = await repo.sweep_processing()
    assert swept == 1
    row = await repo.get(rid)
    assert row["status"] == "failed"
    assert "verify" in row["status_detail"].lower()
    assert row["posted_at"] is None  # will be announced


async def test_sweep_stale_processing_only_touches_old(db):
    import datetime as dt

    repo = AutomationRepo(db)
    rid = await repo.create(
        kind="event", thread_id=1, post_id=2,
        requester_name="A", requester_mc_id=9, payload="{}",
    )
    await repo.claim(rid)  # 'processing', updated_at = now
    # A cutoff in the past must NOT disturb a just-claimed (in-flight) row.
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
    assert await repo.sweep_stale_processing(past) == 0
    assert (await repo.get(rid))["status"] == "processing"
    # A cutoff in the future releases it (simulates it being stuck > threshold).
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    assert await repo.sweep_stale_processing(future) == 1
    row = await repo.get(rid)
    assert row["status"] == "failed" and "stale" in row["status_detail"]
    assert row["posted_at"] is None


class _CapService:
    """Just enough BoardRequestService to exercise _execute_ready's cap."""

    kind = "event"

    def __init__(self, db):
        from types import SimpleNamespace

        from fra_bot.db.repos import BoardDeletionRepo
        from fra_bot.services.board_requests import BoardRequestService
        self._svc = BoardRequestService.__new__(BoardRequestService)
        self._svc.kind = "event"
        self._svc.requests = AutomationRepo(db)
        self._svc.deletions = BoardDeletionRepo(db)
        # dry-run: the terminal-cleanup hook is a no-op, keeping this focused
        # on the attempts cap.
        self._svc.cfg = SimpleNamespace(automation=SimpleNamespace(dry_run=True))
        self.executed = 0

        async def execute(request, *, announce):
            self.executed += 1
        self._svc.execute_request = execute

    async def run(self):
        return await self._svc._execute_ready()


async def test_max_attempts_cap_gives_up(db):
    from fra_bot.services.board_requests import MAX_ATTEMPTS

    repo = AutomationRepo(db)
    rid = await repo.create(kind="event", thread_id=1, post_id=1,
                            requester_name="A", requester_mc_id=9, payload="{}")
    # Simulate a request that has already failed MAX_ATTEMPTS times.
    await db.execute(
        "UPDATE automation_requests SET status='waiting', attempts=? WHERE id=?",
        (MAX_ATTEMPTS, rid),
    )
    cap = _CapService(db)
    await cap.run()
    assert cap.executed == 0  # never executed — capped
    row = await repo.get(rid)
    assert row["status"] == "failed"
    assert "gave up" in row["status_detail"]


async def test_claimable_includes_pending_and_due_waiting(db):
    repo = AutomationRepo(db)
    p = await repo.create(kind="event", thread_id=1, post_id=1,
                          requester_name="A", requester_mc_id=9, payload="{}")
    w = await repo.create(kind="event", thread_id=1, post_id=2,
                          requester_name="B", requester_mc_id=10, payload="{}")
    await repo.set_status(w, "waiting", "held", next_attempt_at="2000-01-01T00:00:00+00:00")
    # A far-future waiting one must NOT be claimable yet.
    future = await repo.create(kind="event", thread_id=1, post_id=3,
                               requester_name="C", requester_mc_id=11, payload="{}")
    await repo.set_status(future, "waiting", "later",
                          next_attempt_at="2999-01-01T00:00:00+00:00")

    claimable_ids = {r["id"] for r in await repo.claimable("event")}
    assert p in claimable_ids       # pending
    assert w in claimable_ids       # due waiting
    assert future not in claimable_ids  # not yet due
