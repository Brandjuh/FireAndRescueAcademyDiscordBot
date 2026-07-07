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
    from fra_bot.db.repos import BoardRepo, MembersRepo, RunsRepo
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
    svc._auto = cfg.automation.training
    return svc, client


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
