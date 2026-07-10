"""Panel keeper: panels place themselves, refresh in place on text
changes, survive deletion, move with the config, and never duplicate."""

from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from fra_bot.cogs.panels import PanelKeeperCog, PanelSpec, panel_digest
from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo


class FakeMessage:
    _next_id = 1000

    def __init__(self, channel, embed, author_id):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.embeds = [embed]
        self.author = SimpleNamespace(id=author_id)
        self.edits = 0
        self.deleted = False

    async def edit(self, *, embed=None, view=None):
        self.edits += 1
        if embed is not None:
            self.embeds = [embed]

    async def delete(self):
        self.deleted = True
        if self in self.channel.messages:
            self.channel.messages.remove(self)


class FakeChannel:
    def __init__(self, channel_id, bot_id=1):
        self.id = channel_id
        self._bot_id = bot_id
        self.messages: list[FakeMessage] = []

    async def send(self, embed=None, view=None):
        msg = FakeMessage(self, embed, self._bot_id)
        self.messages.append(msg)
        return msg

    async def fetch_message(self, message_id):
        for msg in self.messages:
            if msg.id == message_id:
                return msg
        raise discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "gone"
        )

    def history(self, limit=None):
        msgs = list(reversed(self.messages))[: limit or len(self.messages)]

        class _Iter:
            def __init__(self, items):
                self._items = iter(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration

        return _Iter(msgs)


class FakePanelCog:
    def __init__(self, title="🧪 Test panel", description="press the button"):
        self.title = title
        self.description = description

    def panel_embed(self):
        return discord.Embed(title=self.title, description=self.description)

    def panel_view(self):
        return None  # views need a running client; None is send()-compatible


class FakeBot:
    def __init__(self, db, channel):
        self.db = db
        self.user = SimpleNamespace(id=1)
        self._channels = {channel.id: channel}
        self._cogs = {"TestCog": FakePanelCog()}

    def add_channel(self, channel):
        self._channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_cog(self, name):
        return self._cogs.get(name)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "panels.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _keeper(db, channel, *, channel_id=None):
    keeper = PanelKeeperCog.__new__(PanelKeeperCog)
    keeper.bot = FakeBot(db, channel)
    keeper._state = StateRepo(db)
    target = channel_id if channel_id is not None else channel.id
    keeper._specs = lambda: [  # type: ignore[method-assign]
        PanelSpec("test", "TestCog", lambda: target)
    ]
    return keeper


async def test_missing_panel_is_posted_and_tracked(db):
    channel = FakeChannel(555)
    keeper = _keeper(db, channel)
    assert await keeper.ensure("test") == "posted"
    assert len(channel.messages) == 1
    # Second sweep: nothing to do.
    assert await keeper.ensure("test") == "ok"
    assert len(channel.messages) == 1


async def test_deleted_panel_is_reposted(db):
    channel = FakeChannel(555)
    keeper = _keeper(db, channel)
    await keeper.ensure("test")
    channel.messages.clear()                     # someone deleted it
    assert await keeper.ensure("test") == "posted"
    assert len(channel.messages) == 1


async def test_changed_text_edits_in_place(db):
    channel = FakeChannel(555)
    keeper = _keeper(db, channel)
    await keeper.ensure("test")
    original = channel.messages[0]
    keeper.bot._cogs["TestCog"] = FakePanelCog(description="NEW text")
    assert await keeper.ensure("test") == "updated"
    assert original.edits == 1                   # same message, refreshed
    assert len(channel.messages) == 1
    assert channel.messages[0].embeds[0].description == "NEW text"
    # And the new hash sticks.
    assert await keeper.ensure("test") == "ok"


async def test_strays_are_cleaned_up_on_post(db):
    channel = FakeChannel(555)
    # Two hand-posted old copies of the same panel (same title, by the bot).
    embed = FakePanelCog().panel_embed()
    await channel.send(embed=embed)
    await channel.send(embed=embed)
    keeper = _keeper(db, channel)
    assert await keeper.ensure("test") == "posted"
    assert len(channel.messages) == 1            # strays removed


async def test_foreign_messages_survive_cleanup(db):
    channel = FakeChannel(555)
    other = FakeMessage(channel, FakePanelCog().panel_embed(), author_id=999)
    channel.messages.append(other)               # same title, NOT the bot
    keeper = _keeper(db, channel)
    await keeper.ensure("test")
    assert other in channel.messages


async def test_panel_moves_with_the_config(db):
    old_channel = FakeChannel(555)
    keeper = _keeper(db, old_channel)
    await keeper.ensure("test")
    assert len(old_channel.messages) == 1

    new_channel = FakeChannel(777)
    keeper.bot.add_channel(new_channel)
    keeper._specs = lambda: [PanelSpec("test", "TestCog", lambda: 777)]
    assert await keeper.ensure("test") == "posted"
    assert old_channel.messages == []            # old panel removed
    assert len(new_channel.messages) == 1


async def test_unconfigured_panel_is_skipped(db):
    channel = FakeChannel(555)
    keeper = _keeper(db, channel, channel_id=0)
    assert await keeper.ensure("test") == "skipped"
    assert channel.messages == []


async def test_force_reposts_and_stays_single(db):
    channel = FakeChannel(555)
    keeper = _keeper(db, channel)
    await keeper.ensure("test")
    assert await keeper.ensure("test", channel=channel, force=True) == "posted"
    assert len(channel.messages) == 1


def test_panel_digest_tracks_title_and_description():
    a = discord.Embed(title="T", description="one")
    b = discord.Embed(title="T", description="two")
    assert panel_digest(a) != panel_digest(b)
    assert panel_digest(a) == panel_digest(discord.Embed(title="T", description="one"))
