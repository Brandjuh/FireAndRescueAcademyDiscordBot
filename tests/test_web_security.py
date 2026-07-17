"""The console's security middleware: Host allow-list (DNS rebinding)
and same-origin POST enforcement (cross-site form POSTs)."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
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

    async def log_member_action(self, **kwargs) -> None:
        pass

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    db = Database(tmp_path / "sec.sqlite3")
    await db.connect()
    test_client = TestClient(TestServer(build_app(FakeBot(db, cfg))))
    await test_client.start_server()
    yield test_client
    await test_client.close()
    await db.close()


async def test_unknown_host_is_refused(client):
    # DNS rebinding: attacker.example resolves to 127.0.0.1, so the
    # browser's request carries the attacker's Host header.
    response = await client.get("/", headers={"Host": "attacker.example"})
    assert response.status == 403


async def test_localhost_hosts_are_allowed(client):
    assert (await client.get("/", headers={"Host": "localhost"})).status == 200
    assert (await client.get("/")).status == 200  # 127.0.0.1:<test port>


async def test_cross_origin_post_is_refused(client):
    response = await client.post(
        "/settings", data={"key": "automation.dry_run", "value": "off"},
        headers={"Origin": "https://evil.example"}, allow_redirects=False,
    )
    assert response.status == 403


async def test_same_origin_post_passes(client):
    origin = f"http://{client.host}:{client.port}"
    response = await client.post(
        "/settings", data={"key": "automation.dry_run", "value": "on"},
        headers={"Origin": origin}, allow_redirects=False,
    )
    assert response.status == 302


async def test_cross_site_fetch_metadata_is_refused(client):
    response = await client.post(
        "/settings", data={"key": "automation.dry_run", "value": "off"},
        headers={"Sec-Fetch-Site": "cross-site"}, allow_redirects=False,
    )
    assert response.status == 403


async def test_non_browser_post_without_origin_passes(client):
    # curl / scripts send neither Origin nor Sec-Fetch-Site.
    response = await client.post(
        "/settings", data={"key": "automation.dry_run", "value": "on"},
        allow_redirects=False,
    )
    assert response.status == 302