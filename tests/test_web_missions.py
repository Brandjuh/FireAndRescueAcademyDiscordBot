"""Web console missions page: the queue renders in the scheduler's FIFO
order with the rotation list, and every write action reuses the exact
Discord paths — ``enqueue_discord`` for new requests, ``MissionsRepo.cancel``
for cancels and the ``!fra rotation`` repo calls for the rotation. The web
never starts a mission: only the scheduler poller does."""

import json

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.cogs.missions import build_spec
from fra_bot.db.database import Database
from fra_bot.db.repos import MemberActionsRepo, MissionsRepo, RotationRepo
from fra_bot.services.missions import MissionScheduler
from fra_bot.web.server import build_app

pytestmark = pytest.mark.asyncio

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""


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
        # The real service the handler enqueues through; client/geocoder
        # stay None — enqueue_discord never touches either.
        self.missions_service = MissionScheduler(cfg, None, db, None)

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
        "VALUES (101, 'Alice', 'Member', 1, 5000, 10.0, "
        "'2026-01-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')"
    )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def _seed(bot) -> dict:
    """Two queued larges (FIFO), a waiting recurring event, two finished
    requests, and a rotation with one active + one paused entry."""
    service = bot.missions_service
    repo = MissionsRepo(bot.db)
    first = await service.enqueue_discord(
        build_spec(location="Grand Rapids"), requester_name="Alice",
        requester_mc_id=101, discord_user_id=555, channel_id=1,
    )
    second = await service.enqueue_discord(
        build_spec(location="Detroit", preset="Pile-up"),
        requester_name="Bob", requester_mc_id=None, discord_user_id=556,
        channel_id=1,
    )
    event = await service.enqueue_discord(
        build_spec(location="Amsterdam", kind="event", schedule="recurring",
                   event_type="Storm"),
        requester_name="Cara", requester_mc_id=None, discord_user_id=557,
        channel_id=1,
    )
    await repo.set_status(
        event, "waiting", "next free mission at 2026-07-18T09:00:00+00:00",
        next_attempt_at="2026-07-18T09:00:00+00:00", bump_attempts=True,
    )
    done = await service.enqueue_discord(
        build_spec(location="Berlin"), requester_name="Dana",
        requester_mc_id=None, discord_user_id=None, channel_id=None,
    )
    await repo.set_status(done, "done", "large started at 52.52000,13.40500")
    failed = await service.enqueue_discord(
        build_spec(location="Nowhereville"), requester_name="Eve",
        requester_mc_id=None, discord_user_id=None, channel_id=None,
    )
    await repo.set_status(failed, "failed", "geocoding failed: not found")
    rotation = RotationRepo(bot.db)
    active = await rotation.add(
        location_text="Rotterdam", kind="large", mission_source="preset",
        active=1, created_by="Admin",
    )
    paused = await rotation.add(
        location_text="Utrecht", kind="event", mission_source="preset",
        active=0, created_by="Admin",
    )
    return {"first": first, "second": second, "event": event, "done": done,
            "failed": failed, "active": active, "paused": paused}


async def test_page_renders_queue_rotation_and_recent(client):
    ids = await _seed(client.bot)
    response = await client.get("/missions")
    text = await response.text()
    assert response.status == 200
    # FIFO: the older large request renders before the younger one.
    assert text.index("Grand Rapids") < text.index("Detroit")
    assert "preset Pile-up" in text
    # Requester with a member link; recurring flag; waiting details.
    assert "/members/101" in text and "Alice" in text
    assert "recurring" in text
    assert "1 attempt(s)" in text
    assert "next window 2026-07-18T09:00" in text
    # Recently finished rows carry their outcomes.
    assert "large started at 52.52000,13.40500" in text
    assert "geocoding failed: not found" in text
    # Rotation entries with state and the per-entry action forms.
    assert "Rotterdam" in text and "Utrecht" in text
    assert f"/missions/rotation/{ids['active']}/pause" in text
    assert f"/missions/rotation/{ids['paused']}/resume" in text
    assert f"/missions/rotation/{ids['paused']}/remove" in text
    # Open rows get a cancel form; finished ones don't.
    assert f"/missions/{ids['first']}/cancel" in text
    assert f"/missions/{ids['done']}/cancel" not in text
    # Nav entry registered by the auto-discovery.
    assert "href='/missions'" in text


async def test_large_request_enqueues_like_the_slash_command(client):
    response = await client.post(
        "/missions/request",
        data={"kind": "large", "location": "Kansas City",
              "schedule": "once", "preset": "Pile-up"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    rows = await MissionsRepo(client.bot.db).recent()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "discord" and row["status"] == "pending"
    assert row["kind"] == "large" and row["mission_source"] == "preset"
    assert row["preset_type_id"] == 62  # Pile-up
    assert row["location_text"] == "Kansas City"
    assert row["requester_name"] == "Web console"
    assert row["discord_user_id"] is None and row["requester_mc_id"] is None
    assert row["recurring"] == 0
    action = client.bot.actions[-1]
    assert action["action"] == "mission_requested"
    assert "(via Web console)" in action["detail"]


async def test_event_request_stores_the_event_knobs(client):
    response = await client.post(
        "/missions/request",
        data={"kind": "event", "location": "Amsterdam",
              "schedule": "recurring", "event_type": "Storm",
              "area": "large", "shape": "circle", "call_volume": "30"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    row = (await MissionsRepo(client.bot.db).recent())[0]
    assert row["kind"] == "event" and row["recurring"] == 1
    assert row["event_type_id"] == 0 and row["event_random"] == 0
    assert row["area"] == "large" and row["shape"] == "circle"
    assert row["call_volume"] == "30"
    action = client.bot.actions[-1]
    assert action["action"] == "event_requested"
    assert "(via Web console)" in action["detail"]


async def test_request_validation_rejects_without_a_row(client):
    bad = (
        {"kind": "large", "location": ""},
        {"kind": "large", "location": "NYC", "saved": "Wildfire",
         "custom": "need_lf=25"},
        {"kind": "event", "location": "NYC", "event_type": "Vulcano"},
    )
    for data in bad:
        response = await client.post(
            "/missions/request", data=data, allow_redirects=False,
        )
        assert "err=" in response.headers["Location"]
    assert await MissionsRepo(client.bot.db).recent() == []
    assert client.bot.actions == []


async def test_cancel_moves_only_open_requests(client):
    ids = await _seed(client.bot)
    repo = MissionsRepo(client.bot.db)
    response = await client.post(
        f"/missions/{ids['first']}/cancel", allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    assert (await repo.get(ids["first"]))["status"] == "cancelled"
    action = client.bot.actions[-1]
    assert action["action"] == "mission_cancelled"
    assert action["discord_user_id"] == 555 and action["mc_user_id"] == 101
    assert "(via Web console)" in action["detail"]
    # Terminal rows can't be cancelled; unknown ids flash, not 500.
    response = await client.post(
        f"/missions/{ids['done']}/cancel", allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert (await repo.get(ids["done"]))["status"] == "done"
    response = await client.post("/missions/9999/cancel",
                                 allow_redirects=False)
    assert "err=" in response.headers["Location"]


async def test_rotation_add_matches_the_admin_command(client):
    repo = RotationRepo(client.bot.db)
    response = await client.post(
        "/missions/rotation/add",
        data={"location": "Berlin", "kind": "large", "saved": "Wildfire"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    response = await client.post(
        "/missions/rotation/add",
        data={"location": "NYC", "kind": "large",
              "custom": "need_lf=25 need_elw1=6", "name": "Big fire"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    response = await client.post(
        "/missions/rotation/add",
        data={"location": "Utrecht", "kind": "event"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    rows = await repo.list_all()
    assert len(rows) == 3
    saved, custom, event = rows
    assert saved["mission_source"] == "saved"
    assert saved["saved_name"] == "Wildfire" and saved["kind"] == "large"
    assert saved["active"] == 1 and saved["created_by"] == "Web console"
    assert custom["mission_source"] == "custom"
    assert custom["caption"] == "Big fire"
    assert json.loads(custom["custom_values"]) == {
        "need_lf": 25, "need_elw1": 6,
    }
    # Event entries store no event knobs — exactly like `!fra rotation
    # add | kind: event`: the scheduler picks a random type per start.
    assert event["kind"] == "event" and event["event_type_id"] is None
    # A bad spec flashes and stores nothing.
    response = await client.post(
        "/missions/rotation/add", data={"location": ""},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert len(await repo.list_all()) == 3


async def test_rotation_pause_resume_remove_round_trip(client):
    ids = await _seed(client.bot)
    repo = RotationRepo(client.bot.db)
    response = await client.post(
        f"/missions/rotation/{ids['active']}/pause", allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    assert (await repo.get(ids["active"]))["active"] == 0
    response = await client.post(
        f"/missions/rotation/{ids['active']}/resume", allow_redirects=False,
    )
    assert (await repo.get(ids["active"]))["active"] == 1
    response = await client.post(
        f"/missions/rotation/{ids['paused']}/remove", allow_redirects=False,
    )
    assert "ok=" in response.headers["Location"]
    assert await repo.get(ids["paused"]) is None
    # Unknown ids flash an error instead of a 500.
    for path in ("/missions/rotation/999/pause",
                 "/missions/rotation/999/remove"):
        response = await client.post(path, allow_redirects=False)
        assert "err=" in response.headers["Location"]
