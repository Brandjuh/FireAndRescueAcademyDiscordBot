"""Web console verification domain: approved/denied link tables, the
retry queue, and the manual link/unlink actions that reuse the exact
repo paths of ``!link`` / ``!unlink`` (roles stay Discord-side). All
offline via aiohttp's test client."""

from urllib.parse import unquote

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.db.repos import LinksRepo, MemberActionsRepo
from fra_bot.services.membersync import QUEUE_MAX_ATTEMPTS
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
    members = (
        (101, "Alice", "Admin", 1),
        (202, "Bob", "Member", 1),
        (303, "Carol", "Member", 0),   # left the alliance
    )
    for mc_id, name, role, active in members:
        await db.execute(
            "INSERT INTO members (mc_user_id, name, role, is_active, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, "
            "'2026-01-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')",
            (mc_id, name, role, active),
        )
    links = (
        (555, 101, "approved", 0),      # auto-verified, in roster
        (556, 303, "approved", 999),    # admin link, member left since
        (666, 9999, "denied", 0),       # hand-written denial, not in roster
    )
    for discord_id, mc_id, status, reviewer in links:
        await db.execute(
            "INSERT INTO member_links (discord_id, mc_user_id, status, "
            "reviewer_id, created_at, updated_at) VALUES (?, ?, ?, ?, "
            "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')",
            (discord_id, mc_id, status, reviewer),
        )
    await db.execute(
        "INSERT INTO verify_queue (discord_id, mc_user_id, display_name, "
        "guild_id, attempts, enqueued_at) VALUES (777, NULL, 'Newbie', 1, "
        "3, '2026-07-16T12:00:00+00:00')"
    )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def test_page_lists_links_queue_and_role_note(client):
    response = await client.get("/verification")
    text = await response.text()
    assert response.status == 200
    # Approved links join the roster for the MC name and dossier link.
    assert "Approved links" in text and "555" in text
    assert "/members/101" in text and "Alice" in text
    assert "left alliance" in text          # Carol's row is honest
    assert "auto" in text and "admin" in text
    # Denied links render read-only, with an honest roster miss.
    assert "Denied links" in text and "666" in text
    assert "not in roster" in text
    # The retry queue shows who/attempts/when.
    assert "Verification queue" in text and "Newbie" in text
    assert f"3/{QUEUE_MAX_ATTEMPTS}" in text
    # The role caveat is stated: roles are Discord-side only.
    assert "Discord-side" in text and "!link" in text
    # The module joined the shared nav.
    assert ">Verification</a>" in text


async def test_manual_link_matches_the_link_command_repo_path(client):
    # 777 sits in the verify queue; a manual link must clear it, exactly
    # like MemberSyncService.approve_manual does for !link.
    response = await client.post(
        "/verification/link",
        data={"discord_id": "777", "mc_user_id": "202"},
        allow_redirects=False,
    )
    assert response.status == 302
    assert "ok=" in response.headers["Location"]
    links = LinksRepo(client.bot.db)
    row = await links.get_by_discord(777)
    assert row is not None and row["status"] == "approved"
    assert row["mc_user_id"] == 202 and row["reviewer_id"] == -1  # console sentinel, not the auto-verify 0
    assert await links.queue_get(777) is None
    action = client.bot.actions[0]
    assert action["action"] == "verified"
    assert "manually linked to MC 202" in action["detail"]
    assert "(via Web console)" in action["detail"]
    assert action["discord_user_id"] == 777
    assert action["mc_user_id"] == 202
    assert action["actor_name"] == "Bob"
    # The new link shows up on the page with the roster name.
    text = await (await client.get("/verification")).text()
    assert "777" in text and "Bob" in text
    assert "Verification queue is empty" in text


async def test_manual_link_reclaims_a_taken_mc_id(client):
    # Same upsert semantics as !link: re-linking a claimed MC id moves
    # the claim to the new Discord account (re-verify after a rename).
    response = await client.post(
        "/verification/link",
        data={"discord_id": "888", "mc_user_id": "101"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    links = LinksRepo(client.bot.db)
    assert await links.get_by_discord(555) is None
    row = await links.get_by_discord(888)
    assert row is not None and row["mc_user_id"] == 101


async def test_manual_link_outside_roster_warns_but_links(client):
    # !link does not require roster presence (fresh joins predate their
    # roster row) — neither does the console, but the flash says so.
    response = await client.post(
        "/verification/link",
        data={"discord_id": "889", "mc_user_id": "424242"},
        allow_redirects=False,
    )
    location = unquote(response.headers["Location"])
    assert "ok=" in location and "not in the alliance roster" in location
    row = await LinksRepo(client.bot.db).get_by_discord(889)
    assert row is not None and row["mc_user_id"] == 424242
    assert client.bot.actions[0]["actor_name"] is None


async def test_manual_link_rejects_garbage_input(client):
    for data in (
        {"discord_id": "abc", "mc_user_id": "101"},
        {"discord_id": "555", "mc_user_id": ""},
        {"discord_id": "-5", "mc_user_id": "101"},
    ):
        response = await client.post("/verification/link", data=data,
                                     allow_redirects=False)
        assert "err=" in response.headers["Location"]
    assert client.bot.actions == []


async def test_unlink_removes_the_link_and_logs(client):
    response = await client.post(
        "/verification/unlink", data={"discord_id": "555"},
        allow_redirects=False,
    )
    assert response.status == 302
    location = unquote(response.headers["Location"])
    assert "ok=" in location and "removed in Discord" in location
    assert await LinksRepo(client.bot.db).get_by_discord(555) is None
    action = client.bot.actions[0]
    assert action["action"] == "unlinked"
    assert "(via Web console)" in action["detail"]
    assert action["discord_user_id"] == 555 and action["mc_user_id"] == 101
    # Unknown id -> error flash, nothing logged.
    response = await client.post(
        "/verification/unlink", data={"discord_id": "31337"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert len(client.bot.actions) == 1
