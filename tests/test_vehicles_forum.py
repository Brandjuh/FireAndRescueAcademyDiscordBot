"""Vehicles-database forum: the embed, tag application, and the sync loop
(create / edit-in-place / dedup / adopt / orphan-reclaim / cap / announce /
wipe). The catalog fetch is monkeypatched, so no network is touched."""

import json
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import VehiclesForumRepo
from fra_bot.mc import vehicles_catalog as catalog
from fra_bot.services.vehicles_forum import (
    STATE_BACKFILL_DONE,
    VehiclesForumService,
    build_vehicle_embed,
    thread_key,
)


# ---------------------------------------------------------------------------
# Fixture catalog (already-normalised records, the shape fetch_catalog yields)
# ---------------------------------------------------------------------------

def _veh(vid, name, **over):
    base = {
        "id": vid, "name": name, "credits": 5000, "coins": 25,
        "staff_min": 1, "staff_max": 6, "buildings": ["Fire station"],
        "water_tank": None, "foam_tank": None, "pump_capacity": None,
        "pump_type": None, "equipment_capacity": None, "is_trailer": False,
        "special": None, "trainings": [],
    }
    base.update(over)
    return base


def _vehicles():
    return [
        _veh(0, "Type 1 fire engine", water_tank=750, pump_type="fire"),
        _veh(9, "HazMat truck", is_trailer=True,
             trainings=["Fire Station: HazMat"], buildings=["Fire station"]),
        _veh(27, "Ambulance", buildings=["Ambulance station"], water_tank=None),
    ]


# ---------------------------------------------------------------------------
# Discord fakes (mirror the server-side rules the missions forum hit live)
# ---------------------------------------------------------------------------

def _http_400(message):
    return discord.HTTPException(
        SimpleNamespace(status=400, reason="Bad Request"), message
    )


class FakeMessage:
    def __init__(self, message_id, embed):
        self.id = message_id
        self.embeds = [embed]
        self.edits = 0

    async def edit(self, *, embed=None):
        self.edits += 1
        if embed is not None:
            self.embeds = [embed]


class FakeThread:
    _next_id = 7000

    def __init__(self, name, embed, applied_tags, bot, forum=None):
        FakeThread._next_id += 1
        self.id = FakeThread._next_id
        self.name = name
        self.archived = False
        self.applied_tags = list(applied_tags or [])
        self.starter = FakeMessage(self.id, embed)
        self.starter_deleted = False
        self.jump_url = f"https://discord.com/channels/1/{self.id}"
        self.messages = []
        self._bot = bot
        self._forum = forum
        bot.add_channel(self)

    async def delete(self):
        self._bot.remove_channel(self.id)
        if self._forum is not None and self in self._forum.threads:
            self._forum.threads.remove(self)

    async def fetch_message(self, message_id):
        assert message_id == self.id
        if self.starter_deleted:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "gone"
            )
        return self.starter

    async def send(self, content=None, *, embed=None, allowed_mentions=None):
        self.messages.append((content, embed))
        return FakeMessage(self.id + 900000, embed)

    async def edit(self, **kwargs):
        self.name = kwargs.get("name", self.name)
        self.archived = kwargs.get("archived", self.archived)
        if "applied_tags" in kwargs:
            self.applied_tags = list(kwargs["applied_tags"])
        return self


class FakeForum:
    def __init__(self, channel_id, bot):
        self.id = channel_id
        self.available_tags = []
        self.flags = SimpleNamespace(require_tag=False)
        self.threads = []
        self._bot = bot
        bot.add_channel(self)

    async def edit(self, **kwargs):
        if kwargs.get("require_tag"):
            settable = [
                t for t in self.available_tags
                if not getattr(t, "moderated", False)
            ]
            if not settable:
                raise _http_400(
                    "no tags available that can be set by non-moderators "
                    "(error code: 40066)"
                )
        if "available_tags" in kwargs:
            self.available_tags = list(kwargs["available_tags"])
        if "require_tag" in kwargs:
            self.flags.require_tag = kwargs["require_tag"]
        return self

    async def create_thread(self, *, name, embed=None, applied_tags=None, reason=None):
        if self.flags.require_tag and not applied_tags:
            raise _http_400("a tag is required (error code: 40067)")
        thread = FakeThread(name, embed, applied_tags, self._bot, forum=self)
        self.threads.append(thread)
        return SimpleNamespace(thread=thread, message=thread.starter)

    def archived_threads(self, *, limit=None):
        async def _iter():
            return
            yield  # pragma: no cover

        return _iter()


class FakeAnnounce:
    def __init__(self, channel_id, bot):
        self.id = channel_id
        self.sent = []
        bot.add_channel(self)

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class FakeBot:
    def __init__(self):
        self._channels = {}

    def add_channel(self, channel):
        self._channels[channel.id] = channel

    def remove_channel(self, channel_id):
        self._channels.pop(channel_id, None)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self._channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "gone"
            )
        return channel


def _cfg(**overrides):
    auto = SimpleNamespace(
        enabled=True, sync_time="04:30", announce_new=False, max_posts_per_run=100
    )
    for key, value in overrides.items():
        setattr(auto, key, value)
    return SimpleNamespace(
        discord=SimpleNamespace(
            channels=SimpleNamespace(vehicles_forum=900, vehicle_announce=901),
        ),
        automation=SimpleNamespace(vehicles_forum=auto),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "vforum.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _service(db, vehicles=None, cfg=None):
    cfg = cfg or _cfg()
    bot = FakeBot()
    forum = FakeForum(900, bot)
    announce = FakeAnnounce(901, bot)
    service = VehiclesForumService(cfg, db, bot)
    service.post_delay = 0
    service.batch_delay = 0
    data = _vehicles() if vehicles is None else vehicles

    async def _fetch():
        return [dict(v) for v in data]

    service._fetch_catalog = _fetch
    return service, forum, announce, bot


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------

def test_embed_renders_the_core_fields():
    embed = build_vehicle_embed(_veh(0, "Type 1 fire engine",
                                     water_tank=750, pump_type="fire"))
    assert "Type 1 fire engine" in embed.title
    names = {f.name: f.value for f in embed.fields}
    assert "5,000 credits" in names["💰 Price"] and "25 coins" in names["💰 Price"]
    assert names["👥 Crew"] == "1–6"
    assert "Fire station" in names["🏢 Available at"]
    assert "Water" in names["🚰 Tanks & pump"]
    assert "veh-0" in embed.footer.text


def test_embed_notes_trailer_and_trainings():
    embed = build_vehicle_embed(_veh(9, "HazMat truck", is_trailer=True,
                                     trainings=["Fire Station: HazMat"]))
    names = {f.name: f.value for f in embed.fields}
    assert "🎓 Trainings required" in names
    assert "HazMat" in names["🎓 Trainings required"]
    assert "🚚 Trailer" in names


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

async def test_sync_creates_posts_tags_and_rows(db):
    service, forum, announce, _ = _service(db)
    summary = await service.sync()
    assert summary["created"] == 3 and summary["failed"] == 0
    assert len(forum.threads) == 3
    assert {t.name for t in forum.available_tags} == set(catalog.FORUM_TAG_EMOJI)
    assert forum.flags.require_tag is True
    for thread in forum.threads:
        assert thread.applied_tags
        assert thread_key(thread.name)
    assert await VehiclesForumRepo(db).count() == 3
    assert announce.sent == []  # initial fill never announces


async def test_tags_reflect_capabilities(db):
    service, forum, _, _ = _service(db)
    await service.sync()
    by_key = {thread_key(t.name): {tag.name for tag in t.applied_tags}
              for t in forum.threads}
    assert by_key["veh-0"] == {"Fire", "Water/Pump"}
    assert by_key["veh-9"] == {"Fire", "Training required", "Trailer"}
    assert by_key["veh-27"] == {"EMS"}


async def test_resync_unchanged_is_a_noop(db):
    service, forum, _, _ = _service(db)
    await service.sync()
    summary = await service.sync()
    assert summary["created"] == 0 and summary["updated"] == 0
    assert summary["skipped"] == 3
    assert len(forum.threads) == 3  # nothing reposted


async def test_data_change_edits_in_place_and_announces(db):
    service, forum, announce, _ = _service(db)
    await service.sync()  # backfill completes → future changes may announce
    # Price change on the fire engine.
    changed = _vehicles()
    changed[0]["credits"] = 9999

    async def _fetch():
        return [dict(v) for v in changed]

    service._fetch_catalog = _fetch
    service._cfg.automation.vehicles_forum.announce_new = True

    summary = await service.sync()
    assert summary["updated"] == 1 and summary["created"] == 0
    assert len(forum.threads) == 3  # edited in place, not reposted
    engine = next(t for t in forum.threads if thread_key(t.name) == "veh-0")
    assert engine.messages  # a "Vehicle updated" note was posted in-thread
    assert any("Vehicle updated" in (c or "") for c, _ in engine.messages)
    assert announce.sent  # bundled update announcement fired


async def test_format_bump_rerenders_without_announcing(db, monkeypatch):
    service, forum, announce, _ = _service(db)
    await service.sync()
    engine = next(t for t in forum.threads if thread_key(t.name) == "veh-0")
    engine.messages.clear()
    service._cfg.automation.vehicles_forum.announce_new = True
    # A format bump changes content_hash but NOT data_hash.
    monkeypatch.setattr(catalog, "FORMAT_VERSION", "vehicles-forum-v999")
    summary = await service.sync()
    assert summary["updated"] == 3  # every post re-rendered
    assert all(not t.messages for t in forum.threads)  # but no update notes
    assert announce.sent == []      # and no announcement storm


async def test_cap_limits_writes_and_defers_backfill(db):
    service, _, _, _ = _service(db)
    summary = await service.sync(limit=2)
    assert summary["created"] == 2 and summary["capped"] is True
    assert await service._state.get(STATE_BACKFILL_DONE) is None
    # The rest lands on the next run, and THEN the backfill completes.
    summary2 = await service.sync()
    assert summary2["created"] == 1
    assert await service._state.get(STATE_BACKFILL_DONE) is not None


async def test_active_thread_cap_stops_run_without_hammering(db):
    """Discord's 1000-active-thread guild wall must stop the run cleanly, not
    be counted as N per-vehicle failures that hammer the API every catch-up."""
    service, forum, _, _ = _service(db)
    original = forum.create_thread
    calls = {"n": 0}

    async def walled(**kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _http_400("Maximum number of active threads reached")
        return await original(**kwargs)

    forum.create_thread = walled
    summary = await service.sync()
    assert summary["created"] == 1
    assert summary["failed"] == 0
    assert summary["hit_active_cap"] is True
    assert calls["n"] == 2  # stopped at the wall, did not try the rest
    assert await service._state.get(STATE_BACKFILL_DONE) is None


async def test_self_heal_rearchives_stray_active_posts(db):
    service, forum, _, _ = _service(db)
    await service.sync()
    assert all(t.archived for t in forum.threads)
    for thread in forum.threads:  # simulate a past archive failure
        thread.archived = False
    summary = await service.sync()  # data unchanged → no writes
    assert summary["created"] == 0 and summary["updated"] == 0
    assert all(t.archived for t in forum.threads)  # self-healed


async def test_deleted_thread_is_recreated(db):
    service, forum, _, _ = _service(db)
    await service.sync()
    victim = next(t for t in forum.threads if thread_key(t.name) == "veh-0")
    await victim.delete()
    summary = await service.sync(force=True)
    assert summary["created"] >= 1  # the missing one was reposted
    assert any(thread_key(t.name) == "veh-0" for t in forum.threads)


async def test_adopt_rebuilds_mapping_from_titles(db):
    service, forum, _, bot = _service(db)
    await service.sync()
    # Wipe the DB mapping but keep the threads (simulates DB loss).
    for row in await VehiclesForumRepo(db).all():
        await VehiclesForumRepo(db).delete(row["vehicle_key"])
    assert await VehiclesForumRepo(db).count() == 0
    adopted = await service.adopt(forum)
    assert adopted == 3
    # A follow-up sync must not duplicate anything.
    await service.sync()
    assert len(forum.threads) == 3


async def test_orphan_active_thread_is_reclaimed(db):
    service, forum, _, bot = _service(db)
    # An untracked ACTIVE thread carrying our marker (crash before archive).
    FakeThread("Type 1 fire engine · #veh-0", None, [], bot, forum=forum)
    forum.threads.append(bot.get_channel(list(bot._channels)[-1]))
    summary = await service.sync()
    # veh-0 reclaimed (not duplicated); only 9 and 27 are fresh creates.
    keys = [thread_key(t.name) for t in forum.threads]
    assert keys.count("veh-0") == 1
    assert await VehiclesForumRepo(db).count() == 3


async def test_wipe_deletes_all_and_resets_backfill(db):
    service, forum, _, _ = _service(db)
    await service.sync()
    assert await service._state.get(STATE_BACKFILL_DONE) is not None
    summary = await service.wipe()
    assert summary["deleted"] == 3
    assert forum.threads == []
    assert await VehiclesForumRepo(db).count() == 0
    assert await service._state.get(STATE_BACKFILL_DONE) is None


async def test_unconfigured_forum_returns_error(db):
    cfg = _cfg()
    cfg.discord.channels.vehicles_forum = 0
    service, *_ = _service(db, cfg=cfg)
    summary = await service.sync()
    assert summary["error"] and "not configured" in summary["error"]


async def test_status_lines_report_state(db):
    service, _, _, _ = _service(db)
    await service.sync()
    lines = "\n".join(await service.status_lines())
    assert "tracked posts: 3" in lines
    assert "backfill: ✅" in lines
    assert "daily sync: on" in lines


# ---------------------------------------------------------------------------
# Building-name cache: a transient buildings.ts failure must NOT rewrite every
# label to "Building N" (which would flip every hash and fake a mass "updated")
# ---------------------------------------------------------------------------

async def test_building_names_are_cached_and_reused_on_failure(db, monkeypatch):
    service, *_ = _service(db)

    async def ok(session):
        return {0: "Fire station", 3: "Ambulance station"}

    monkeypatch.setattr(catalog, "fetch_building_names", ok)
    assert await service._building_names(object()) == {0: "Fire station", 3: "Ambulance station"}

    async def boom(session):
        raise ValueError("buildings.ts returned HTTP 500")

    monkeypatch.setattr(catalog, "fetch_building_names", boom)
    # Failure reuses the cached map, not {} — so labels stay real.
    assert await service._building_names(object()) == {0: "Fire station", 3: "Ambulance station"}


async def test_building_names_empty_fetch_falls_back_to_cache(db, monkeypatch):
    service, *_ = _service(db)

    async def ok(session):
        return {5: "Prison Cells"}

    monkeypatch.setattr(catalog, "fetch_building_names", ok)
    await service._building_names(object())

    async def empty(session):  # an empty parse is indistinguishable from a bad one
        return {}

    monkeypatch.setattr(catalog, "fetch_building_names", empty)
    assert await service._building_names(object()) == {5: "Prison Cells"}


async def test_building_names_failure_with_no_cache_degrades(db, monkeypatch):
    service, *_ = _service(db)

    async def boom(session):
        raise ValueError("down")

    monkeypatch.setattr(catalog, "fetch_building_names", boom)
    assert await service._building_names(object()) == {}  # cold start: honest degrade


async def test_buildings_failure_does_not_fake_a_vehicle_update(db, monkeypatch):
    """The core regression: once the real names are cached, a later buildings.ts
    failure must leave every hash unchanged — no "updated" notes, no announce."""
    real_names = {0: "Fire station", 13: "Fire station (Small station)",
                  3: "Ambulance station"}

    # A service whose sync uses the REAL _building_names + a stubbed vehicle fetch.
    cfg = _cfg(announce_new=True)
    bot = FakeBot()
    forum = FakeForum(900, bot)
    announce = FakeAnnounce(901, bot)
    service = VehiclesForumService(cfg, db, bot)
    service.post_delay = 0
    service.batch_delay = 0

    async def fetch_names_ok(session):
        return dict(real_names)

    async def fetch_vehicles(session, *, building_names=None):
        names = building_names or {}
        return [
            {**v, "buildings": [names.get(b, f"Building {b}")
                                for b in ({0: [0, 13], 9: [0], 27: [3]}[v["id"]])]}
            for v in _vehicles()
        ]

    monkeypatch.setattr(catalog, "fetch_building_names", fetch_names_ok)
    monkeypatch.setattr(catalog, "fetch_catalog", fetch_vehicles)

    await service.sync()  # backfill completes with REAL names cached
    for t in forum.threads:
        t.messages.clear()
    announce.sent.clear()

    # buildings.ts now fails; the cache must keep the real names, so nothing churns.
    async def fetch_names_fail(session):
        raise ValueError("buildings.ts 500")

    monkeypatch.setattr(catalog, "fetch_building_names", fetch_names_fail)
    summary = await service.sync()
    assert summary["updated"] == 0 and summary["created"] == 0
    assert summary["skipped"] == 3
    assert all(not t.messages for t in forum.threads)  # no "Vehicle updated" notes
    assert announce.sent == []                          # no false announcement


async def test_new_vehicle_announcement_pings_configured_role(db):
    cfg = _cfg()
    cfg.discord.vehicle_announce_role_id = 4242
    service, _forum, announce, _ = _service(db, cfg=cfg)
    thread = SimpleNamespace(jump_url="http://x/1", mention="<#1>")
    assert await service._announce(_veh(99, "Test Rig"), thread) == 1
    assert announce.sent[-1].startswith("<@&4242>")


async def test_new_vehicle_announcement_without_role_has_no_ping(db):
    service, _forum, announce, _ = _service(db)  # no role configured
    thread = SimpleNamespace(jump_url="http://x/1", mention="<#1>")
    await service._announce(_veh(99, "Test Rig"), thread)
    assert announce.sent and not announce.sent[-1].startswith("<@&")
