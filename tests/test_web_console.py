"""The local web console: pages render from the live repos, mutations go
through the same repos as the Discord commands, settings reuse the
!fra-set registry. All offline via aiohttp's test client."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.db.repos import (
    MemberActionsRepo,
    MemberProfilesRepo,
    SanctionsRepo,
    StateRepo,
)
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
    await db.execute(
        "INSERT INTO members (mc_user_id, name, role, is_active, "
        "earned_credits, contribution_rate, first_seen_at, last_seen_at) "
        "VALUES (101, 'Alice', 'Admin', 1, 5000, 10.0, "
        "'2026-01-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')"
    )
    await db.execute(
        "INSERT INTO member_links (discord_id, mc_user_id, status, "
        "created_at, updated_at) VALUES (555, 101, 'approved', "
        "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
    )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def test_dashboard_shows_counts(client):
    response = await client.get("/")
    text = await response.text()
    assert response.status == 200
    assert "Active members" in text and "Game-synced" in text


async def test_members_list_and_search(client):
    text = await (await client.get("/members")).text()
    assert "Alice" in text and "/members/101" in text
    # A search that misses shows an empty capped list, not an error.
    text = await (await client.get("/members?q=zzz")).text()
    assert "Alice" not in text and "0 member(s)" in text


async def test_member_detail_renders_all_panels(client):
    text = await (await client.get("/members/101")).text()
    assert "Alice" in text
    for heading in ("Profile", "Game sync", "Sanctions", "Bot actions",
                    "Timeline", "Add sanction", "Add note"):
        assert heading in text
    assert (await client.get("/members/999")).status == 404


async def test_profile_edit_persists_and_logs(client):
    response = await client.post(
        "/members/101/profile",
        data={"timezone": "EST", "bio": "Hi there"},
        allow_redirects=False,
    )
    assert response.status == 302
    row = await MemberProfilesRepo(client.bot.db).get(555)
    assert row["timezone"] == "EST" and row["bio"] == "Hi there"
    assert any(a["action"] == "profile_updated" for a in client.bot.actions)


async def test_sanction_add_and_revoke_round_trip(client):
    response = await client.post(
        "/members/101/sanctions",
        data={"type": "w1", "reason": "AFK during event"},
        allow_redirects=False,
    )
    assert response.status == 302
    repo = SanctionsRepo(client.bot.db)
    rows = await repo.for_member(mc_user_id=101, discord_user_id=555,
                                 name="Alice")
    assert rows and rows[0]["sanction_type"].startswith("Warning - Official 1st")
    assert await repo.official_warning_count(
        mc_user_id=101, discord_user_id=555, name="Alice") == 1

    sanction_id = rows[0]["id"]
    response = await client.post(
        f"/sanctions/{sanction_id}/revoke", data={"mc_id": "101"},
        allow_redirects=False,
    )
    assert response.status == 302
    assert await repo.official_warning_count(
        mc_user_id=101, discord_user_id=555, name="Alice") == 0
    # An unknown type is rejected with a flash, not stored.
    response = await client.post(
        "/members/101/sanctions", data={"type": "bogus", "reason": "x"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]


async def test_note_lands_in_the_action_log(client):
    await client.post("/members/101/note", data={"note": "Spoke about AFK"},
                      allow_redirects=False)
    rows = await MemberActionsRepo(client.bot.db).for_member(mc_user_id=101)
    assert any("Spoke about AFK" in (r["detail"] or "") for r in rows)


async def test_settings_page_lists_registry_and_applies(client):
    text = await (await client.get("/settings")).text()
    assert "automation.dry_run" in text
    response = await client.post(
        "/settings", data={"key": "automation.dry_run", "value": "off"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    assert client.bot.cfg.automation.dry_run is False
    # Persisted as an override, exactly like !fra set.
    from fra_bot.core import settings as rt

    setting = rt.resolve("automation.dry_run")
    assert await rt.get_override(StateRepo(client.bot.db), setting) == "off"
    # Garbage value -> error flash, nothing changed.
    response = await client.post(
        "/settings", data={"key": "automation.dry_run", "value": "misschien"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]


async def test_images_404_without_the_cog(client):
    # The fake bot has no GameSyncCog: the image endpoints must 404, not 500.
    assert (await client.get("/images/infographic.png")).status == 404
    assert (await client.get("/images/fleet.png")).status == 404
