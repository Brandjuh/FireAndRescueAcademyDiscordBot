"""Web console authentication: password login, session cookie, logout,
brute-force throttle, and the LAN Host rules."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

import fra_bot.web.auth as auth
from fra_bot.db.database import Database
from fra_bot.web.server import build_app, resolve_password

pytestmark = pytest.mark.asyncio

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""

PASSWORD = "hunter2-but-long"


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

    async def log_member_action(self, **kwargs) -> None:
        pass

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def client(tmp_path, cfg, monkeypatch):
    # No real sleeping in the wrong-password path.
    async def no_sleep(_):
        pass

    monkeypatch.setattr(auth.asyncio, "sleep", no_sleep)
    db = Database(tmp_path / "auth.sqlite3")
    await db.connect()
    test_client = TestClient(TestServer(
        build_app(FakeBot(db, cfg), password=PASSWORD)
    ))
    await test_client.start_server()
    yield test_client
    await test_client.close()
    await db.close()


async def _login(client, password=PASSWORD):
    return await client.post("/login", data={"password": password},
                             allow_redirects=False)


async def test_pages_require_login(client):
    for path in ("/", "/members", "/settings", "/automation",
                 "/images/fleet.png"):
        response = await client.get(path, allow_redirects=False)
        assert response.status == 302
        assert response.headers["Location"] == "/login"


async def test_health_stays_open(client):
    assert (await client.get("/health")).status == 200


async def test_login_round_trip(client):
    assert (await client.get("/login")).status == 200
    response = await _login(client)
    assert response.status == 302 and response.headers["Location"] == "/"
    cookie = response.cookies.get(auth.COOKIE_NAME)
    assert cookie is not None and cookie["httponly"]
    assert cookie["samesite"].lower() == "lax"
    # The session cookie now opens every page.
    assert (await client.get("/members", allow_redirects=False)).status == 200
    # Logout drops the session; the next page bounces to /login again.
    response = await client.post("/logout", allow_redirects=False)
    assert response.headers["Location"] == "/login"
    assert (await client.get("/", allow_redirects=False)).status == 302


async def test_wrong_password_is_refused(client):
    response = await _login(client, "nope")
    assert response.status == 403
    assert "Wrong password" in await response.text()
    assert (await client.get("/", allow_redirects=False)).status == 302


async def test_repeated_failures_lock_the_login(client):
    for _ in range(auth.MAX_FAILURES):
        await _login(client, "nope")
    response = await _login(client)  # even the RIGHT password waits now
    assert response.status == 429
    assert "locked" in (await response.text()).lower()


async def test_lan_ip_host_is_accepted_domains_still_refused(client):
    # Browsing to http://192.168.1.50:8462/ sends an IP-literal Host.
    response = await client.get(
        "/login", headers={"Host": "192.168.1.50:8462"}
    )
    assert response.status == 200
    # DNS rebinding still shows a domain — refused before auth runs.
    response = await client.get("/", headers={"Host": "attacker.example"})
    assert response.status == 403


async def test_resolve_password_generates_once_and_env_wins(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "pw.sqlite3")
    await db.connect()
    try:
        monkeypatch.delenv("WEB_PASSWORD", raising=False)
        first = await resolve_password(db)
        assert first and await resolve_password(db) == first  # stable
        monkeypatch.setenv("WEB_PASSWORD", "operator-choice")
        assert await resolve_password(db) == "operator-choice"
    finally:
        await db.close()
