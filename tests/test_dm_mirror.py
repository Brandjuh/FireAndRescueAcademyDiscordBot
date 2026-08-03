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
<div class="panel panel-default">
  <div class="panel-heading">System messages</div>
  <div class="panel-body system_messages_container">
    <table class="table table-condensed table-striped"><tbody>
      <tr>
        <td class="system-message-row"><div class="system-message-content">New</div></td>
        <td class="system-message-row"><div class="system-message-content">
          <a href="/messages/system_message/5">Daily reward</a>
        </div></td>
      </tr>
    </tbody></table>
  </div>
</div>
<form action="/messages/trash" method="post">
  <input id="current_box" name="current_box" type="hidden" value="inbox"/>
  <table class="table table-striped"><tbody>
    <tr>
      <td><input class="delete_multiple_checkbox" name="conversations[]" type="checkbox" value="9001"/></td>
      <td>New</td>
      <td><a href="/messages/9001">Alex1129</a></td>
      <td><a href="/messages/9001">Question about tax</a></td>
    </tr>
    <tr>
      <td><input class="delete_multiple_checkbox" name="conversations[]" type="checkbox" value="9002"/></td>
      <td></td>
      <td><a href="/messages/9002">4m1rudin</a></td>
      <td><a href="/messages/9002">Reminder: Please set your alliance donation to 5%</a></td>
    </tr>
  </tbody></table>
</form>
"""

# The verified live layout (from `!fra dump /messages`): system messages
# in their own panel OUTSIDE the inbox form, two cells per row (a "New"
# marker + the subject link), no checkbox / sender / date.
REAL_PAGE_HTML = """
<ol class="breadcrumb"><li class="active">Inbox</li></ol>
<div class="panel panel-default ">
  <div class="panel-heading">System messages</div>
  <div class="panel-body system_messages_container">
    <div class="system_messages_content_container" style="max-height: none">
      <table class="table table-condensed table-striped"><tbody>
        <tr>
          <td class="system-message-row"><div class="system-message-content" style="height: 30px">New</div></td>
          <td class="system-message-row"><div class="system-message-content" style="height: 30px">
            <a href="/messages/system_message/787">\U0001f6a8Alliance Challenge \U0001f6a8</a>
          </div></td>
        </tr>
        <tr>
          <td class="system-message-row"><div class="system-message-content" style="height: 30px">New</div></td>
          <td class="system-message-row"><div class="system-message-content" style="height: 30px">
            <a href="/messages/system_message/782">\U0001f33b Summer Event Part 2 ☀️</a>
          </div></td>
        </tr>
        <tr>
          <td class="system-message-row"><div class="system-message-content" style="height: 30px"></div></td>
          <td class="system-message-row"><div class="system-message-content" style="height: 30px">
            <a href="/messages/system_message/770">\U0001f31e\U0001f6a8Summer Event\U0001f6a8\U0001f31e</a>
          </div></td>
        </tr>
      </tbody></table>
    </div>
  </div>
</div>
<form accept-charset="UTF-8" action="/messages/trash" method="post">
  <input id="current_box" name="current_box" type="hidden" value="inbox" />
  <table class="table table-striped">
    <thead><tr><th></th><th></th><th>Sender</th><th>Subject</th></tr></thead>
    <tbody>
      <tr>
        <td><input class="delete_multiple_checkbox" id="" name="conversations[]" type="checkbox" value="240834" /></td>
        <td></td>
        <td><a href="/messages/240834">FawnsathBB</a></td>
        <td><a href="/messages/240834">Reminder: Please set your alliance donation to 5%</a></td>
      </tr>
      <tr>
        <td><input class="delete_multiple_checkbox" id="" name="conversations[]" type="checkbox" value="219650" /></td>
        <td></td>
        <td><a href="/alliances/1621">Fire &amp; Rescue Academy</a></td>
        <td><a href="/messages/219650">Welcome to the Fire &amp; Rescue Academy. PLEASE READ</a></td>
      </tr>
    </tbody>
  </table>
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

def test_parse_inbox_rows_and_flags_system_messages():
    rows = mailbox.parse_inbox(INBOX_HTML)
    # Page-wide system scan runs first, then the inbox form.
    assert [r.conversation_id for r in rows] == ["5", "9001", "9002"]
    assert [r.is_system for r in rows] == [True, False, False]
    system = rows[0]
    assert system.subject == "Daily reward"
    assert system.is_new is True
    conv = rows[1]
    assert conv.sender == "Alex1129" and conv.is_new is True
    assert conv.subject == "Question about tax"
    assert rows[2].sender == "4m1rudin" and rows[2].is_new is False
    assert mailbox.parse_inbox("<html>no form</html>") == []


def test_parse_inbox_matches_the_verified_live_layout():
    # Regression guard: the REAL page (from `!fra dump /messages`) keeps
    # system messages in their own panel OUTSIDE the inbox form — the
    # original parser only walked the form and saw zero of them.
    rows = mailbox.parse_inbox(REAL_PAGE_HTML)
    system = [r for r in rows if r.is_system]
    assert [r.conversation_id for r in system] == ["787", "782", "770"]
    assert system[0].subject == "\U0001f6a8Alliance Challenge \U0001f6a8"
    assert [r.is_new for r in system] == [True, True, False]
    convs = [r for r in rows if not r.is_system]
    assert [r.conversation_id for r in convs] == ["240834", "219650"]
    assert convs[1].sender == "Fire & Rescue Academy"


SYSTEM_MSG_HTML = """
<div class="container">
  <div class="well">
    <p>Dear player,</p>
    <p>You received your daily reward: 500 coins.</p>
  </div>
</div>
"""

SYSTEM_MSG_PLAIN_HTML = """
<div id="content">
  <h1>Maintenance</h1>
  Servers restart at 04:00.
  <script>ignore()</script>
</div>
"""


def test_parse_system_message_prefers_well_paragraphs():
    body = mailbox.parse_system_message(SYSTEM_MSG_HTML)
    assert body == "Dear player,\nYou received your daily reward: 500 coins."
    # No profile link required — that's exactly what parse_conversation
    # would demand (and system messages don't have one).
    assert mailbox.parse_conversation(SYSTEM_MSG_HTML) == []


def test_parse_system_message_falls_back_to_main_content():
    body = mailbox.parse_system_message(SYSTEM_MSG_PLAIN_HTML)
    assert "Servers restart at 04:00" in body
    assert "ignore()" not in body
    assert mailbox.parse_system_message("<html></html>") == ""


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
        self.system_pages = {"5": SYSTEM_MSG_HTML}
        self.posts = []
        self.fetched = []
        self.reply_response = SENT_HTML

    def url(self, path):
        return "https://www.missionchief.com" + path

    async def fetch_page(self, path, **kwargs):
        self.fetched.append(path)
        if path == "/messages":
            return self.inbox_html
        if path.rstrip("/").endswith("/messages/new"):
            return COMPOSE_HTML
        if "/system_message/" in path:
            return self.system_pages[path.rsplit("/", 1)[-1]]
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


def _cfg(dry_run=False, *, dm_mirror=800, system_channel=0, system_role=0):
    return SimpleNamespace(
        discord=SimpleNamespace(
            channels=SimpleNamespace(
                dm_mirror=dm_mirror, system_messages=system_channel,
            ),
            admin_role_ids=(1,),
            staff_role_ids=(2,),
            system_message_role_id=system_role,
        ),
        automation=SimpleNamespace(
            dry_run=dry_run,
            dm_mirror=SimpleNamespace(enabled=True, interval=15),
        ),
    )


class FakeTextChannel:
    """The system-message channel: a plain sendable channel (no threads)."""

    def __init__(self, channel_id, bot):
        self.id = channel_id
        self.sent = []  # (content, embed, allowed_mentions)
        bot.add_channel(self)

    async def send(self, content=None, embed=None, allowed_mentions=None):
        self.sent.append((content, embed, allowed_mentions))
        return FakeMessage(content)


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
    cfg = cfg or _cfg()
    if getattr(cfg.discord.channels, "system_messages", 0):
        FakeTextChannel(cfg.discord.channels.system_messages, bot)
    service = DmMirrorService(cfg, mc, db, bot)
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


async def test_mirror_now_creates_the_thread_once(db):
    """Send-time mirroring (tax warnings and friends): the conversation
    thread appears immediately, and a second call reuses it."""
    service, forum, _, _ = _service(db)
    service._mc.conversations["777"] = CONV_9002_HTML.replace("9002", "777")
    thread = await service.mirror_now("777", "4m1rudin", "Reminder")
    assert thread is not None and "#777" in thread.name
    assert len(forum.threads) == 1
    again = await service.mirror_now("777", "4m1rudin", "Reminder")
    assert again is not None and again.id == thread.id
    assert len(forum.threads) == 1  # no duplicate


def test_settings_expose_the_new_keys():
    from fra_bot.core import settings as rt

    assert rt.resolve("dm_mirror").path == "discord.channels.dm_mirror"
    assert (
        rt.resolve("dm_mirror.enabled").path == "automation.dm_mirror.enabled"
    )
    assert (
        rt.resolve("dm_mirror.interval").path == "automation.dm_mirror.interval"
    )


# ---------------------------------------------------------------------------
# System messages → the system-message channel
# ---------------------------------------------------------------------------

async def test_system_message_posts_embed_once_and_never_mirrors(db):
    service, forum, mc, bot = _service(db, _cfg(system_channel=900))
    summary = await service.scan()
    channel = bot.get_channel(900)
    assert summary["system_posted"] == 1
    assert len(channel.sent) == 1
    content, embed, allowed = channel.sent[0]
    assert content is None                        # role id 0: no mention line
    assert allowed.roles is False or allowed.roles == []
    assert embed.title == "📢 System message — Daily reward"
    assert "daily reward: 500 coins" in embed.description
    assert "System message #5" in embed.footer.text
    # It never became a mirrored conversation thread.
    assert all("#5" not in t.name for t in forum.threads)
    # Dedupe: the next scan posts nothing new.
    summary = await service.scan()
    assert summary.get("system_posted", 0) == 0
    assert len(channel.sent) == 1


async def test_system_message_mention_structure_above_the_embed(db):
    service, _, _, bot = _service(
        db, _cfg(system_channel=900, system_role=4242),
    )
    await service.scan()
    content, _, allowed = bot.get_channel(900).sent[0]
    assert content == "<@&4242>"                  # above/outside the embed
    assert [r.id for r in allowed.roles] == [4242]


async def test_system_channel_off_keeps_ignoring(db):
    service, _, mc, _ = _service(db)              # system_messages = 0
    summary = await service.scan()
    assert summary.get("system_posted", 0) == 0
    # The system-message page is never opened (no needless traffic, and
    # no in-game mark-as-read side effect).
    assert all("/system_message/" not in p for p in mc.fetched)


async def test_system_pass_runs_even_without_a_mirror_forum(db):
    service, _, _, bot = _service(
        db, _cfg(dm_mirror=0, system_channel=900),
    )
    summary = await service.scan()
    assert summary["error"] is not None           # mirror still unconfigured
    assert summary["system_posted"] == 1
    assert len(bot.get_channel(900).sent) == 1


async def test_failed_system_post_retries_next_scan(db):
    service, _, _, bot = _service(db, _cfg(system_channel=900))
    channel = bot.get_channel(900)
    original_send = channel.send

    async def broken_send(**kwargs):
        raise discord.HTTPException(
            SimpleNamespace(status=500, reason="boom"), "boom"
        )

    channel.send = broken_send
    summary = await service.scan()
    assert summary["system_failed"] == 1 and summary["system_posted"] == 0
    # Not recorded — the next scan retries and succeeds.
    channel.send = original_send
    summary = await service.scan()
    assert summary["system_posted"] == 1
    assert len(channel.sent) == 1


# ---------------------------------------------------------------------------
# Live-page tolerance + observability (system messages missing in prod)
# ---------------------------------------------------------------------------

def test_parse_inbox_system_link_variants():
    # The page-wide scan copes with absolute hrefs, links outside any
    # table, and duplicates of the same id (posted once).
    html = """
    <div class="system_messages_container">
      <a href="https://www.missionchief.com/messages/system_message/77">Server maintenance</a>
      <a href="/messages/system_message/77">Server maintenance</a>
    </div>
    """
    rows = mailbox.parse_inbox(html)
    assert [r.conversation_id for r in rows] == ["77"]
    assert rows[0].is_system and rows[0].subject == "Server maintenance"


async def test_scan_summary_always_reports_system_state(db):
    service, _, _, _ = _service(db, _cfg(system_channel=900))
    summary = await service.scan()
    assert summary["system_configured"] is True
    assert summary["system_seen"] == 1
    assert summary["system_posted"] == 1
    assert "system messages: 1 in the inbox, 1 posted" in summary["lines"][0]
    # Second scan: still spelled out, so "0 posted" is visibly distinct
    # from "feature broken".
    summary = await service.scan()
    assert "system messages: 1 in the inbox, 0 posted" in summary["lines"][0]


async def test_unreachable_system_channel_warns_instead_of_silence(db):
    bot = FakeBot()
    FakeForum(800, bot)
    mc = FakeMC()
    # Channel 901 configured but never added to the bot: unreachable.
    service = DmMirrorService(_cfg(system_channel=901), mc, db, bot)
    summary = await service.scan()
    assert summary["system_warning"] is not None
    assert "not reachable" in summary["system_warning"]
    assert summary["system_failed"] == 1        # the seen row went nowhere
    assert any("not reachable" in line for line in summary["lines"])
    # Nothing recorded — once the channel works the message still posts.
    FakeTextChannel(901, bot)
    summary = await service.scan()
    assert summary["system_posted"] == 1 and summary["system_warning"] is None


async def test_status_lines_show_system_channel_state(db):
    service, _, _, _ = _service(db, _cfg(system_channel=900))
    await service.scan()
    lines = await service.status_lines()
    system_line = next(line for line in lines if "system messages" in line)
    assert "<#900>" in system_line and "1 posted so far" in system_line
    # Feature off → says so.
    service_off, _, _, _ = _service(db, _cfg())
    lines = await service_off.status_lines()
    assert any("off (" in line for line in lines if "system messages" in line)
