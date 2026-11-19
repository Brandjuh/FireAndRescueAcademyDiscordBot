"""Building queue: strict first-in-first-out, the public queue post, and the
12h tidy-up of every member post on the building request thread."""

import json

from fra_bot.db.repos import AutomationRepo
from fra_bot.mc.parsers.board import BoardPost, BoardThreadPage
from fra_bot.services.buildings import building_queue_body
from tests.test_building_rules import (  # noqa: F401 — fixture
    _cached_payload,
    _live_service,
    db,
)

async def test_newer_request_never_overtakes_an_older_one(db, monkeypatch):
    # Funds are plentiful, but the older request sits in an error backoff:
    # the newer one must wait its turn instead of spending the money.
    svc, browser = _live_service(db, funds=99_000_000)
    monkeypatch.setattr(browser, "available", staticmethod(lambda: True))
    built = []

    async def _build(**kw):
        built.append(kw)
        raise AssertionError("must not build")

    svc._builder.build = _build
    repo = AutomationRepo(db)
    older = await repo.create(kind="building", thread_id=777, post_id=1,
                              requester_name="Ann", requester_mc_id=None,
                              payload=_cached_payload())
    await repo.set_status(older, "waiting", "MissionChief error; retrying",
                          next_attempt_at="2999-01-01T00:00:00+00:00")
    newer = await repo.create(kind="building", thread_id=777, post_id=2,
                              requester_name="Bob", requester_mc_id=None,
                              payload=_cached_payload())

    await svc.execute_request(await repo.get(newer), announce=True)

    row = await repo.get(newer)
    assert row["status"] == "waiting"
    assert "first in, first out" in row["status_detail"]
    assert built == []
    assert "There is 1 building request ahead of yours" in svc.replies[-1]

async def test_fifo_ignores_ungated_built_and_confirming_rows(db):
    repo = AutomationRepo(db)
    ungated = await repo.create(kind="building", thread_id=777, post_id=1,
                                requester_name="A", requester_mc_id=None,
                                payload=json.dumps({"link": "x"}))
    confirming = await repo.create(kind="building", thread_id=777, post_id=2,
                                   requester_name="B", requester_mc_id=None,
                                   payload=_cached_payload())
    await repo.set_status(confirming, "waiting", "submitted", payload=json.dumps(
        {**json.loads(_cached_payload()), "pending_confirm": {"x": 1}}))
    done = await repo.create(kind="building", thread_id=777, post_id=3,
                             requester_name="C", requester_mc_id=None,
                             payload=_cached_payload())
    await repo.set_status(done, "done", "built")
    me = await repo.create(kind="building", thread_id=777, post_id=4,
                           requester_name="D", requester_mc_id=None,
                           payload=_cached_payload())
    assert await repo.fifo_blockers("building", me) == 0
    assert ungated  # still listed in the public queue
    assert [r["id"] for r in await repo.open_queue("building")] == [
        ungated, confirming, me]

async def test_daily_build_yields_to_queued_member_requests(db):
    svc, _ = _live_service(db, funds=99_000_000)
    repo = AutomationRepo(db)
    rid = await repo.create(kind="building", thread_id=777, post_id=1,
                            requester_name="Ann", requester_mc_id=None,
                            payload=_cached_payload())
    await repo.set_status(rid, "waiting", "below floor")
    line = await svc._auto_build_one("hospital", [])
    assert line.startswith("⏳") and "member building request" in line

async def test_queue_post_lists_requests_in_order(db):
    repo = AutomationRepo(db)
    a = await repo.create(kind="building", thread_id=777, post_id=1,
                          requester_name="Ann", requester_mc_id=None,
                          payload=_cached_payload())
    await repo.set_status(a, "waiting", "alliance funds 5 below floor 10; queued")
    b = await repo.create(kind="building", thread_id=777, post_id=2,
                          requester_name="Bob", requester_mc_id=None,
                          payload=json.dumps({"link": "x"}))
    body = building_queue_body(await repo.open_queue("building"))
    assert body.startswith("[FRA] 🏗️ BUILDING QUEUE")
    assert body.index("1. 🏥 Hospital — St Mary Hospital (requested by Ann)") \
        < body.index("2. 🏗️ Building (requested by Bob) — being checked")
    assert "waiting for alliance funds" in body
    assert b
    assert "queue is empty" in building_queue_body([])

def _post(pid, author, content="hello"):
    return BoardPost(post_id=pid, author_name=str(author), author_mc_id=author,
                     raw_timestamp=None, content=content)

async def test_every_member_post_is_scheduled_but_never_our_own(db):
    svc, _ = _live_service(db, funds=0)
    bot_id = 42
    page = BoardThreadPage(
        posts=[
            _post(10, bot_id, "[FRA] 📋 How to request a BUILDING"),
            _post(11, 7, "thanks guys!"),           # chatter
            _post(12, 8, "Hospital: https://maps.app.goo.gl/abc"),
            _post(13, None, "unknown author"),     # can't tell — leave it
            _post(14, 9, "[FRA] old bot notice"),  # not OUR account: goes
        ],
        current_user_id=bot_id,
    )

    class _Board:
        async def fetch_new_posts(self, thread_id, last_seen):
            return page, list(page.posts)

        async def fetch_latest_page(self, thread_id):
            return page

    svc.board = _Board()
    await svc.state.set("board_baseline_done:building:777", "1")
    await svc._scan_board_posts()
    scheduled = {r["post_id"] for r in await _all(db)}
    assert scheduled == {11, 12, 14}

    # The daily walk finds the same posts; idempotent, still not ours.
    await db.execute("DELETE FROM board_pending_deletions")
    assert await svc._tidy_walk() == 3
    assert {r["post_id"] for r in await _all(db)} == {11, 12, 14}
    assert await svc._tidy_walk() == 0  # throttled for a day

async def test_no_member_post_cleanup_in_dry_run(db):
    svc, _ = _live_service(db, funds=0)
    svc.cfg.automation.dry_run = True
    assert await svc._tidy_walk() == 0
    await svc._schedule_member_post(_post(11, 7))
    assert await _all(db) == []

async def _all(db):
    async with db.conn.execute("SELECT * FROM board_pending_deletions") as cur:
        return list(await cur.fetchall())

