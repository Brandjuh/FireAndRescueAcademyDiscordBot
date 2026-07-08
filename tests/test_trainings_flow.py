"""Training detect→execute flow (M6) through the new state machine,
driven by a fake client so no network is touched."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo
from fra_bot.mc.parsers.board import BoardPost
from fra_bot.services.trainings import TrainingsService

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, pages, post_result=(200, "", "")):
        self.pages = pages
        self.post_result = post_result
        self.posts = []

    async def start(self):
        pass

    def url(self, path):
        return "https://www.missionchief.com/" + path.lstrip("/")

    async def fetch_page(self, path, *, referer=None):
        return self.pages.get(path, self.pages.get("*", "<html></html>"))

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, data))
        return self.post_result


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "tr.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _cfg(dry_run):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            reply_to_board=False,
            training=SimpleNamespace(
                thread_id=5935, interval=5, min_contribution_rate=5.0,
                preferred_academies={"fire": 4951748},
            ),
        )
    )


ACADEMY_LIST = (
    "<table><tr search_attribute='Fire Academy'>"
    "<td><img building_id='4951748' src='/img/fire.png' alt='Fire'/></td>"
    "<td><a href='/buildings/4951748' class='btn btn-success'>"
    "Start a new training course</a></td></tr></table>"
)
ACADEMY_PAGE = (
    "<form action='/buildings/4951748/education' method='post'>"
    "<input type='hidden' name='authenticity_token' value='tok'/>"
    "<select name='building_rooms_use'><option value='1'>1</option>"
    "<option value='2'>2</option></select>"
    "<select name='alliance[cost]'><option value='0'>Free</option></select>"
    "<select name='education_select'><option value='12'>HazMat</option></select>"
    "<input type='submit' value='Educate'/></form>"
)


def _service(db, dry_run):
    svc = TrainingsService.__new__(TrainingsService)
    # minimal init without the real client wiring
    from fra_bot.db.repos import (
        BoardDeletionRepo,
        BoardRepo,
        MembersRepo,
        RunsRepo,
        StateRepo,
    )
    from fra_bot.mc.board import BoardClient

    cfg = _cfg(dry_run)
    client = FakeClient({
        "/verband/gebauede": ACADEMY_LIST,
        "/buildings/4951748": ACADEMY_PAGE,
    })
    svc.cfg = cfg
    svc.client = client
    svc.board = BoardClient(client)
    svc.board_repo = BoardRepo(db)
    svc.requests = AutomationRepo(db)
    svc.members = MembersRepo(db)
    svc.runs = RunsRepo(db)
    svc.state = StateRepo(db)
    svc.deletions = BoardDeletionRepo(db)
    svc._auto = cfg.automation.training
    return svc, client


class _GuideBoard:
    """Records guide find/create/edit/delete calls to test find-or-edit."""

    def __init__(self, *, existing=None):
        self.existing = existing
        self.created: list[tuple[int, str]] = []
        self.edited: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []

    async def find_bot_post(self, thread_id, marker, *, max_pages=None):
        return self.existing

    async def create_post_get_id(self, thread_id, content):
        self.created.append((int(thread_id), content))
        return 77

    async def edit_post(self, post_id, content):
        self.edited.append((int(post_id), content))
        return True

    async def delete_post(self, thread_id, post_id):
        self.deleted.append((int(thread_id), int(post_id)))
        return True


async def test_parse_request_detects_training(db):
    svc, _ = _service(db, dry_run=True)
    post = BoardPost(1, "Alice", 42, "t", "Please open a HazMat training")
    req = await svc.parse_request(post)
    assert req is not None
    payload = json.loads(req["payload"])
    assert payload["trainings"][0]["name"] == "HazMat"


async def test_parse_request_ignores_chatter(db):
    svc, _ = _service(db, dry_run=True)
    post = BoardPost(1, "Alice", 42, "t", "thanks everyone!")
    assert await svc.parse_request(post) is None


async def test_execute_dry_run_reaches_done_without_posting(db):
    svc, client = _service(db, dry_run=True)
    repo = svc.requests
    rid = await repo.create(
        kind="training", thread_id=5935, post_id=1,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat", "duration": 3}],
            "ambiguous": [],
        }),
    )
    await repo.claim(rid)
    request = await repo.get(rid)
    await svc.execute_request(request, announce=True)

    row = await repo.get(rid)
    assert row["status"] == "done"
    assert client.posts == []  # dry-run performed NO education POST


async def test_busy_retry_is_bounded_with_backoff(db):
    """A request that can't proceed (no academy of that discipline) must
    retry with a bumped attempt count and a future next_attempt_at — so
    MAX_ATTEMPTS eventually ends it instead of re-walking the academy list
    on every poll forever."""
    svc, _ = _service(db, dry_run=True)   # list only contains a FIRE academy
    repo = svc.requests
    rid = await repo.create(
        kind="training", thread_id=5935, post_id=2,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({
            "trainings": [{"discipline": "police", "name": "SWAT", "duration": 5}],
            "ambiguous": [],
        }),
    )
    await repo.claim(rid)
    await svc.execute_request(await repo.get(rid), announce=True)

    row = await repo.get(rid)
    assert row["status"] == "waiting"
    assert row["attempts"] == 1                        # bumped -> cap reachable
    assert row["next_attempt_at"] is not None          # backed off, not next poll
    import datetime as dt
    assert row["next_attempt_at"] > dt.datetime.now(dt.timezone.utc).isoformat()


async def test_execute_live_posts_education_form(db):
    # A verified academy page whose classroom count drops after the POST.
    after_page = ACADEMY_PAGE.replace(
        "<option value='2'>2</option>", ""  # only 1 room now -> count dropped
    )
    svc, client = _service(db, dry_run=False)
    # After the POST, re-fetching the academy shows fewer rooms.
    client.pages["/buildings/4951748"] = ACADEMY_PAGE
    calls = {"n": 0}
    orig_fetch = client.fetch_page

    async def fetch(path, *, referer=None):
        if path == "/buildings/4951748":
            calls["n"] += 1
            return ACADEMY_PAGE if calls["n"] == 1 else after_page
        return await orig_fetch(path, referer=referer)

    client.fetch_page = fetch

    repo = svc.requests
    rid = await repo.create(
        kind="training", thread_id=5935, post_id=1,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat", "duration": 3}],
            "ambiguous": [],
        }),
    )
    await repo.claim(rid)
    await svc.execute_request(await repo.get(rid), announce=True)

    assert len(client.posts) == 1
    assert client.posts[0][0] == "/buildings/4951748/education"
    row = await repo.get(rid)
    assert row["status"] == "done"


class _SeqBoard:
    """Board stub whose posts can be appended between polls."""

    def __init__(self):
        self.posts = []
        self._page = SimpleNamespace(current_user_id=999)

    async def fetch_new_posts(self, thread_id, last_seen):
        fresh = [p for p in self.posts if p.post_id > (last_seen or 0)]
        return self._page, fresh


async def test_first_post_on_initially_empty_thread_is_processed(db):
    """An empty thread's first poll is the baseline; the FIRST real post that
    arrives later must be processed — not swallowed as baseline again."""
    svc, _ = _service(db, dry_run=True)
    board = _SeqBoard()
    svc.board = board

    await svc.poll()                                   # empty thread: baseline
    board.posts.append(SimpleNamespace(
        post_id=101, author_name="Bob", author_mc_id=7,
        raw_timestamp="t", content="HazMat please",
    ))
    await svc.poll()                                   # first real post

    rows = await svc.requests.recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"                 # processed (dry-run)


# -- board guide: find-or-edit, never duplicate -----------------------------

async def test_training_guide_created_then_skipped(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    await svc._ensure_guide()
    assert len(board.created) == 1                     # first time: created once
    assert board.created[0][1].startswith("[FRA] 📋 How to request a TRAINING")
    assert "HazMat" in board.created[0][1]             # lists the catalog
    assert await svc.state.get(svc._guide_id_key()) == "77"
    # Same content next poll: no duplicate, no needless edit.
    await svc._ensure_guide()
    assert len(board.created) == 1
    assert board.edited == []


async def test_training_guide_edits_existing_instead_of_duplicating(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=91)                   # a guide already on the board
    svc.board = board
    await svc._ensure_guide()
    assert board.created == []                         # found it -> edit, never create
    assert board.edited and board.edited[0][0] == 91
    assert await svc.state.get(svc._guide_id_key()) == "91"


async def test_training_guide_suppressed_when_replies_off(db):
    svc, _ = _service(db, dry_run=True)                # cfg default: reply_to_board=False
    board = _GuideBoard(existing=None)
    svc.board = board
    await svc._ensure_guide()
    assert board.created == [] and board.edited == []


async def test_training_guide_content_has_availability_and_timestamp(db):
    svc, _ = _service(db, dry_run=True)
    desired = await svc.guide_content(now_epoch=1000.0)
    assert desired.startswith("[FRA] 📋 How to request a TRAINING")
    assert "[b]Free classrooms right now[/b]" in desired
    assert "Last updated:" in desired
    # The fake academy (fire, id 4951748) has 2 free classrooms.
    assert "🚒 Fire: 2" in desired


async def test_force_guide_creates_and_reports(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    line = await svc.force_guide()
    assert line.startswith("✅") and "#77" in line
    assert len(board.created) == 1 and board.deleted == []


async def test_force_guide_repost_deletes_then_recreates(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    await svc.state.set(svc._guide_id_key(), "55")     # old guide, buried
    line = await svc.force_guide(repost=True)
    assert board.deleted == [(5935, 55)]               # old one removed
    assert len(board.created) == 1                     # fresh one at the bottom
    assert line.startswith("✅") and "#77" in line


async def test_force_guide_reports_replies_off(db):
    svc, _ = _service(db, dry_run=True)                # reply_to_board=False
    line = await svc.force_guide()
    assert "reply_to_board is off" in line


async def test_training_guide_skips_availability_fetch_when_throttled(db):
    """The expensive availability walk must NOT run on quiet polls: with the
    guide up-to-date and inside the refresh window, _ensure_guide returns
    before guide_content is ever built."""
    import hashlib

    from fra_bot.mc.board import guide_now

    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    calls = {"n": 0}
    orig = svc._collect_availability

    async def counting():
        calls["n"] += 1
        return await orig()

    svc._collect_availability = counting

    # Prime state: guide exists, signature current, refreshed just now.
    signature = hashlib.sha1(svc.guide_body().encode("utf-8")).hexdigest()[:12]
    await svc.state.set(svc._guide_id_key(), "77")
    await svc.state.set(svc._guide_hash_key(), signature)
    await svc.state.set(svc._guide_refreshed_key(), repr(guide_now()))

    await svc._ensure_guide()
    assert calls["n"] == 0                              # nothing fetched
    assert board.edited == [] and board.created == []   # nothing written

    # Push the last refresh outside the window -> now it builds + edits once.
    await svc.state.set(svc._guide_refreshed_key(), repr(guide_now() - 7200))
    await svc._ensure_guide()
    assert calls["n"] == 1
    assert board.edited and board.edited[0][0] == 77
