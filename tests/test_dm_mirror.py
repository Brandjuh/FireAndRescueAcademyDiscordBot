"""In-game DM mirror: mailbox parsing, thread mirroring both directions,
dedup across scans, echo suppression, and the Discord→game reply path."""

import json
from types import SimpleNamespace

import discord
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import DmMirrorRepo
from fra_bot.mc import mailbox
from fra_bot.services.dm_mirror import (
    DmMirrorService,
    split_chunks,
    thread_title,
)

INBOX_HTML = """
<form action="/messages/move_folder">
  <input type="hidden" name="current_box" value="inbox"/>
  <table><tbody>
    <tr>
      <td><input type="checkbox" name="conversations[]" value="9001"/></td>
      <td>New</td>
      <td><a href="/profile/111">Alex1129</a></td>
      <td><a href="/messages/9001">Question about tax</a></td>
      <td>today</td>
    </tr>
    <tr>
      <td><input type="checkbox" name="conversations[]" value="9002"/></td>
      <td></td>
      <td><a href="/profile/222">4m1rudin</a></td>
      <td><a href="/messages/9002">Reminder: Please set your alliance donation to 5%</a></td>
      <td>today</td>
    </tr>
    <tr>
      <td><input type="checkbox" name="conversations[]" value="9003"/></td>
      <td>New</td>
      <td><a href="/profile/333">System</a></td>
      <td><a href="/messages/system_message/5">Daily reward</a></td>
      <td>today</td>
    </tr>
  </tbody></table>
</form>
"""

CONV_9001_HTML = """
<div class="well" data-message-time="2026-07-11T10:00:00+00:00">
  <a href="/profile/111">Alex1129</a>
  <p>Why did I get a warning?</p>
</div>
<div class="well" data-message-time="2026-07-11T09:00:00+00:00">
  <a href="/profile/999">FRA-Bot</a>
  <p>Hello Alex1129,</p><p>Please set your donation to 5%.</p>
</div>
<form action="/messages">
  <input type="hidden" name="authenticity_token" value="tok"/>
  <input type="hidden" name="message[conversation_id]" value="9001"/>
  <textarea name="message[body]"></textarea>
  <input type="submit" name="commit" value="Reply"/>
</form>
"""

CONV_9002_HTML = """
<div class="well" data-message-time="2026-07-11T08:00:00+00:00">
  <a href="/profile/999">FRA-Bot</a>
  <p>Hello 4m1rudin,</p><p>This is a friendly reminder about your donation.</p>
</div>
<form action="/messages">
  <input type="hidden" name="authenticity_token" value="tok"/>
  <input type="hidden" name="message[conversation_id]" value="9002"/>
  <textarea name="message[body]"></textarea>
</form>
"""

SENT_HTML = "<html><body>Message Sent.</body></html>"

COMPOSE_HTML = """
<form action="/messages" method="post">
  <input type="hidden" name="authenticity_token" value="tok"/>
  <input type="text" name="message[recipient]" value=""/>
  <input type="text" name="message[subject]" value=""/>
  <textarea name="message[body]"></textarea>
  <input type="submit" name="commit" value="Send"/>
</form>
"""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_inbox_rows_and_skips_system_messages():
    rows = mailbox.parse_inbox(INBOX_HTML)
    assert [r.conversation_id for r in rows] == ["9001", "9002"]
    assert rows[0].sender == "Alex1129" and rows[0].is_new is True
    assert rows[1].sender == "4m1rudin" and rows[1].is_new is False
    assert rows[0].subject == "Question about tax"
    assert mailbox.parse_inbox("<html>no form</html>") == []


def test_parse_conversation_messages():
    messages = mailbox.parse_conversation(CONV_9001_HTML)
    assert len(messages) == 2
    assert messages[0].author == "Alex1129"
    assert messages[0].body == "Why did I get a warning?"
    assert messages[0].timestamp == "2026-07-11T10:00:00+00:00"
    assert messages[1].author == "FRA-Bot"
    assert messages[1].body == "Hello Alex1129,\nPlease set your donation to 5%."


def test_build_reply_payload_echoes_form_and_sets_body():
    action, payload = mailbox.build_reply_payload(CONV_9001_HTML, "On it!")
    data = dict(payload)
    assert action == "/messages"
    assert data["authenticity_token"] == "tok"
    assert data["message[conversation_id]"] == "9001"
    assert data["message[body]"] == "On it!"
    assert data["commit"] == "Reply"


def test_thread_title_keeps_id_suffix():
    title = thread_title("Alex1129", "x" * 200, "9001")
    assert len(title) <= 100 and title.endswith("· #9001")
    assert thread_title("A", "Hi", "1") == "A · Hi · #1"


def test_split_chunks_prefers_paragraphs():
    text = ("para one\n\n" + "a" * 1900 + "\n\npara three")
    chunks = split_chunks(text)
    assert all(len(c) <= 1900 for c in chunks)
    assert chunks[0].startswith("para one")


# ---------------------------------------------------------------------------
# Service fakes
# ---------------------------------------------------------------------------

class FakeMC:
    def __init__(self):
        self.inbox_html = INBOX_HTML
        self.conversations = {"9001": CONV_9001_HTML, "9002": CONV_9002_HTML}
        self.posts = []
        self.reply_response = SENT_HTML

    def url(self, path):
        return "https://www.missionchief.com" + path

    async def fetch_page(self, path, **kwargs):
        if path == "/messages":
            return self.inbox_html
        if path.rstrip("/").endswith("/messages/new"):
            return COMPOSE_HTML
        cid = path.rsplit("/", 1)[-1]
        return self.conversations[cid]

    @staticmethod
    def _pairs(data):
        return list(data.items()) if isinstance(data, dict) else list(data)

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, self._pairs(data)))
        return (200, self.reply_response, "https://www.missionchief.com/messages/9001")


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeThread:
    _next_id = 7000

    def __init__(self, name, embed, bot, forum):
        FakeThread._next_id += 1
        self.id = FakeThread._next_id
        self.name = name
        self.embeds = [embed] if embed else []
        self.messages = []
        self._bot = bot
        self._forum = forum
        bot.add_channel(self)

    async def send(self, content=None, allowed_mentions=None, embed=None):
        self.messages.append(content)
        return FakeMessage(content)


class FakeForum:
    def __init__(self, channel_id, bot):
        self.id = channel_id
        self.threads = []
        self._bot = bot
        bot.add_channel(self)

    async def create_thread(
        self, *, name, embed=None, allowed_mentions=None, reason=None
    ):
        thread = FakeThread(name, embed, self._bot, self)
        self.threads.append(thread)
        return SimpleNamespace(thread=thread, message=None)


class FakeBot:
    def __init__(self):
        self._channels = {}

    def add_channel(self, channel):
        self._channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self._channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "gone"
            )
        return channel


def _cfg(dry_run=False):
    return SimpleNamespace(
        discord=SimpleNamespace(
            channels=SimpleNamespace(dm_mirror=800),
            admin_role_ids=(1,),
            staff_role_ids=(2,),
        ),
        automation=SimpleNamespace(
            dry_run=dry_run,
            dm_mirror=SimpleNamespace(enabled=True, interval=15),
        ),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "dm.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _service(db, cfg=None):
    bot = FakeBot()
    forum = FakeForum(800, bot)
    mc = FakeMC()
    service = DmMirrorService(cfg or _cfg(), mc, db, bot)
    return service, forum, mc, bot


# ---------------------------------------------------------------------------
# Mirroring
# ---------------------------------------------------------------------------

async def test_scan_mirrors_incoming_and_outgoing_conversations(db):
    service, forum, _, _ = _service(db)
    summary = await service.scan()
    # 9001 (incoming, New) AND 9002 (outgoing-only, no badge but unknown)
    assert summary["threads_created"] == 2
    assert len(forum.threads) == 2
    conv1 = next(t for t in forum.threads if "#9001" in t.name)
    assert conv1.name.startswith("Alex1129 · Question about tax")
    # Both directions mirrored, chronological, with direction arrows.
    assert "📤 **FRA-Bot**" in conv1.messages[0]
    assert "Please set your donation to 5%." in conv1.messages[0]
    assert "📥 **Alex1129**" in conv1.messages[1]
    assert "<t:" in conv1.messages[1]  # real timestamp rendering
    conv2 = next(t for t in forum.threads if "#9002" in t.name)
    assert "📤 **FRA-Bot**" in conv2.messages[0]


async def test_second_scan_is_quiet(db):
    service, forum, mc, _ = _service(db)
    await service.scan()
    # Badge cleared in game after reading; nothing changed since.
    mc.inbox_html = INBOX_HTML.replace(">New<", "><")
    summary = await service.scan()
    assert summary["threads_created"] == 0 and summary["mirrored"] == 0
    assert len(forum.threads) == 2
    assert len(next(t for t in forum.threads if "#9001" in t.name).messages) == 2


async def test_new_reply_mirrors_only_the_new_message(db):
    service, forum, mc, _ = _service(db)
    await service.scan()
    mc.conversations["9001"] = CONV_9001_HTML.replace(
        '<div class="well" data-message-time="2026-07-11T10:00:00+00:00">',
        '<div class="well" data-message-time="2026-07-11T12:00:00+00:00">'
        '<a href="/profile/111">Alex1129</a><p>Fixed it, thanks!</p></div>'
        '<div class="well" data-message-time="2026-07-11T10:00:00+00:00">',
    )
    summary = await service.scan()
    assert summary["mirrored"] == 1
    thread = next(t for t in forum.threads if "#9001" in t.name)
    assert len(thread.messages) == 3
    assert "Fixed it, thanks!" in thread.messages[-1]


async def test_reply_from_thread_posts_into_the_game(db):
    service, forum, mc, _ = _service(db)
    await service.scan()
    thread = next(t for t in forum.threads if "#9001" in t.name)
    ok, detail = await service.reply_from_thread(thread.id, "No problem!")
    assert ok is True
    path, payload = mc.posts[0]
    data = dict(payload)
    assert data["message[body]"] == "No problem!"
    assert data["message[conversation_id]"] == "9001"
    assert data["authenticity_token"] == "tok"


async def test_reply_echo_is_not_mirrored_back(db):
    service, forum, mc, _ = _service(db)
    await service.scan()
    thread = next(t for t in forum.threads if "#9001" in t.name)
    before = len(thread.messages)
    await service.reply_from_thread(thread.id, "No problem!")
    # The game now shows our reply as the newest message.
    mc.conversations["9001"] = CONV_9001_HTML.replace(
        '<div class="well" data-message-time="2026-07-11T10:00:00+00:00">',
        '<div class="well" data-message-time="2026-07-11T13:00:00+00:00">'
        '<a href="/profile/999">FRA-Bot</a><p>No problem!</p></div>'
        '<div class="well" data-message-time="2026-07-11T10:00:00+00:00">',
    )
    summary = await service.scan()
    assert summary["mirrored"] == 0  # echo suppressed
    assert len(thread.messages) == before


async def test_unconfirmed_reply_reports_failure(db):
    service, forum, mc, _ = _service(db)
    await service.scan()
    thread = next(t for t in forum.threads if "#9001" in t.name)
    mc.reply_response = CONV_9001_HTML  # re-rendered form, no confirmation
    ok, detail = await service.reply_from_thread(thread.id, "hello?")
    assert ok is False
    assert "confirm" in detail


async def test_reply_honours_dry_run(db):
    service, forum, mc, _ = _service(db, _cfg(dry_run=True))
    await service.scan()
    thread = next(t for t in forum.threads if "#9001" in t.name)
    ok, detail = await service.reply_from_thread(thread.id, "test")
    assert ok is False and "dry-run" in detail
    assert mc.posts == []  # nothing went to the game


async def test_reply_in_unlinked_thread_is_refused(db):
    service, _, _, _ = _service(db)
    ok, detail = await service.reply_from_thread(123456, "hi")
    assert ok is False and "not linked" in detail


async def test_deleted_thread_is_recreated_on_new_activity(db):
    service, forum, mc, bot = _service(db)
    await service.scan()
    victim = next(t for t in forum.threads if "#9001" in t.name)
    forum.threads.remove(victim)
    bot._channels.pop(victim.id)
    # New in-game reply arrives -> thread is gone -> recreate with history.
    mc.conversations["9001"] = CONV_9001_HTML.replace(
        '<div class="well" data-message-time="2026-07-11T10:00:00+00:00">',
        '<div class="well" data-message-time="2026-07-11T12:00:00+00:00">'
        '<a href="/profile/111">Alex1129</a><p>Are you there?</p></div>'
        '<div class="well" data-message-time="2026-07-11T10:00:00+00:00">',
    )
    summary = await service.scan()
    assert any("#9001" in t.name for t in forum.threads)
    row = await DmMirrorRepo(db).get("9001")
    assert row["thread_id"] != victim.id


async def test_unconfigured_forum_reports(db):
    cfg = _cfg()
    cfg.discord.channels.dm_mirror = 0
    bot = FakeBot()
    service = DmMirrorService(cfg, FakeMC(), db, bot)
    summary = await service.scan()
    assert summary["error"]


async def _seed_member(db, mc_user_id, name):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '2026-01-01T00:00:00', '2026-07-01T00:00:00')",
        (mc_user_id, name),
    )


async def test_send_new_resolves_roster_name_and_mirrors_immediately(db):
    service, forum, mc, _ = _service(db)
    await _seed_member(db, 111, "Alex1129")

    # The game reports the new conversation as id 777 via the redirect.
    async def post_form(path, data, **kwargs):
        mc.posts.append((path, mc._pairs(data)))
        return (200, SENT_HTML, "https://www.missionchief.com/messages/777")

    mc.post_form = post_form
    mc.conversations["777"] = CONV_9002_HTML.replace("9002", "777").replace(
        "4m1rudin", "Alex1129"
    )
    # Case-insensitive roster match ("alex1129" -> "Alex1129").
    result = await service.send_new("alex1129", "Hello", "Welcome to FRA!")
    assert result["ok"] is True
    assert result["thread"] is not None
    assert "#777" in result["thread"].name
    # Sent to the game with the exact roster casing.
    _, payload = mc.posts[0]
    assert dict(payload)["message[recipient]"] == "Alex1129"
    # The mapping exists, so thread replies work right away.
    assert (await DmMirrorRepo(db).get("777"))["thread_id"] == result["thread"].id


async def test_send_new_refuses_non_members_with_suggestions(db):
    service, _, mc, _ = _service(db)
    await _seed_member(db, 111, "Alex1129")
    result = await service.send_new("Alex1130", "Hi", "Body")
    assert result["ok"] is False
    assert "not an alliance member" in result["detail"]
    assert "Alex1129" in result["detail"]  # did-you-mean
    assert mc.posts == []


async def test_send_new_honours_dry_run(db):
    service, _, mc, _ = _service(db, _cfg(dry_run=True))
    await _seed_member(db, 111, "Alex1129")
    result = await service.send_new("Alex1129", "Hi", "Body")
    assert result["ok"] is False and "dry-run" in result["detail"]
    assert mc.posts == []


def test_extract_conversation_id_paths():
    from fra_bot.mc.messages import extract_conversation_id

    assert extract_conversation_id("", "https://x/messages/777") == "777"
    assert extract_conversation_id(
        '<input name="message[conversation_id]" value="88"/>'
    ) == "88"
    assert extract_conversation_id(
        '<a href="/messages/99">conversation</a>'
    ) == "99"
    assert extract_conversation_id("<p>nothing</p>", "https://x/messages/new") is None


async def test_reply_by_conversation_id(db):
    """The panel's Reply button routes by conversation id directly."""
    service, forum, mc, _ = _service(db)
    await service.scan()
    ok, _detail = await service.reply_to_conversation("9001", "Direct reply")
    assert ok is True
    data = dict(mc.posts[0][1])
    assert data["message[conversation_id]"] == "9001"
    assert data["message[body]"] == "Direct reply"


def test_panel_exposes_stable_custom_ids():
    from fra_bot.cogs.dm_mirror import (
        PANEL_REPLY_ID,
        PANEL_SCAN_ID,
        PANEL_SEND_ID,
        DmMirrorCog,
        DmPanelView,
    )

    cog = DmMirrorCog.__new__(DmMirrorCog)
    embed = DmMirrorCog.panel_embed(cog)
    assert embed.title == "📬 MissionChief messages"
    view = DmPanelView(cog)
    ids = {child.custom_id for child in view.children}
    # Stable ids: persistent buttons must survive restarts.
    assert ids == {PANEL_SEND_ID, PANEL_SCAN_ID, PANEL_REPLY_ID}


def test_panel_keeper_maintains_the_dm_panel():
    from fra_bot.cogs.panels import PanelKeeperCog

    keeper = PanelKeeperCog.__new__(PanelKeeperCog)
    keeper.bot = SimpleNamespace(
        cfg=SimpleNamespace(
            automation=SimpleNamespace(
                mission=SimpleNamespace(panel_channel_id=1)
            ),
            discord=SimpleNamespace(
                channels=SimpleNamespace(
                    request_panel=2, member_panel=3, dm_panel=4
                )
            ),
        )
    )
    specs = {spec.key: spec.channel_id() for spec in keeper._specs()}
    assert specs["dms"] == 4


def test_settings_expose_the_new_keys():
    from fra_bot.core import settings as rt

    assert rt.resolve("dm_mirror").path == "discord.channels.dm_mirror"
    assert (
        rt.resolve("dm_mirror.enabled").path == "automation.dm_mirror.enabled"
    )
    assert (
        rt.resolve("dm_mirror.interval").path == "automation.dm_mirror.interval"
    )
