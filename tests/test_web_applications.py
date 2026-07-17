"""Web console applications domain: the pending list, the derived
history outcomes, and Accept/Deny actions that go through the same
``bot.applications_sync`` service path as the Discord buttons —
including the dry_run gate. All offline via aiohttp's test client."""

from urllib.parse import unquote

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.db.repos import ApplicationsRepo, MemberActionsRepo
from fra_bot.mc.errors import MissionChiefError
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


def _set_dry_run(cfg, value: bool) -> None:
    # AutomationConfig is frozen; the settings registry pokes it the same way.
    object.__setattr__(cfg.automation, "dry_run", value)


class FakeApplicationsSync:
    """Stands in for ``bot.applications_sync``: records the calls the real
    service would turn into paced game GETs and mirrors its bookkeeping
    (``mark_resolved`` after a successful action)."""

    def __init__(self, db) -> None:
        self.db = db
        self.calls = []
        self.fail = False

    async def accept(self, application_id: int) -> None:
        await self._act(application_id, "accept")

    async def deny(self, application_id: int) -> None:
        await self._act(application_id, "deny")

    async def _act(self, application_id: int, verb: str) -> None:
        if self.fail:
            raise MissionChiefError("kaboom")
        self.calls.append((verb, application_id))
        await ApplicationsRepo(self.db).mark_resolved(application_id)


class FakeBot:
    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self.actions = []
        self.applications_sync = FakeApplicationsSync(db)

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
    seed = (
        # Pending, already announced to Discord.
        (7001, "Bob", 202, "2026-07-15T09:00:00+00:00",
         "2026-07-16T09:00:00+00:00", None, "2026-07-15T09:05:00+00:00"),
        # Pending, announcement still queued.
        (7005, "Dave", None, "2026-07-16T10:00:00+00:00",
         "2026-07-16T10:00:00+00:00", None, None),
        # Vanished from the page without a console decision.
        (7002, "Carol", None, "2026-07-01T00:00:00+00:00",
         "2026-07-02T00:00:00+00:00", "2026-07-03T00:00:00+00:00",
         "2026-07-01T00:05:00+00:00"),
        # Resolved and now an active member -> derived "accepted".
        (7003, "Alice", 101, "2025-12-30T00:00:00+00:00",
         "2025-12-31T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
         "2025-12-30T00:05:00+00:00"),
    )
    for row in seed:
        await db.execute(
            "INSERT INTO applications (application_id, applicant_name, "
            "mc_user_id, first_seen_at, last_seen_at, resolved_at, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", row,
        )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def test_page_lists_pending_history_and_automation_state(client):
    response = await client.get("/applications")
    text = await response.text()
    assert response.status == 200
    assert "Pending applications" in text and "Recent history" in text
    # Both pending rows carry decision forms; announce state is visible.
    assert "/applications/7001/accept" in text
    assert "/applications/7005/deny" in text
    assert "announced" in text and "announce queued" in text
    # History outcomes are derived honestly: no console decision on
    # record means "expired / decided in-game", unless the applicant is
    # an active member now.
    assert "expired / decided in-game" in text
    assert "accepted — now a member" in text
    # Credits only where the applicant maps to a member row.
    assert "5,000" in text
    # Minimal config: auto_accept off, dry_run on — both surfaced.
    assert "auto-accept off" in text
    assert "dry-run" in text
    # The module joined the shared nav.
    assert ">Applications</a>" in text


async def test_dry_run_gate_blocks_the_game_action(client):
    response = await client.post("/applications/7001/accept",
                                 allow_redirects=False)
    assert response.status == 302
    location = unquote(response.headers["Location"])
    assert "ok=" in location and "dry-run" in location
    assert client.bot.applications_sync.calls == []
    row = await ApplicationsRepo(client.bot.db).get(7001)
    assert row["resolved_at"] is None
    assert client.bot.actions == []


async def test_accept_reuses_the_service_path_and_logs(client):
    _set_dry_run(client.bot.cfg, False)
    response = await client.post("/applications/7001/accept",
                                 allow_redirects=False)
    assert response.status == 302
    assert "ok=" in response.headers["Location"]
    assert client.bot.applications_sync.calls == [("accept", 7001)]
    row = await ApplicationsRepo(client.bot.db).get(7001)
    assert row["resolved_at"] is not None
    action = client.bot.actions[0]
    assert action["action"] == "application_accepted"
    assert "application #7001" in action["detail"]
    assert "(via Web console)" in action["detail"]
    assert action["mc_user_id"] == 202
    # The decision shows up in history as a console outcome.
    text = await (await client.get("/applications")).text()
    assert "accepted (console)" in text
    # A second accept is refused: no longer pending, no second game call.
    response = await client.post("/applications/7001/accept",
                                 allow_redirects=False)
    assert "err=" in response.headers["Location"]
    assert client.bot.applications_sync.calls == [("accept", 7001)]


async def test_deny_unknown_and_failure_paths(client):
    _set_dry_run(client.bot.cfg, False)
    # Unknown application -> error flash, no game call.
    response = await client.post("/applications/9999/deny",
                                 allow_redirects=False)
    assert "err=" in unquote(response.headers["Location"])
    assert client.bot.applications_sync.calls == []
    # Game failure -> error flash, nothing resolved, nothing logged.
    client.bot.applications_sync.fail = True
    response = await client.post("/applications/7005/deny",
                                 allow_redirects=False)
    location = unquote(response.headers["Location"])
    assert "err=" in location and "Could not deny" in location
    row = await ApplicationsRepo(client.bot.db).get(7005)
    assert row["resolved_at"] is None
    assert client.bot.actions == []
    # Recovered -> the deny goes through, is logged, and shows in history.
    client.bot.applications_sync.fail = False
    response = await client.post("/applications/7005/deny",
                                 allow_redirects=False)
    assert "ok=" in response.headers["Location"]
    assert ("deny", 7005) in client.bot.applications_sync.calls
    assert client.bot.actions[-1]["action"] == "application_denied"
    text = await (await client.get("/applications")).text()
    assert "denied (console)" in text
