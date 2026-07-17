"""Web console chat page: the live game history renders with status and
own-account markers, and /chat/send relays through the exact same
ChatSyncService path as the Discord bridge (prefix, echo memory,
dry-run and enabled gates)."""

import time

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.core import settings as rt
from fra_bot.db.database import Database
from fra_bot.db.repos import MemberActionsRepo
from fra_bot.mc.errors import FetchError
from fra_bot.services.chat_sync import ChatSyncService
from fra_bot.web.server import build_app

pytestmark = pytest.mark.asyncio

# Chat enabled + dry-run off so sends actually reach the fake client;
# individual tests flip the gates back via the settings registry.
MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
automation:
  dry_run: false
  chat:
    enabled: true
    interval_seconds: 45
"""

CHAT_FORM_HTML = """
<html><body>
  <form action="/alliance_chats" id="new_alliance_chat" method="post">
    <input name="utf8" type="hidden" value="&#x2713;" />
    <input name="authenticity_token" type="hidden" value="secret" />
    <input id="alliance_chat_message" name="alliance_chat[message]" type="text" />
  </form>
</body></html>
"""

CHAT_HISTORY_HTML = """
<html><body>
  <div class="well" id="chat_message_6941664" data-message-time="2026-06-20T16:14:12-04:00">
    <strong><a href="/profile/814047">Mtycofire</a></strong>
    <div class="message-content"><p>@MOCOFIREEMS thanks for that</p></div>
  </div>
  <div class="well" id="chat_message_6941627" data-message-time="2026-06-20T15:45:46-04:00">
    <strong><a href="/profile/814047">Mtycofire</a></strong>
    <div class="message-content"><p>Yep 3 more missions to add to the Bermuda mess</p></div>
  </div>
</body></html>
"""


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.posts = []
        self.fail_fetch = False
        self.fail_posts = False

    def url(self, path):
        return "https://www.missionchief.com/" + path.lstrip("/")

    async def fetch_page(self, path, *, referer=None, ajax=False):
        if self.fail_fetch:
            raise FetchError(path, 503, "kaboom")
        return self.pages.get(path, "<html></html>")

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, data, kwargs))
        if self.fail_posts:
            return 500, "", ""
        return 200, "", ""


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
    """No chat_sync attribute by default — fixtures attach one, and one
    test exercises the console against a bot without the service."""

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
    bot = FakeBot(db, cfg)
    mc_client = FakeClient({
        "/": CHAT_FORM_HTML,
        "/alliance_chats": CHAT_HISTORY_HTML,
    })
    bot.chat_sync = ChatSyncService(mc_client, db)
    # Pretend the last game post was long ago: no 30s spacing sleep.
    bot.chat_sync._last_post_at = time.monotonic() - 1000
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    test_client.mc = mc_client
    yield test_client
    await test_client.close()
    await db.close()


# -- page ---------------------------------------------------------------------

async def test_page_shows_history_newest_first_and_status(client):
    response = await client.get("/chat")
    text = await response.text()
    assert response.status == 200
    # Both fixture messages render, newest (highest chat id) first.
    assert "Mtycofire" in text
    assert text.index("@MOCOFIREEMS thanks for that") < text.index(
        "Yep 3 more missions to add to the Bermuda mess"
    )
    # Raw offset timestamps render as UTC; ids and profile links appear.
    assert "2026-06-20 20:14 UTC" in text
    assert "#6941664" in text and "profile/814047" in text
    # Status panel: enabled, dry-run off, 45s interval, baseline pending.
    assert "enabled" in text and "45 s" in text
    assert "baseline pending" in text
    assert "not learned yet" in text
    # Nav entry registered by the auto-discovery.
    assert "href='/chat'" in text


async def test_page_watermark_and_fresh_count(client):
    await client.bot.chat_sync.set_last_seen(6941627)
    text = await (await client.get("/chat")).text()
    assert "<code>6941627</code>" in text
    assert "1 newer than the watermark" in text


async def test_page_marks_learned_own_account(client):
    await client.bot.chat_sync.learn_own_account("Mtycofire")
    text = await (await client.get("/chat")).text()
    assert "own account" in text


async def test_page_survives_fetch_failure(client):
    client.mc.fail_fetch = True
    response = await client.get("/chat")
    text = await response.text()
    assert response.status == 200
    assert "Live fetch failed" in text and "kaboom" in text


# -- send ---------------------------------------------------------------------

async def test_send_relays_via_the_bridge_service_with_prefix(client):
    response = await client.post(
        "/chat/send", data={"message": "hello team"}, allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    # Byte-for-byte the Discord relay's POST: prefix, CSRF echo, ajax.
    path, data, kwargs = client.mc.posts[0]
    assert "alliance_chats" in path
    assert data["alliance_chat[message]"] == "[Web console] hello team"
    assert data["authenticity_token"] == "secret"
    assert kwargs.get("ajax") is True
    # The echo is remembered so the poll won't mirror it back to Discord.
    assert await client.bot.chat_sync.consume_echo(
        "[Web console] hello team"
    ) is True
    action = client.bot.actions[-1]
    assert action["action"] == "chat_message_sent"
    assert "(via Web console)" in action["detail"]


async def test_send_dry_run_never_posts(client):
    rt.apply(client.bot.cfg, rt.resolve("automation.dry_run"), True)
    response = await client.post(
        "/chat/send", data={"message": "hello team"}, allow_redirects=False,
    )
    # Acknowledged (like the cog's 🚫 reaction) but nothing reached the game.
    assert response.status == 302 and "ok=" in response.headers["Location"]
    assert client.mc.posts == []
    assert client.bot.actions == []


async def test_send_refused_when_bridge_disabled(client):
    rt.apply(client.bot.cfg, rt.resolve("automation.chat.enabled"), False)
    response = await client.post(
        "/chat/send", data={"message": "hello team"}, allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert client.mc.posts == [] and client.bot.actions == []


async def test_send_rejects_empty_message(client):
    response = await client.post(
        "/chat/send", data={"message": "   "}, allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert client.mc.posts == [] and client.bot.actions == []


async def test_send_failure_flashes_error_and_rolls_back_echo(client):
    client.mc.fail_posts = True
    response = await client.post(
        "/chat/send", data={"message": "boom"}, allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert client.bot.actions == []
    # The rolled-back echo must not swallow a genuine future message.
    assert await client.bot.chat_sync.consume_echo("[Web console] boom") is False


async def test_page_and_send_without_chat_service(tmp_path, cfg):
    # The shared console FakeBot has no chat_sync: the page must render
    # read-only and the send must refuse, never 500.
    db = Database(tmp_path / "nochat.sqlite3")
    await db.connect()
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    try:
        response = await test_client.get("/chat")
        text = await response.text()
        assert response.status == 200
        assert "not running in this process" in text
        response = await test_client.post(
            "/chat/send", data={"message": "hi"}, allow_redirects=False,
        )
        assert "err=" in response.headers["Location"]
    finally:
        await test_client.close()
        await db.close()
