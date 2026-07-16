"""Alliance chat ↔ Discord bridge (reference: chatmanager).

Parsing fixtures are the reference cog's own test HTML, so the port
provably reads the same pages the old bot read.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.chat_bridge import ChatBridgeCog
from fra_bot.db.database import Database
from fra_bot.mc.parsers.chat import (
    build_chat_payload,
    discord_timestamp,
    format_discord_message_for_mc,
    parse_chat_form,
    parse_chat_history,
    truncate_embed_value,
)
from fra_bot.services.chat_sync import ChatSyncService

pytestmark = pytest.mark.asyncio

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

    def url(self, path):
        return "https://www.missionchief.com/" + path.lstrip("/")

    async def fetch_page(self, path, *, referer=None, ajax=False):
        return self.pages.get(path, "<html></html>")

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, data, kwargs))
        return 200, "", ""


class FakeChannel:
    def __init__(self, channel_id=900):
        self.id = channel_id
        self.sent = []

    async def send(self, embed=None, **kwargs):
        self.sent.append(embed)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "chat.sqlite3")
    await database.connect()
    yield database
    await database.close()


# -- parsers (reference fixtures) --------------------------------------------

async def test_parse_chat_form_reads_payload_fields():
    form = parse_chat_form(CHAT_FORM_HTML, "https://www.missionchief.com/")
    assert form.action == "https://www.missionchief.com/alliance_chats"
    assert form.method == "post"
    assert form.message_field == "alliance_chat[message]"
    assert form.hidden_fields["authenticity_token"] == "secret"
    payload = build_chat_payload(form, "[DutchFireFighter] Test message")
    assert payload["alliance_chat[message]"] == "[DutchFireFighter] Test message"
    assert payload["authenticity_token"] == "secret"


async def test_parse_chat_history_oldest_first():
    messages = parse_chat_history(CHAT_HISTORY_HTML)
    assert [m.chat_id for m in messages] == [6941627, 6941664]
    assert messages[0].username == "Mtycofire"
    assert messages[0].mc_user_id == 814047
    assert messages[0].message == "Yep 3 more missions to add to the Bermuda mess"
    assert messages[1].timestamp == "2026-06-20T16:14:12-04:00"


async def test_formatting_helpers_match_reference():
    assert (
        format_discord_message_for_mc("DutchFireFighter", "Hello\nworld")
        == "[DutchFireFighter] Hello world"
    )
    assert discord_timestamp("") == "Unknown"
    assert discord_timestamp("2026-06-20T16:14:12-04:00").startswith("<t:")
    assert len(truncate_embed_value("x" * 1100)) == 1024


# -- service ------------------------------------------------------------------

async def test_send_from_discord_posts_form_and_remembers_echo(db):
    client = FakeClient({"/": CHAT_FORM_HTML})
    svc = ChatSyncService(client, db)
    svc._last_post_at = 0.0  # no 30s wait in tests
    sent = await svc.send_from_discord("Brandjuh", "hello there")
    assert sent == "[Brandjuh] hello there"
    path, data, kwargs = client.posts[0]
    assert "alliance_chats" in path
    assert data["alliance_chat[message]"] == "[Brandjuh] hello there"
    assert data["authenticity_token"] == "secret"
    assert kwargs.get("ajax") is True
    # The echo is remembered so the poll won't mirror it back.
    assert await svc.consume_echo("[Brandjuh] hello there") is True
    assert await svc.consume_echo("[Brandjuh] hello there") is False


# -- cog sync flow --------------------------------------------------------------

def _cog(db, client, channel, *, enabled=True, dry_run=False):
    cog = ChatBridgeCog.__new__(ChatBridgeCog)
    cog.bot = SimpleNamespace(
        cfg=SimpleNamespace(
            automation=SimpleNamespace(
                dry_run=dry_run,
                chat=SimpleNamespace(enabled=enabled, interval_seconds=30),
            ),
            discord=SimpleNamespace(
                channels=SimpleNamespace(chat_bridge=channel.id if channel else 0)
            ),
        ),
        get_channel=lambda cid: channel if channel and cid == channel.id else None,
    )
    cog.chat = ChatSyncService(client, db)
    return cog


async def test_first_sync_baselines_without_posting(db):
    client = FakeClient({"/alliance_chats": CHAT_HISTORY_HTML})
    channel = FakeChannel()
    cog = _cog(db, client, channel)
    result = await cog._sync_once()
    assert result["posted"] == 0  # history is never replayed
    assert channel.sent == []
    assert await cog.chat.last_seen() == 6941664


async def test_second_sync_posts_only_new_messages(db):
    client = FakeClient({"/alliance_chats": CHAT_HISTORY_HTML})
    channel = FakeChannel()
    cog = _cog(db, client, channel)
    await cog.chat.set_last_seen(6941627)  # older message already seen
    result = await cog._sync_once()
    assert result["posted"] == 1
    embed = channel.sent[0]
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Name"] == "Mtycofire"
    assert fields["Message"] == "@MOCOFIREEMS thanks for that"
    assert "6941664" in embed.footer.text
    assert await cog.chat.last_seen() == 6941664


async def test_own_relayed_message_is_not_mirrored_back(db):
    history = CHAT_HISTORY_HTML.replace(
        "@MOCOFIREEMS thanks for that", "[Brandjuh] hello there"
    )
    client = FakeClient({"/alliance_chats": history})
    channel = FakeChannel()
    cog = _cog(db, client, channel)
    await cog.chat.set_last_seen(6941627)
    await cog.chat.remember_echo("[Brandjuh] hello there")
    result = await cog._sync_once()
    assert result["skipped_echoes"] == 1
    assert channel.sent == []
    assert await cog.chat.last_seen() == 6941664


class FakeMessage:
    def __init__(self, channel, content, *, bot=False, name="Brandjuh"):
        self.author = SimpleNamespace(bot=bot, display_name=name)
        self.channel = channel
        self.content = content
        self.attachments = []
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


async def test_on_message_relays_with_prefix(db):
    client = FakeClient({"/": CHAT_FORM_HTML})
    channel = FakeChannel()
    cog = _cog(db, client, channel)
    cog.chat._last_post_at = 0.0
    await cog.on_message(FakeMessage(channel, "hello team"))
    assert client.posts, "expected a game chat post"
    assert client.posts[0][1]["alliance_chat[message]"] == "[Brandjuh] hello team"


async def test_on_message_dry_run_reacts_and_skips(db):
    client = FakeClient({"/": CHAT_FORM_HTML})
    channel = FakeChannel()
    cog = _cog(db, client, channel, dry_run=True)
    message = FakeMessage(channel, "hello team")
    await cog.on_message(message)
    assert client.posts == []
    assert message.reactions == ["🚫"]


async def test_on_message_ignores_bots_and_other_channels(db):
    client = FakeClient({"/": CHAT_FORM_HTML})
    channel = FakeChannel()
    cog = _cog(db, client, channel)
    await cog.on_message(FakeMessage(channel, "beep", bot=True))
    other = FakeChannel(channel_id=901)
    await cog.on_message(FakeMessage(other, "elsewhere"))
    assert client.posts == []
