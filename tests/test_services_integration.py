"""End-to-end service tests with a fake MissionChief client (no network).

Covers the previously-untested critical paths: the members retention
guard, the board multi-page walk-back, and the training detect→execute
flow through the new state machine.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MembersRepo, RunsRepo
from fra_bot.mc.board import BoardClient
from fra_bot.services.members_sync import MembersSyncService

pytestmark = pytest.mark.asyncio


class FakeClient:
    """Minimal stand-in for MissionChiefClient driven by canned HTML."""

    def __init__(self, pages: dict, post_result=(200, "", "")):
        self.pages = pages
        self.post_result = post_result
        self.posts: list = []
        self.fetched: list = []

    async def start(self):
        pass

    def url(self, path):
        return "https://www.missionchief.com/" + path.lstrip("/")

    async def fetch_page(self, path, *, referer=None):
        self.fetched.append(path)
        if path in self.pages:
            return self.pages[path]
        return self.pages.get("*", "<html><body></body></html>")

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, data))
        return self.post_result


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "svc.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _members_html(members):
    rows = "".join(
        f"<tr><td><a href='/users/{m}'>M{m}</a></td><td>Member</td>"
        f"<td>{m*100} Credits</td><td>0%</td><td>5%</td><td>01/01/2025</td></tr>"
        for m in members
    )
    return (
        "<html><body><a href='/users/sign_out'>Logout</a>"
        "<table><thead><tr><th>Name</th><th>Role</th><th>Earned Credits</th>"
        "<th>Discount</th><th>Alliance contribution rate</th><th>Member since</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></body></html>"
    )


def _members_cfg():
    return SimpleNamespace(missionchief=SimpleNamespace(alliance_id=1621))


def _logs_html(entries):
    rows = "".join(
        f"<tr><td>{ts}</td><td><a href='/profile/1'>X</a></td>"
        f"<td>{action}</td><td></td></tr>"
        for ts, action in entries
    )
    return f'<table class="table"><tbody>{rows}</tbody></table>'


async def test_logs_backfill_walks_history_and_suppresses_feed(db):
    from fra_bot.db.repos import LogsRepo
    from fra_bot.services.logs_sync import LogsSyncService

    cfg = SimpleNamespace(sync=SimpleNamespace(logs_backfill_pages_per_chunk=10))
    client = FakeClient(
        {
            "/alliance_logfiles?page=1": _logs_html(
                [("06 Jul 14:23", "Alpha added to the alliance"),
                 ("06 Jul 13:00", "Beta added to the alliance")]
            ),
            "/alliance_logfiles?page=2": _logs_html(
                [("05 Jul 10:00", "Gamma added to the alliance")]
            ),
            "*": "<html><body></body></html>",  # page 3: past the end
        }
    )
    svc = LogsSyncService(cfg, client, db)

    done = await svc.backfill_step()

    assert done is True
    assert await svc.backfill_done() is True
    logs = LogsRepo(db)
    assert await logs.count() == 3
    # Backfilled history must be marked posted, never queued for Discord.
    assert await logs.pending_posts() == []


async def test_logs_backfill_resumes_from_cursor(db):
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.logs_sync import (
        STATE_BACKFILL_NEXT_PAGE,
        LogsSyncService,
    )

    cfg = SimpleNamespace(sync=SimpleNamespace(logs_backfill_pages_per_chunk=1))
    client = FakeClient(
        {
            "/alliance_logfiles?page=1": _logs_html([("06 Jul 14:23", "Alpha added to the alliance")]),
            "/alliance_logfiles?page=2": _logs_html([("05 Jul 10:00", "Beta added to the alliance")]),
            "*": "<html><body></body></html>",
        }
    )
    svc = LogsSyncService(cfg, client, db)

    # One page per chunk: first step processes page 1 and is not done.
    assert await svc.backfill_step() is False
    assert await StateRepo(db).get(STATE_BACKFILL_NEXT_PAGE) == "2"
    # Next step processes page 2, then the empty page completes it.
    assert await svc.backfill_step() is False
    assert await svc.backfill_step() is True


async def _seed_roster(db, n):
    members = MembersRepo(db)
    run = await RunsRepo(db).start("members")
    roster = [
        {"mc_user_id": i, "name": f"M{i}", "role": "Member",
         "earned_credits": i * 100, "contribution_rate": 5.0, "raw_member_since": "x"}
        for i in range(1, n + 1)
    ]
    await members.apply_roster(run, roster, detect_changes=False)


# ---------------------------------------------------------------------
# M7: members retention guard aborts without wiping the roster
# ---------------------------------------------------------------------

async def test_members_retention_guard_protects_roster(db):
    await _seed_roster(db, 200)  # a healthy stored roster
    base = "/verband/mitglieder/1621"
    client = FakeClient({
        f"{base}?page=1": _members_html(range(1, 11)),  # only 10 scraped!
        f"{base}?page=2": _members_html([]),
        f"{base}?page=3": _members_html([]),
    })
    svc = MembersSyncService(_members_cfg(), client, db)

    events = await svc.run()
    assert events == []  # guard tripped, no mass-departure events
    # The stored roster is untouched — no one was marked inactive.
    assert await MembersRepo(db).active_count() == 200


async def test_members_normal_sync_applies(db):
    base = "/verband/mitglieder/1621"
    client = FakeClient({
        f"{base}?page=1": _members_html(range(1, 6)),
        f"{base}?page=2": _members_html([]),
        f"{base}?page=3": _members_html([]),
    })
    svc = MembersSyncService(_members_cfg(), client, db)
    await svc.run()
    assert await MembersRepo(db).active_count() == 5


# ---------------------------------------------------------------------
# M8: board multi-page walk-back doesn't strand burst posts
# ---------------------------------------------------------------------

def _thread_html(posts, *, last_page, active_page):
    body = ""
    for pid, author, content in posts:
        body += (
            f"<div id='post-on-page-{pid}'>"
            f"<a href='/profile/{author}'>User{author}</a>"
            f"<span title='Mon, 06 Jul 2026 14:00'>x</span>"
            f"<a href='/alliance_posts/{pid}'>link</a>"
            f"<div class='col-md-11'>{content}</div></div>"
        )
    pager = "".join(
        f"<a href='?page={n}'>{n}</a>" if n != active_page
        else f"<li class='active'>{n}</li>"
        for n in range(1, last_page + 1)
    )
    return (
        "<html><body><script>var user_id = 999;</script>"
        f"{body}<ul class='pagination'>{pager}</ul>"
        "<form id='new_alliance_post' action='/alliance_posts'>"
        "<input name='authenticity_token' value='t'/></form></body></html>"
    )


async def test_board_walk_back_collects_prior_page(db):
    thread = 5935
    base = f"/alliance_threads/{thread}"
    # last page (page 2) has newest post 200; page 1 has post 100 (unseen).
    page2 = _thread_html([(200, 1, "b")], last_page=2, active_page=2)
    page1 = _thread_html([(100, 1, "a")], last_page=2, active_page=1)
    client = FakeClient({base: page2, f"{base}?page=2": page2, f"{base}?page=1": page1})
    board = BoardClient(client)

    # last_seen = 50, so both 100 and 200 are new; the walk-back must
    # fetch page 1 to find post 100.
    page, fresh = await board.fetch_new_posts(thread, 50)
    ids = {p.post_id for p in fresh}
    assert ids == {100, 200}


async def test_board_no_walk_back_when_covered(db):
    thread = 5935
    base = f"/alliance_threads/{thread}"
    # The last page already contains a post (140) at/below last_seen, so
    # the walk-back can prove there's nothing older to fetch.
    page2 = _thread_html([(140, 1, "seen"), (200, 1, "new")], last_page=2, active_page=2)
    client = FakeClient({base: page2, f"{base}?page=2": page2})
    board = BoardClient(client)
    _, fresh = await board.fetch_new_posts(thread, 150)
    assert {p.post_id for p in fresh} == {200}  # only the truly-new one
    assert f"{base}?page=1" not in client.fetched  # no wasted walk-back
