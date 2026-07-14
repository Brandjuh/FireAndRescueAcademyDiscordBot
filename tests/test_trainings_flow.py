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
        RemindersRepo,
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
    svc.reminders = RemindersRepo(db)
    svc._auto = cfg.automation.training
    return svc, client


class _GuideBoard:
    """Records guide find/create/edit/delete calls to test find-or-edit."""

    def __init__(self, *, existing=None, order_ids=None):
        self.existing = existing
        # {marker: id|None} the order check sees; None -> return the five posts
        # already in canonical order (so the repair is a no-op for plain tests).
        self.order_ids = order_ids
        self.created: list[tuple[int, str]] = []
        self.edited: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []

    async def find_bot_post(self, thread_id, marker, *, max_pages=None):
        return self.existing

    async def find_bot_posts(self, thread_id, markers, *, max_pages=None):
        if self.order_ids is not None:
            return {m: self.order_ids.get(m) for m in markers}
        return {m: 100 + i for i, m in enumerate(markers)}  # already in order

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


async def test_dry_run_board_reply_says_would_open(db):
    """Dry-run feedback still posts to the board, but must be honest: it
    says what WOULD happen instead of claiming a class was opened."""
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    replies: list[str] = []

    class _Recorder:
        async def post_reply(self, thread_id, content):
            replies.append(content)
            return True

    svc.board = _Recorder()
    rid = await svc.requests.create(
        kind="training", thread_id=5935, post_id=8,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat", "duration": 3}],
            "ambiguous": [],
        }),
    )
    await svc.requests.claim(rid)
    await svc.execute_request(await svc.requests.get(rid), announce=True)

    assert replies, "dry-run must still post board feedback"
    text = "\n".join(replies)
    assert "dry-run" in text and "Would open" in text
    assert "nothing was started" in text
    assert "Opened:" not in text          # never claims a real open


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


# -- Discord-sourced requests (thread_id = 0) --------------------------------

async def test_reply_for_skips_discord_source(db):
    svc, _ = _service(db, dry_run=False)
    svc.cfg.automation.reply_to_board = True
    sent: list[str] = []

    class _Recorder:
        async def post_reply(self, thread_id, content):
            sent.append(content)
            return True

    svc.board = _Recorder()
    await svc.reply_for({"thread_id": 5935}, "to the board")
    await svc.reply_for({"thread_id": 0}, "never to the board")
    assert sent == ["to the board"]


async def test_discord_training_request_schedules_reminder(db):
    """A Discord request (thread 0) with remind=True runs the normal open
    flow and leaves a reminder due when the course should finish; the
    Discord flags survive the payload rewrite."""
    import datetime as dt

    svc, _ = _service(db, dry_run=True)
    rid = await svc.requests.create(
        kind="training", thread_id=0, post_id=999,
        requester_name="Alice", requester_mc_id=None,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat", "duration": 3}],
            "ambiguous": [],
            "discord_user_id": 42, "channel_id": 7, "remind": True,
        }),
    )
    await svc.requests.claim(rid)
    await svc.execute_request(await svc.requests.get(rid), announce=True)

    row = await svc.requests.get(rid)
    assert row["status"] == "done"
    assert json.loads(row["payload"])["discord_user_id"] == 42   # flags kept
    async with db.conn.execute("SELECT * FROM training_reminders") as cur:
        reminders = await cur.fetchall()
    assert len(reminders) == 1
    r = reminders[0]
    assert r["training"] == "HazMat" and r["discord_user_id"] == 42
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    assert r["due_at"] > soon                                    # ~3 days out


async def test_reminders_repo_due_and_mark(db):
    import datetime as dt

    from fra_bot.db.repos import RemindersRepo

    repo = RemindersRepo(db)
    now = dt.datetime.now(dt.timezone.utc)
    a = await repo.add(
        discord_user_id=1, channel_id=2, training="X",
        due_at=(now - dt.timedelta(hours=1)).isoformat(),
    )
    await repo.add(
        discord_user_id=1, channel_id=2, training="Y",
        due_at=(now + dt.timedelta(hours=1)).isoformat(),
    )
    due = await repo.due()
    assert [r["training"] for r in due] == ["X"]                 # only the past one
    await repo.mark_posted(a)
    assert await repo.due() == []


class _SeqBoard:
    """Board stub whose posts can be appended between polls."""

    def __init__(self):
        self.posts = []
        self._page = SimpleNamespace(current_user_id=999)

    async def fetch_new_posts(self, thread_id, last_seen):
        fresh = [p for p in self.posts if p.post_id > (last_seen or 0)]
        return self._page, fresh


async def test_poll_executes_queue_even_when_board_is_broken(db):
    """THE starvation fix: a Discord-sourced request must execute even when
    the board thread is unreachable — before, the scan error aborted the poll
    ahead of _execute_ready and the request hung 'pending' forever."""
    from fra_bot.mc.errors import FetchError

    svc, _ = _service(db, dry_run=True)

    class _BrokenBoard:
        async def fetch_new_posts(self, thread_id, last_seen):
            raise FetchError("/alliance_threads/5935", 403)

    svc.board = _BrokenBoard()
    rid = await svc.requests.create(
        kind="training", thread_id=0, post_id=1234,
        requester_name="Alice", requester_mc_id=None,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat", "duration": 3}],
            "ambiguous": [], "discord_user_id": 42, "channel_id": 7,
            "remind": False,
        }),
    )
    await svc.poll()                                  # board raises inside
    row = await svc.requests.get(rid)
    assert row["status"] == "done"                    # queue still ran


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

async def test_training_guide_creates_all_sections_then_skips(db):
    from fra_bot.services.trainings import _AGENCY_ORDER

    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    await svc._ensure_guide()
    # Overview + one post per agency, each small enough for the forum.
    assert len(board.created) == 1 + len(_AGENCY_ORDER)
    assert board.created[0][1].startswith("[FRA] 📋 How to request a TRAINING")
    assert "[b]Training Request Guide[/b]" in board.created[0][1]
    fire_post = board.created[1][1]
    assert fire_post.startswith("[FRA] 📋 Fire Station training request text")
    assert "- HazMat (3 days)" in fire_post
    assert "- Fire Station - Lifeguard Training (5 days) - opens Lifeguard Training" in fire_post
    assert all(len(content) < 2000 for _, content in board.created)
    assert await svc.state.get(svc._guide_id_key()) == "77"
    assert await svc.state.get(svc._section_id_key("fire")) == "77"
    # Same content next poll: no duplicates, no needless edits.
    await svc._ensure_guide()
    assert len(board.created) == 1 + len(_AGENCY_ORDER)
    assert board.edited == []


async def test_training_guide_edits_existing_instead_of_duplicating(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=91)                   # every marker already posted
    svc.board = board
    await svc._ensure_guide()
    assert board.created == []                         # found -> edit, never create
    assert len(board.edited) == 5 and board.edited[0][0] == 91
    assert await svc.state.get(svc._guide_id_key()) == "91"


async def test_training_guide_suppressed_when_replies_off(db):
    svc, _ = _service(db, dry_run=True)                # cfg default: reply_to_board=False
    board = _GuideBoard(existing=None)
    svc.board = board
    await svc._ensure_guide()
    assert board.created == [] and board.edited == []


async def test_training_overview_has_availability_and_timestamp(db):
    svc, _ = _service(db, dry_run=True)
    desired = await svc._overview_content(now_epoch=1000.0)
    assert desired.startswith("[FRA] 📋 How to request a TRAINING")
    assert "[b]Current academy availability[/b]" in desired
    assert "Last updated:" in desired
    # The fake academy (fire, id 4951748) has 2 free classrooms.
    assert "🚒 Fire Station: 2 classes" in desired


async def test_force_guide_creates_and_reports_sections(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    line = await svc.force_guide()
    assert line.startswith("✅")
    for part in ("overview #77", "fire #77", "police #77", "ems #77", "coastal #77"):
        assert part in line
    assert len(board.created) == 5 and board.deleted == []


async def test_force_guide_repost_deletes_then_recreates(db):
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(existing=None)
    svc.board = board
    await svc.state.set(svc._guide_id_key(), "55")       # buried overview
    await svc.state.set(svc._section_id_key("fire"), "56")
    line = await svc.force_guide(repost=True)
    assert (5935, 55) in board.deleted                   # old posts removed
    assert (5935, 56) in board.deleted
    assert len(board.created) == 5                       # fresh set at the bottom
    assert line.startswith("✅") and "overview #77" in line


async def test_force_guide_is_quick_and_arms_availability_refresh(db):
    """The forced sync must not spend minutes walking academy pages: it
    posts with an availability placeholder and clears the refresh marker so
    the next poll fills the numbers in."""
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
    line = await svc.force_guide()
    assert line.startswith("✅")
    assert calls["n"] == 0                              # no walk during force
    overview = board.created[0][1]
    assert "being refreshed" in overview                # placeholder shown
    # Refresh marker cleared -> the next poll rebuilds with real numbers.
    assert await svc.state.get(svc._guide_refreshed_key()) is None
    await svc._ensure_guide()
    assert calls["n"] == 1
    assert board.edited and "🚒 Fire Station: 2 classes" in board.edited[-1][1]


async def test_force_guide_reports_failure_reason(db):
    """A section that can't post must say WHY (e.g. no reply form)."""
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True

    class _DeadBoard:
        last_error = None

        async def find_bot_post(self, thread_id, marker, *, max_pages=None):
            return None

        async def find_bot_posts(self, thread_id, markers, *, max_pages=None):
            return {m: None for m in markers}

        async def create_post_get_id(self, thread_id, content):
            self.last_error = (
                "no reply form/token on the thread — can the bot's "
                "MissionChief account post there?"
            )
            return None

        async def edit_post(self, post_id, content):
            return False

        async def delete_post(self, thread_id, post_id):
            return True

    svc.board = _DeadBoard()
    line = await svc.force_guide()
    assert "❌" in line and "overview ❌" in line
    assert "no reply form/token" in line                # the actual reason


async def test_force_guide_reports_replies_off(db):
    svc, _ = _service(db, dry_run=True)                # reply_to_board=False
    line = await svc.force_guide()
    assert "reply_to_board is off" in line


async def test_training_guide_skips_availability_fetch_when_throttled(db):
    """The expensive availability walk must NOT run on quiet polls: with the
    overview up-to-date and inside the refresh window, _ensure_guide returns
    before the availability content is ever built."""
    import hashlib

    from fra_bot.mc.board import guide_now
    from fra_bot.services.trainings import _AGENCY_ORDER, _discipline_guide

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

    # Prime state: every post exists, signatures current, refreshed just now.
    now = repr(guide_now())
    signature = hashlib.sha1(svc.guide_body().encode("utf-8")).hexdigest()[:12]
    await svc.state.set(svc._guide_id_key(), "77")
    await svc.state.set(svc._guide_hash_key(), signature)
    await svc.state.set(svc._guide_refreshed_key(), now)
    for key in _AGENCY_ORDER:
        sig = hashlib.sha1(_discipline_guide(key).encode("utf-8")).hexdigest()[:12]
        await svc.state.set(svc._section_id_key(key), "78")
        await svc.state.set(svc._section_hash_key(key), sig)
        await svc.state.set(svc._section_refreshed_key(key), now)

    await svc._ensure_guide()
    assert calls["n"] == 0                              # nothing fetched
    assert board.edited == [] and board.created == []   # nothing written

    # Push the overview's refresh outside the window -> it rebuilds ONCE;
    # the static agency posts stay untouched.
    await svc.state.set(svc._guide_refreshed_key(), repr(guide_now() - 7200))
    await svc._ensure_guide()
    assert calls["n"] == 1
    assert board.edited == [(77, await svc._overview_content(0.0))] or (
        len(board.edited) == 1 and board.edited[0][0] == 77
    )


def _guide_markers():
    from fra_bot.services.trainings import (
        GUIDE_MARKER, _AGENCY_ORDER, _section_marker,
    )
    return [GUIDE_MARKER] + [_section_marker(k) for k in _AGENCY_ORDER]


async def test_guide_order_leaves_ordered_posts_untouched(db):
    from fra_bot.mc.board import guide_now

    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    order = {m: 10 + i for i, m in enumerate(_guide_markers())}  # strictly increasing
    board = _GuideBoard(order_ids=order)
    svc.board = board
    await svc._maybe_repair_guide_order(guide_now())
    assert board.deleted == []  # already in order -> no rebuild


async def test_guide_order_rebuilds_when_out_of_order(db):
    from fra_bot.mc.board import guide_now

    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    markers = _guide_markers()
    # Police (12) sits BEFORE Fire (20): a post was deleted and reposted at the
    # bottom, breaking the canonical order.
    order = {markers[0]: 10, markers[1]: 20, markers[2]: 12,
             markers[3]: 13, markers[4]: 14}
    # Prime stored ids so the rebuild must clear them.
    await svc.state.set(svc._guide_id_key(), "10")
    board = _GuideBoard(order_ids=order)
    svc.board = board
    await svc._maybe_repair_guide_order(guide_now())
    # Every surviving guide post is deleted so the ensure pass recreates them
    # top-to-bottom in order; bookkeeping is cleared.
    assert sorted(pid for _, pid in board.deleted) == [10, 12, 13, 14, 20]
    assert await svc.state.get(svc._guide_id_key()) is None


async def test_guide_order_rebuilds_when_a_post_is_missing(db):
    from fra_bot.mc.board import guide_now

    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    markers = _guide_markers()
    order = {markers[0]: 10, markers[1]: 11, markers[2]: None,  # EMS deleted
             markers[3]: 13, markers[4]: 14}
    board = _GuideBoard(order_ids=order)
    svc.board = board
    await svc._maybe_repair_guide_order(guide_now())
    assert sorted(pid for _, pid in board.deleted) == [10, 11, 13, 14]


async def test_guide_order_check_is_throttled(db):
    from fra_bot.mc.board import guide_now

    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    board = _GuideBoard(order_ids={})  # every marker missing -> would rebuild
    svc.board = board
    now = guide_now()
    await svc.state.set(svc._guide_order_key(), repr(now))  # just checked
    await svc._maybe_repair_guide_order(now + 60)            # 1 min later
    assert board.deleted == []  # throttled: no board walk, no rebuild


async def test_live_success_reply_uses_reference_format_and_sends_pm(db):
    """Board success: the reply follows the reference bot's structure and
    the requester gets an in-game PM (board posts have no Discord id)."""
    after_page = ACADEMY_PAGE.replace("<option value='2'>2</option>", "")
    svc, client = _service(db, dry_run=False)
    svc.cfg.automation.reply_to_board = True
    compose = (
        "<form action='/messages' method='post'>"
        "<input type='hidden' name='authenticity_token' value='tok'/>"
        "<input type='text' name='message[recipient]' value=''/>"
        "<input type='text' name='message[subject]' value=''/>"
        "<textarea name='message[body]'></textarea></form>"
    )
    calls = {"n": 0}
    orig_fetch = client.fetch_page

    async def fetch(path, *, referer=None):
        if path == "/buildings/4951748":
            calls["n"] += 1
            return ACADEMY_PAGE if calls["n"] == 1 else after_page
        if path == "/messages/new":
            return compose
        return await orig_fetch(path, referer=referer)

    client.fetch_page = fetch
    replies: list[str] = []

    class _Board:
        async def post_reply(self, thread_id, content):
            replies.append(content)
            return True

    svc.board = _Board()
    rid = await svc.requests.create(
        kind="training", thread_id=5935, post_id=11,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat", "duration": 3}],
            "ambiguous": [],
        }),
    )
    await svc.requests.claim(rid)
    await svc.execute_request(await svc.requests.get(rid), announce=True)

    text = "\n".join(replies)
    assert "Training request processed for Alice." in text
    assert "- HazMat: opened 1 class(es) in academy 4951748" in text
    assert "Where to find and join the class:" in text
    assert "https://www.missionchief.com/buildings/4951748" in text

    # One education POST + one in-game PM.
    pm_posts = [p for p in client.posts if p[0] == "/messages"]
    assert len(pm_posts) == 1
    assert pm_posts[0][1]["message[recipient]"] == "Alice"
    assert "started automatically" in pm_posts[0][1]["message[body]"]


async def test_all_failed_reply_uses_could_not_be_processed(db):
    """When nothing can be opened at all, the reference bot's error format
    is used instead of the processed format."""
    svc, _ = _service(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    replies: list[str] = []

    class _Board:
        async def post_reply(self, thread_id, content):
            replies.append(content)
            return True

    svc.board = _Board()
    rid = await svc.requests.create(
        kind="training", thread_id=5935, post_id=12,
        requester_name="Alice", requester_mc_id=42,
        payload=json.dumps({
            "trainings": [],
            "ambiguous": [{"name": "Lifeguard Training",
                           "disciplines": ["fire", "coastal"]}],
        }),
    )
    await svc.requests.claim(rid)
    await svc.execute_request(await svc.requests.get(rid), announce=True)

    assert replies
    assert "Training request could not be processed for Alice." in replies[0]
    assert "exists in multiple academy types" in replies[0]
    assert "Fire Station - Lifeguard Training" in replies[0]


# -- multi-class requests (Discord: up to 4 copies of one course) ------------

async def test_clamp_class_count_bounds():
    from fra_bot.services.trainings import clamp_class_count

    assert clamp_class_count(0) == 1
    assert clamp_class_count(1) == 1
    assert clamp_class_count("3") == 3
    assert clamp_class_count(9) == 4       # MAX_CLASSES_PER_REQUEST
    assert clamp_class_count(None) == 1
    assert clamp_class_count("junk") == 1


async def test_multi_class_request_opens_every_copy_once(db):
    """count=3 opens the course three times but schedules ONE reminder
    (the copies share start + duration)."""
    svc, _ = _service(db, dry_run=True)
    rid = await svc.requests.create(
        kind="training", thread_id=0, post_id=1,
        requester_name="Alice", requester_mc_id=None,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat",
                           "duration": 3, "count": 3}],
            "ambiguous": [],
            "discord_user_id": 42, "channel_id": 7, "remind": True,
        }),
    )
    await svc.requests.claim(rid)
    await svc.execute_request(await svc.requests.get(rid), announce=True)

    row = await svc.requests.get(rid)
    assert row["status"] == "done"
    results = json.loads(row["payload"])["results"]
    assert [r["outcome"] for r in results] == ["opened"] * 3
    async with db.conn.execute("SELECT * FROM training_reminders") as cur:
        assert len(await cur.fetchall()) == 1


async def test_multi_class_parks_the_remainder_when_rooms_run_out(db):
    """count=2 with one free room: the first copy opens (verified by the
    room-count drop), the second finds no room and is parked with its
    remaining count for the next pass."""
    one_room = ACADEMY_PAGE.replace("<option value='2'>2</option>", "")
    no_rooms = one_room.replace("<option value='1'>1</option>", "")
    svc, client = _service(db, dry_run=False)
    calls = {"n": 0}
    orig_fetch = client.fetch_page

    async def fetch(path, *, referer=None):
        if path == "/buildings/4951748":
            calls["n"] += 1
            return one_room if calls["n"] == 1 else no_rooms
        return await orig_fetch(path, referer=referer)

    client.fetch_page = fetch
    rid = await svc.requests.create(
        kind="training", thread_id=0, post_id=2,
        requester_name="Alice", requester_mc_id=None,
        payload=json.dumps({
            "trainings": [{"discipline": "fire", "name": "HazMat",
                           "duration": 3, "count": 2}],
            "ambiguous": [],
            "discord_user_id": 42, "channel_id": 7,
        }),
    )
    await svc.requests.claim(rid)
    await svc.execute_request(await svc.requests.get(rid), announce=True)

    assert len(client.posts) == 1          # only the first copy was posted
    row = await svc.requests.get(rid)
    assert row["status"] == "waiting"
    pending = json.loads(row["payload"])["pending_trainings"]
    assert pending == [{"discipline": "fire", "name": "HazMat", "count": 1}]
    assert "HazMat" in row["status_detail"]


# -- live course harvest (the Discord dropdown must never miss a course) -----

RICH_ACADEMY_PAGE = ACADEMY_PAGE.replace(
    "<select name='education_select'><option value='12'>HazMat</option></select>",
    "<select name='education_select'>"
    "<option value='12'>HazMat</option>"
    "<option value='31'>Brand New Course</option>"
    "<option value='32'>Foam Firefighting Training (2 days)</option>"
    "</select>",
)


async def test_availability_walk_harvests_the_live_course_list(db):
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.trainings import (
        TRAINING_COURSES_STATE_KEY,
        merged_course_catalog,
    )

    svc, client = _service(db, dry_run=True)
    client.pages["/buildings/4951748"] = RICH_ACADEMY_PAGE
    assert await svc._collect_availability() is not None

    raw = await StateRepo(db).get(TRAINING_COURSES_STATE_KEY)
    assert raw is not None
    catalog = await merged_course_catalog(StateRepo(db))
    fire = catalog["fire"]
    # The live dropdown replaces the built-in fire list entirely.
    assert set(fire) == {"HazMat", "Brand New Course",
                         "Foam Firefighting Training"}
    assert fire["HazMat"] == 3                     # days from the built-in catalog
    assert fire["Foam Firefighting Training"] == 2  # days from the "(2 days)" label
    assert fire["Brand New Course"] == 0            # brand new: unknown duration
    # Agencies without an academy in the walk keep the built-in catalog.
    assert catalog["police"]  # non-empty static fallback


async def test_empty_harvest_keeps_the_previous_course_list(db):
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.trainings import merged_course_catalog

    svc, client = _service(db, dry_run=True)
    client.pages["/buildings/4951748"] = RICH_ACADEMY_PAGE
    await svc._collect_availability()
    # A later walk that can't read any academy page must not wipe the list.
    client.pages["/buildings/4951748"] = "<html>maintenance</html>"
    await svc._collect_availability()
    catalog = await merged_course_catalog(StateRepo(db))
    assert "Brand New Course" in catalog["fire"]


async def test_board_parse_uses_the_live_catalog_and_counts(db):
    """A board post naming a live-harvested course (absent from the
    built-in list) parses into a request with its copy count."""
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.trainings import TRAINING_COURSES_STATE_KEY

    await StateRepo(db).set(TRAINING_COURSES_STATE_KEY, json.dumps({
        "courses": {"fire": {"Technical Rescue Training": 4}}, "at": 1,
    }))
    svc, _ = _service(db, dry_run=True)
    request = await svc.parse_request(BoardPost(
        post_id=1, author_name="Alice", author_mc_id=42,
        raw_timestamp="", content="3x Technical Rescue Training",
    ))
    assert request is not None
    payload = json.loads(request["payload"])
    assert payload["trainings"] == [{
        "discipline": "fire", "name": "Technical Rescue Training",
        "duration": 4, "count": 3,
    }]


async def test_find_academies_uses_api_type_id(db):
    import json
    svc, _ = _service(db, dry_run=True)
    # A coastal academy (type 24) with NO list-page start button — the API
    # type-id must still find it; a fire station (type 0) is ignored.
    svc.client = FakeClient({
        "/api/buildings": json.dumps([
            {"id": 700, "building_type": 24, "latitude": 1.0, "longitude": 2.0},
            {"id": 701, "building_type": 0, "latitude": 1.0, "longitude": 2.0},
        ]),
    })
    academies = await svc._find_academies("coastal")
    assert [a.building_id for a in academies] == [700]
    assert academies[0].discipline == "coastal"


async def test_find_academies_falls_back_to_list_scrape(db):
    # No usable /api/buildings JSON → the alliance-buildings scrape still finds
    # the fire academy (it has a list-page start button).
    svc, _ = _service(db, dry_run=True)
    academies = await svc._find_academies("fire")
    assert [a.building_id for a in academies] == [4951748]
