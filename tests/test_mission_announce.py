"""Where a mission outcome ends up. The request channel is a member
channel, so the default is to post nowhere at all — but the rows must
still drain, or the day someone flips the switch the queue floods."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.missions import MissionsCog
from fra_bot.config import _announce_mode
from fra_bot.db.database import Database
from fra_bot.db.repos import MissionsRepo

REQUEST_CHANNEL = 1421627971831070730


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "announce.sqlite3")
    await database.connect()
    yield database
    await database.close()


class FakeChannel:
    def __init__(self, name):
        self.name = name
        self.embeds = []

    async def send(self, embed=None, **kwargs):
        self.embeds.append(embed)


class FakeBot(SimpleNamespace):
    async def wait_until_ready(self):
        await asyncio.Event().wait()  # park the publisher loop forever

    def job_lock(self, name):
        locks = self.__dict__.setdefault("_locks", {})
        return locks.setdefault(name, asyncio.Lock())

    def channel_for(self, key):
        return self.channels.get(key)

    def get_channel(self, channel_id):
        return self.channels.get(int(channel_id))


def _cog(db, mode):
    cfg = SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=True,
            mission=SimpleNamespace(
                enabled=True, min_contribution_rate=5.0, announce=mode,
            ),
        ),
    )
    bot = FakeBot(
        db=db, cfg=cfg, missions_service=None,
        channels={"admin_log": FakeChannel("admin"),
                  REQUEST_CHANNEL: FakeChannel("request")},
    )
    cog = MissionsCog(bot)
    return cog, bot


async def _queued(db, *, channel_id=REQUEST_CHANNEL):
    repo = MissionsRepo(db)
    mission_id = await repo.create(
        source="discord", kind="large", mission_source="preset",
        location_text="NYC", channel_id=channel_id,
        requester_name="Tester",
    )
    # 'waiting' is the "⏳ Mission queued" notice the request channel kept
    # showing; 'done' is "🚨 Mission started".
    await repo.set_status(mission_id, "waiting", detail="queued", announce=True)
    return repo, mission_id


async def test_announce_off_drains_without_posting(db):
    repo, mission_id = await _queued(db)
    cog, bot = _cog(db, "off")
    try:
        assert await repo.pending_announcements()
        await cog._publish_outcomes()
    finally:
        cog.cog_unload()
    assert await repo.pending_announcements() == []
    assert bot.channels["admin_log"].embeds == []
    assert bot.channels[REQUEST_CHANNEL].embeds == []


async def test_announce_admin_keeps_it_out_of_the_member_channel(db):
    repo, mission_id = await _queued(db)
    cog, bot = _cog(db, "admin")
    try:
        await cog._publish_outcomes()
    finally:
        cog.cog_unload()
    assert await repo.pending_announcements() == []
    assert len(bot.channels["admin_log"].embeds) == 1
    assert bot.channels[REQUEST_CHANNEL].embeds == []


async def test_announce_request_posts_back_where_it_came_from(db):
    repo, mission_id = await _queued(db)
    cog, bot = _cog(db, "request")
    try:
        await cog._publish_outcomes()
    finally:
        cog.cog_unload()
    assert await repo.pending_announcements() == []
    assert len(bot.channels[REQUEST_CHANNEL].embeds) == 1
    assert bot.channels["admin_log"].embeds == []


async def test_announce_request_falls_back_to_admin_without_a_channel(db):
    repo, mission_id = await _queued(db, channel_id=None)
    cog, bot = _cog(db, "request")
    try:
        await cog._publish_outcomes()
    finally:
        cog.cog_unload()
    assert len(bot.channels["admin_log"].embeds) == 1


def test_announce_mode_normalises_yamls_boolean_off():
    # YAML reads a bare `off` / `no` as the boolean False; taken literally
    # that would fall through to "post it somewhere" — the opposite of what
    # the line says.
    assert _announce_mode(False) == "off"
    assert _announce_mode("off") == "off"
    assert _announce_mode(None) == "off"
    assert _announce_mode("nonsense") == "off"
    assert _announce_mode("admin") == "admin"
    assert _announce_mode("REQUEST") == "request"
    assert _announce_mode(True) == "request"
