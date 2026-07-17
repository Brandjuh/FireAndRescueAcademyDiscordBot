"""Web console requests page: the list renders every automation_requests
row with working filters, and the create/requeue actions go through the
exact same repo calls and payload encoding as the Discord panel."""

import json

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.cogs.requests_panel import (
    DISCORD_THREAD,
    building_request_payload,
    training_request_payload,
)
from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo, MemberActionsRepo
from fra_bot.web.server import build_app

pytestmark = pytest.mark.asyncio

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""

MAPS_LINK = "https://maps.app.goo.gl/AbCdEf12345"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from fra_bot.config import load_config

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("MC_EMAIL", "x@example.com")
    monkeypatch.setenv("MC_PASSWORD", "x")
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return load_config(path)


class FakeBot:
    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self.actions = []

    async def log_member_action(self, **kwargs) -> None:
        self.actions.append(kwargs)
        await MemberActionsRepo(self.db).log(
            action=kwargs.get("action", "?"), detail=kwargs.get("detail"),
            discord_user_id=kwargs.get("discord_user_id"),
            mc_user_id=kwargs.get("mc_user_id"),
            actor_name=kwargs.get("actor_name"),
        )

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    db = Database(tmp_path / "web.sqlite3")
    await db.connect()
    await db.execute(
        "INSERT INTO members (mc_user_id, name, role, is_active, "
        "earned_credits, contribution_rate, first_seen_at, last_seen_at) "
        "VALUES (101, 'BoardBob', 'Member', 1, 5000, 10.0, "
        "'2026-01-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')"
    )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def _seed(db) -> dict:
    """One board training, one Discord building, one failed training."""
    repo = AutomationRepo(db)
    board_id = await repo.create(
        kind="training", thread_id=4242, post_id=77,
        requester_name="BoardBob", requester_mc_id=101,
        payload=json.dumps(
            {"trainings": [{"name": "SWAT-Training", "count": 2}]}
        ),
    )
    discord_id = await repo.create(
        kind="building", thread_id=DISCORD_THREAD, post_id=555000111,
        requester_name="DiscordDave", requester_mc_id=None,
        payload=json.dumps({"link": MAPS_LINK, "discord_user_id": 555}),
    )
    await repo.set_status(discord_id, "done", "built hospital #91")
    failed_id = await repo.create(
        kind="training", thread_id=DISCORD_THREAD, post_id=555000222,
        requester_name="FailedFred", requester_mc_id=None,
        payload=json.dumps({"trainings": [{"name": "Critical Care"}],
                            "discord_user_id": 777}),
    )
    await repo.set_status(failed_id, "failed", "no free classroom",
                          bump_attempts=True)
    return {"board": board_id, "discord": discord_id, "failed": failed_id}


async def test_list_shows_rows_status_and_error_info(client):
    ids = await _seed(client.bot.db)
    response = await client.get("/requests")
    text = await response.text()
    assert response.status == 200
    for name in ("BoardBob", "DiscordDave", "FailedFred"):
        assert name in text
    # Payload summary, board-post source link, member link, error detail.
    assert "SWAT-Training ×2" in text
    assert "alliance_threads/4242" in text and "board #77" in text
    assert "/members/101" in text
    assert "built hospital #91" in text
    assert "no free classroom" in text and "1 attempt(s)" in text
    # The requeue button appears ONLY on the terminal failed row.
    assert f"/requests/{ids['failed']}/requeue" in text
    assert f"/requests/{ids['board']}/requeue" not in text
    assert f"/requests/{ids['discord']}/requeue" not in text
    # Nav entry registered by the auto-discovery.
    assert "href='/requests'" in text


async def test_list_filters_by_kind_status_and_source(client):
    await _seed(client.bot.db)
    text = await (await client.get("/requests?kind=building")).text()
    assert "DiscordDave" in text
    assert "BoardBob" not in text and "FailedFred" not in text
    text = await (await client.get("/requests?status=failed")).text()
    assert "FailedFred" in text
    assert "BoardBob" not in text and "DiscordDave" not in text
    text = await (await client.get("/requests?source=board")).text()
    assert "BoardBob" in text
    assert "DiscordDave" not in text and "FailedFred" not in text
    text = await (await client.get("/requests?source=discord")).text()
    assert "BoardBob" not in text
    assert "DiscordDave" in text and "FailedFred" in text


async def test_forms_render_catalog_courses_and_class_counts(client):
    text = await (await client.get("/requests")).text()
    # Course select: built-in catalog grouped per academy, encoded values.
    assert "<optgroup label='Fire'>" in text
    assert "value='fire|HazMat'" in text
    assert "4 classes — 40 people" in text
    assert "action='/requests/building'" in text


async def test_training_request_enqueues_exactly_like_the_panel(client):
    response = await client.post(
        "/requests/training", data={"course": "fire|HazMat", "count": "2"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    rows = await AutomationRepo(client.bot.db).recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "training" and row["status"] == "pending"
    assert row["thread_id"] == DISCORD_THREAD and row["post_id"] == 0
    assert row["requester_name"] == "Web console"
    assert row["requester_mc_id"] is None
    # Byte-for-byte the panel's payload encoding (web has no Discord user).
    expected = training_request_payload(
        "fire", "HazMat", user_id=0, channel_id=None, remind=False, count=2,
    )
    assert json.loads(row["payload"]) == expected
    assert json.loads(row["payload"])["trainings"][0]["duration"] == 3
    action = client.bot.actions[-1]
    assert action["action"] == "training_requested"
    assert "(via Web console)" in action["detail"]


async def test_training_request_rejects_unknown_course(client):
    for course in ("fire|Not A Course", "HazMat", ""):
        response = await client.post(
            "/requests/training", data={"course": course, "count": "1"},
            allow_redirects=False,
        )
        assert "err=" in response.headers["Location"]
    assert await AutomationRepo(client.bot.db).recent() == []
    assert client.bot.actions == []


async def test_building_request_enqueues_link_only_payload(client):
    response = await client.post(
        "/requests/building", data={"link": MAPS_LINK},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    rows = await AutomationRepo(client.bot.db).recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "building" and row["status"] == "pending"
    assert row["thread_id"] == DISCORD_THREAD and row["post_id"] == 0
    assert row["requester_name"] == "Web console"
    # The queue-as-is shape: the poller geocodes + validates the pin.
    expected = building_request_payload(MAPS_LINK, user_id=0, channel_id=None)
    assert json.loads(row["payload"]) == expected
    action = client.bot.actions[-1]
    assert action["action"] == "building_requested"
    assert "(via Web console)" in action["detail"]


async def test_building_request_rejects_non_maps_link_without_a_row(client):
    response = await client.post(
        "/requests/building", data={"link": "https://example.com/hospital"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    # Flash only — no skipped audit row for an operator typo.
    assert await AutomationRepo(client.bot.db).recent() == []
    assert client.bot.actions == []


async def test_requeue_rearms_only_terminal_requests(client):
    ids = await _seed(client.bot.db)
    repo = AutomationRepo(client.bot.db)
    response = await client.post(
        f"/requests/{ids['failed']}/requeue", allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    row = await repo.get(ids["failed"])
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["status_detail"] == "re-queued by admin"
    action = client.bot.actions[-1]
    assert action["action"] == "request_requeued"
    assert action["discord_user_id"] == 777
    # A pending request cannot be re-queued (it is being worked on).
    response = await client.post(
        f"/requests/{ids['board']}/requeue", allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert (await repo.get(ids["board"]))["status"] == "pending"
    # Unknown id: error flash, not a 500.
    response = await client.post("/requests/999/requeue",
                                 allow_redirects=False)
    assert "err=" in response.headers["Location"]
