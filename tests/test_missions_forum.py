"""Missions-database forum: catalog normalisation, tag derivation, the
embed, and the sync loop (create / edit-in-place / dedup / adopt / cap /
announce)."""

import json
from types import SimpleNamespace

import discord
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MissionsForumRepo
from fra_bot.mc import missions_catalog as catalog
from fra_bot.services.missions_forum import (
    MissionsForumService,
    build_mission_embed,
    thread_key,
    thread_title,
)

BASE_URL = "https://www.missionchief.com"

FUEL_TRUCK = {
    "id": "297",
    "name": "Overturned Fuel Truck",
    "average_credits": 12500,
    "mission_categories": ["fire", "hazmat"],
    "requirements": {
        "firetrucks": 4,
        "platform_trucks": 2,
        "hazmat_vehicles": 2,
        "water_needed": 12000,
        "special": "oneof Foam Tender or Airport Crash Tender",
        "personnel": {"gw_gefahrgut": 2, "elw2": 1},
    },
    "prerequisites": {"main_building": 0, "fire_stations": 6, "tow_truck_extension": 1},
    "chances": {"patient_transport": 60},
    "additional": {
        "possible_patient": 4,
        "patient_specialization_captions": ["Traumatology"],
        "possible_crashed_car_min": 1,
        "possible_crashed_car_max": 2,
        "expansion_missions_ids": ["298"],
    },
    "place_array": ["Gas Station", "Highway"],
}

EXPLOSION = {
    "id": "298",
    "name": "Fuel Truck Explosion",
    "average_credits": 500,
    "mission_categories": ["fire"],
    "requirements": {"firetrucks": 2},
    "prerequisites": {"main_building": 0},
    "additional": {},
}

OVERLAY = {
    "id": "700-heat",
    "base_mission_id": "700",
    "additive_overlays": "heat_wave",
    "name": "Brush Fire (Heat Wave)",
    "average_credits": 3000,
    "mission_categories": ["wildfire", "police"],
    "requirements": {"firetrucks": 1},
    "prerequisites": {},
    "additional": {"min_possible_prisoners": 1, "max_possible_prisoners": 3},
}


def _missions():
    return [json.loads(json.dumps(m)) for m in (FUEL_TRUCK, EXPLOSION, OVERLAY)]


# ---------------------------------------------------------------------------
# Catalog basics
# ---------------------------------------------------------------------------

def test_normalize_accepts_all_three_payload_shapes():
    as_list = catalog.normalize_missions(_missions())
    as_wrapper = catalog.normalize_missions({"missions": _missions()})
    as_dict = catalog.normalize_missions({m["id"]: m for m in _missions()})
    assert len(as_list) == len(as_wrapper) == len(as_dict) == 3
    # dict-keyed payloads inherit the key as id
    keyed = catalog.normalize_missions({"42": {"name": "No id"}})
    assert keyed[0]["id"] == "42"


def test_mission_key_variants():
    assert catalog.mission_key(FUEL_TRUCK) == "297"
    assert catalog.mission_key(OVERLAY) == "700/heat_wave"
    assert catalog.mission_key({"id": "644-0"}) == "644-0"
    assert catalog.mission_key({"name": "Weird Mission!"}) == "weird-mission"


def test_mission_key_never_contains_whitespace():
    # Keys double as the thread-title marker, whose parser stops at spaces.
    spaced = {"id": "700-x", "base_mission_id": "700", "additive_overlays": "Heat Wave"}
    key = catalog.mission_key(spaced)
    assert key == "700/heat-wave"
    assert thread_key(thread_title("Some Mission", key)) == key
    listy = {"id": "1-0", "base_mission_id": "1", "additive_overlays": ["a b", "c"]}
    assert " " not in catalog.mission_key(listy)
    long_name = {"name": "x " * 120}
    assert len(catalog.mission_key(long_name)) <= 64


def test_expansion_names_do_not_depend_on_payload_order():
    base = {"id": "700", "name": "Brush Fire"}
    overlay = {
        "id": "700-h", "base_mission_id": "700",
        "additive_overlays": "heat_wave", "name": "Brush Fire (Heat Wave)",
    }
    referrer = {
        "id": "1", "name": "Ref",
        "additional": {"expansion_missions_ids": ["700"]},
    }
    for order in ([base, overlay], [overlay, base]):
        missions = [json.loads(json.dumps(m)) for m in order + [referrer]]
        catalog.add_related_mission_names(missions)
        names = missions[-1]["additional"]["expansion_mission_names"]
        assert names == ["Brush Fire"]  # the plain id wins, in any order


def test_detail_url_points_at_base_mission():
    assert catalog.detail_url(FUEL_TRUCK, BASE_URL).endswith("/einsaetze/297")
    assert catalog.detail_url(OVERLAY, BASE_URL).endswith("/einsaetze/700")
    assert catalog.detail_url({"id": "644-0"}, BASE_URL).endswith("/einsaetze/644")


def test_content_hash_tracks_data_changes():
    a = catalog.content_hash(FUEL_TRUCK)
    changed = json.loads(json.dumps(FUEL_TRUCK))
    changed["average_credits"] = 99999
    assert a == catalog.content_hash(FUEL_TRUCK)
    assert a != catalog.content_hash(changed)


def test_related_names_resolve_expansions():
    missions = _missions()
    catalog.add_related_mission_names(missions)
    assert missions[0]["additional"]["expansion_mission_names"] == [
        "Fuel Truck Explosion"
    ]
    assert catalog.expansion_names(missions[0]) == ["Fuel Truck Explosion"]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_tag_names_fit_discords_20_char_limit():
    assert all(len(name) <= 20 for name in catalog.FORUM_TAG_EMOJI)
    assert len(catalog.FORUM_TAG_EMOJI) <= 20


def test_derive_tags_disciplines_and_attributes():
    tags = catalog.derive_tags(FUEL_TRUCK)
    # Disciplines first (max 2), then Patients before the rest; max 5 total.
    assert tags[:2] == ["Fire", "HazMat"]
    assert tags[2] == "Patients"
    assert len(tags) == 5
    # main_building alone is not an unlock gate.
    assert catalog.derive_tags(EXPLOSION) == ["Fire"]


def test_extension_tag_ignores_plain_station_counts():
    """Nearly every mission requires N stations — that must NOT tag it
    (the old Unlock Needed tag sat on every post and filtered nothing)."""
    stations_only = {
        "id": "10", "name": "Everyday Fire", "mission_categories": ["fire"],
        "prerequisites": {"main_building": 0, "fire_stations": 4},
    }
    assert "Extension Needed" not in catalog.derive_tags(stations_only)
    with_extension = json.loads(json.dumps(stations_only))
    with_extension["prerequisites"]["tow_truck_extension"] = 1
    assert "Extension Needed" in catalog.derive_tags(with_extension)
    assert "Unlock Needed" not in catalog.FORUM_TAG_EMOJI


def test_derive_tags_variation_and_prisoners():
    tags = catalog.derive_tags(OVERLAY)
    assert "Police" in tags and "Wildfire" in tags
    assert "Prisoners" in tags and "Variation" in tags


def test_discipline_inferred_from_generating_building():
    # No (mapped) categories, but generated by a Police Station.
    mission = {
        "id": "1",
        "name": "Plain mission",
        "mission_categories": [],
        "requirements": {"police_cars": 1},
        "prerequisites": {"main_building": 5},
    }
    assert catalog.derive_tags(mission)[0] == "Police"
    assert catalog.discipline_of(mission) == "Police"


def test_derive_tags_never_returns_empty():
    # Nothing recognisable at all → the Other fallback carries the post
    # (the forum requires a tag on every post).
    bare = {"id": "2", "name": "Bare", "average_credits": 100}
    assert catalog.derive_tags(bare) == [catalog.FALLBACK_TAG]


# ---------------------------------------------------------------------------
# Titles + embed
# ---------------------------------------------------------------------------

def test_thread_title_roundtrip_and_truncation():
    title = thread_title("Overturned Fuel Truck", "297")
    assert title == "Overturned Fuel Truck · #297"
    assert thread_key(title) == "297"
    long = thread_title("x" * 200, "700/heat_wave")
    assert len(long) <= 100
    assert thread_key(long) == "700/heat_wave"  # key survives truncation
    assert thread_key("random thread") is None


def test_embed_renders_all_sections():
    missions = _missions()
    catalog.add_related_mission_names(missions)
    embed = build_mission_embed(missions[0], base_url=BASE_URL, updated="2026-07-10")
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "Overturned Fuel Truck"
    assert embed.url.endswith("/einsaetze/297")
    assert fields["💰 Credits"] == "12,500 average"
    assert fields["🏢 Generated by"] == "Fire Station"
    assert "Fire, Hazmat" in fields["📂 Type"]
    vehicles = fields["🚒 Vehicles & equipment"]
    assert "• 4× Fire Trucks" in vehicles
    assert "• Water needed: 12,000 gallons" in vehicles
    assert "one of Foam Tender OR Airport Crash Tender" in vehicles
    assert "• 2× HazMat" in fields["🎓 Trainings"]
    assert "• 6× Fire Stations" in fields["🔓 Unlock requirements"]
    assert "• 1× Tow Truck Extension" in fields["🔓 Unlock requirements"]
    assert fields["🧑‍⚕️ Patients"] == (
        "Up to 4 · transport chance 60% · departments: Traumatology"
    )
    assert fields["🧲 Towing"] == "1–2 vehicle(s) to tow"
    assert fields["📍 POI"] == "Gas Station, Highway"
    assert fields["🔁 Expands to"] == "Fuel Truck Explosion"
    assert "Mission #297" in embed.footer.text
    assert "updated 2026-07-10" in embed.footer.text
    assert len(embed) <= 6000


def test_embed_omits_empty_sections():
    embed = build_mission_embed(EXPLOSION, base_url=BASE_URL)
    names = [f.name for f in embed.fields]
    assert "🧑‍⚕️ Patients" not in names
    assert "🧲 Towing" not in names
    assert "📍 POI" not in names
    assert "🎓 Trainings" not in names


def test_huge_embed_is_squeezed_under_the_limit():
    monster = json.loads(json.dumps(FUEL_TRUCK))
    monster["requirements"] = {f"unit_type_{i}": i + 1 for i in range(200)}
    monster["place_array"] = [f"Some Point of Interest {i}" for i in range(200)]
    embed = build_mission_embed(monster, base_url=BASE_URL)
    assert len(embed) <= 6000


# ---------------------------------------------------------------------------
# Sync (fakes)
# ---------------------------------------------------------------------------

class FakeMC:
    def __init__(self, payload):
        self.payload = payload

    async def fetch_page(self, path, **kwargs):
        return json.dumps(self.payload)


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
    _next_id = 5000

    def __init__(self, name, embed, applied_tags, bot, forum=None):
        FakeThread._next_id += 1
        self.id = FakeThread._next_id
        self.name = name
        self.archived = False
        self.applied_tags = list(applied_tags or [])
        self.starter = FakeMessage(self.id, embed)
        self.starter_deleted = False
        self.jump_url = f"https://discord.com/channels/1/{self.id}"
        self.edit_calls = []
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

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)
        self.name = kwargs.get("name", self.name)
        self.archived = kwargs.get("archived", self.archived)
        if "applied_tags" in kwargs:
            self.applied_tags = list(kwargs["applied_tags"])
        return self


def _http_400(message):
    return discord.HTTPException(
        SimpleNamespace(status=400, reason="Bad Request"), message
    )


class FakeForum:
    """Mimics Discord's server-side rules that bit us live:

    * enabling require_tag is validated against the tag list as it exists
      BEFORE the edit (error 40066), even when the same request adds tags;
    * a require_tag forum refuses tag-less posts (error 40067).
    """

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
                    "There are no tags available that can be set by "
                    "non-moderators (error code: 40066)"
                )
        if "available_tags" in kwargs:
            self.available_tags = list(kwargs["available_tags"])
        if "require_tag" in kwargs:
            self.flags.require_tag = kwargs["require_tag"]
        return self

    async def create_thread(self, *, name, embed=None, applied_tags=None, reason=None):
        if self.flags.require_tag and not applied_tags:
            raise _http_400(
                "A tag is required to create a forum post in this channel "
                "(error code: 40067)"
            )
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
        enabled=True, sync_time="04:00", announce_new=False, max_posts_per_run=100
    )
    for key, value in overrides.items():
        setattr(auto, key, value)
    return SimpleNamespace(
        missionchief=SimpleNamespace(base_url=BASE_URL),
        discord=SimpleNamespace(
            channels=SimpleNamespace(missions_forum=900, mission_announce=901)
        ),
        automation=SimpleNamespace(missions_forum=auto),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "forum.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _service(db, payload, cfg=None):
    cfg = cfg or _cfg()
    bot = FakeBot()
    forum = FakeForum(900, bot)
    announce = FakeAnnounce(901, bot)
    service = MissionsForumService(cfg, FakeMC(payload), db, bot)
    service.post_delay = 0
    service.batch_delay = 0
    return service, forum, announce, bot


async def test_sync_creates_posts_tags_and_rows(db):
    service, forum, announce, _ = _service(db, _missions())
    summary = await service.sync()
    assert summary["created"] == 3 and summary["failed"] == 0
    assert len(forum.threads) == 3
    # Tags were created in bulk and posting now requires one.
    assert {t.name for t in forum.available_tags} == set(catalog.FORUM_TAG_EMOJI)
    assert forum.flags.require_tag is True
    assert summary["tags_created"]
    # Every thread carries tags and the recovery marker in its title.
    for thread in forum.threads:
        assert thread.applied_tags
        assert thread_key(thread.name)
    assert await MissionsForumRepo(db).count() == 3
    # Initial fill never announces, even though missions are "new".
    assert announce.sent == []


async def test_fresh_forum_gets_tags_then_flag_in_two_steps(db):
    """Regression for live error 40066: enabling require_tag together with
    the tag creation is rejected on a fresh forum — the flag must follow in
    a second edit (the fake validates like Discord does)."""
    service, forum, _, _ = _service(db, _missions())
    created, forum2 = await service.ensure_tags(forum)
    assert set(created) == set(catalog.FORUM_TAG_EMOJI)
    assert forum2.flags.require_tag is True


async def test_old_unlock_tag_is_renamed_in_place(db):
    """An existing forum with the old tag keeps the tag's id — the rename
    travels to every already-tagged post automatically."""
    service, forum, _, _ = _service(db, _missions())
    old = discord.ForumTag(name="Unlock Needed", emoji="🔓")
    old.id = 424242
    forum.available_tags = [old]
    changes, forum2 = await service.ensure_tags(forum)
    names = {t.name for t in forum2.available_tags}
    assert "Extension Needed" in names and "Unlock Needed" not in names
    renamed = next(t for t in forum2.available_tags if t.name == "Extension Needed")
    assert renamed.id == 424242  # same tag, renamed — not a new one
    assert "Unlock Needed → Extension Needed" in changes


def _old_tag_set():
    """The forum tag set as the first release created it (19 tags with
    'Unlock Needed' instead of 'Extension Needed'), with real ids."""
    tags = []
    for index, name in enumerate(catalog.FORUM_TAG_EMOJI):
        if name == "Extension Needed":
            name = "Unlock Needed"
        tag = discord.ForumTag(name=name, emoji=catalog.FORUM_TAG_EMOJI.get(name, "🔓"))
        tag.id = 1000 + index
        tags.append(tag)
    return tags


async def test_live_forum_state_gets_the_rename_via_sync(db):
    """Mirror of the production forum after the first release: all 19 old
    tags exist, require_tag is on, posts exist. One sync must rename the
    tag and report it."""
    payload = _missions()
    service, forum, _, _ = _service(db, payload)
    forum.available_tags = _old_tag_set()
    forum.flags.require_tag = True
    summary = await service.sync()
    names = {t.name for t in forum.available_tags}
    assert "Extension Needed" in names and "Unlock Needed" not in names
    assert len(forum.available_tags) == 19  # renamed, not added
    assert any(
        "Unlock Needed → Extension Needed" in line for line in summary["lines"]
    )


async def test_rename_tolerates_case_and_whitespace(db):
    service, forum, _, _ = _service(db, _missions())
    odd = discord.ForumTag(name="unlock  needed", emoji="🔓")
    odd.id = 55
    forum.available_tags = [odd]
    changes, forum2 = await service.ensure_tags(forum)
    renamed = next(t for t in forum2.available_tags if t.id == 55)
    assert renamed.name == "Extension Needed"
    # And no duplicate "Extension Needed" was created alongside it.
    assert sum(1 for t in forum2.available_tags if t.name == "Extension Needed") == 1


async def test_stale_old_tag_is_removed_when_both_exist(db):
    service, forum, _, _ = _service(db, _missions())
    old = discord.ForumTag(name="Unlock Needed", emoji="🔓")
    old.id = 1
    new = discord.ForumTag(name="Extension Needed", emoji="🔓")
    new.id = 2
    forum.available_tags = [old, new]
    changes, forum2 = await service.ensure_tags(forum)
    names = [t.name for t in forum2.available_tags]
    assert "Unlock Needed" not in names
    assert names.count("Extension Needed") == 1
    assert any("removed stale tag" in c for c in changes)


async def test_stop_halts_the_sync_and_next_run_continues(db):
    service, forum, _, _ = _service(db, _missions())

    async def stopping_pause(writes):
        service.request_stop()  # as if the admin ran `!fra missionsforum stop`

    service._pause = stopping_pause
    summary = await service.sync()
    assert summary["created"] == 1 and summary["stopped"] is True
    assert any("stopped" in line for line in summary["lines"])
    # A stopped run never counts as a completed backfill.
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.missions_forum import STATE_BACKFILL_DONE

    assert await StateRepo(db).get(STATE_BACKFILL_DONE) is None
    # The stop flag resets: the next run finishes the job.
    async def no_pause(writes):
        return None

    service._pause = no_pause
    summary = await service.sync()
    assert summary["created"] == 2 and summary["stopped"] is False
    assert len(forum.threads) == 3


async def test_wipe_deletes_everything_and_resync_is_quiet(db):
    payload = _missions()
    cfg = _cfg(announce_new=True)
    service, forum, announce, _ = _service(db, payload, cfg)
    await service.sync()  # completes → backfill flag set
    summary = await service.wipe()
    assert summary["deleted"] == 3 and summary["failed"] == 0
    assert forum.threads == []
    assert await MissionsForumRepo(db).count() == 0
    # The repost after a wipe is a fresh backfill: no announcement flood.
    summary = await service.sync()
    assert summary["created"] == 3 and summary["announced"] == 0
    assert announce.sent == []


async def test_fallback_tag_wins_when_foreign_tags_fill_slots(db):
    """A forum with foreign tags leaves <19 free slots: the Other fallback
    must be created FIRST (it keeps tag-less missions postable), trimming
    lower-priority tags instead."""
    payload = _missions()
    payload.append({"id": "999", "name": "Bare Mission", "average_credits": 100})
    service, forum, _, _ = _service(db, payload)
    forum.available_tags = [
        SimpleNamespace(name=f"Foreign{i}", moderated=False) for i in range(3)
    ]
    summary = await service.sync()
    names = {t.name for t in forum.available_tags}
    assert catalog.FALLBACK_TAG in names
    assert len(forum.available_tags) == 20  # never over Discord's cap
    assert summary["failed"] == 0 and summary["created"] == 4
    bare = next(t for t in forum.threads if "#999" in t.name)
    assert [t.name for t in bare.applied_tags] == [catalog.FALLBACK_TAG]


async def test_no_manage_channels_still_posts_without_tags(db):
    """Tag creation refused (no Manage Channels) → keep posting untagged
    instead of silently zero-progressing every day."""
    class NoEditForum(FakeForum):
        async def edit(self, **kwargs):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "no perms"
            )

    cfg = _cfg()
    bot = FakeBot()
    forum = NoEditForum(900, bot)
    FakeAnnounce(901, bot)
    service = MissionsForumService(cfg, FakeMC(_missions()), db, bot)
    service.post_delay = 0
    service.batch_delay = 0
    summary = await service.sync()
    assert summary["created"] == 3 and summary["failed"] == 0
    assert summary["tags_created"] == []
    assert forum.flags.require_tag is False


async def test_unparseable_json_reports_instead_of_crashing(db):
    service, _, _, _ = _service(db, _missions())
    service._mc = SimpleNamespace()
    async def bad_fetch(path, **kwargs):
        return "<html>maintenance</html>"
    service._mc.fetch_page = bad_fetch
    summary = await service.sync()
    assert summary["error"] and "unusable" in summary["error"]


async def test_forum_refusing_require_tag_does_not_block_posting(db):
    class StubbornForum(FakeForum):
        async def edit(self, **kwargs):
            if kwargs.get("require_tag"):
                raise _http_400("no flag for you (error code: 40066)")
            return await super().edit(**kwargs)

    cfg = _cfg()
    bot = FakeBot()
    forum = StubbornForum(900, bot)
    FakeAnnounce(901, bot)
    service = MissionsForumService(cfg, FakeMC(_missions()), db, bot)
    service.post_delay = 0
    service.batch_delay = 0
    summary = await service.sync()
    assert summary["created"] == 3 and summary["failed"] == 0
    assert forum.flags.require_tag is False  # flag refused, posts landed


async def test_tagless_mission_gets_the_fallback_tag_applied(db):
    payload = _missions()
    payload.append({"id": "999", "name": "Bare Mission", "average_credits": 100})
    service, forum, _, _ = _service(db, payload)
    summary = await service.sync()
    assert summary["created"] == 4 and summary["failed"] == 0
    bare = next(t for t in forum.threads if "#999" in t.name)
    assert [t.name for t in bare.applied_tags] == [catalog.FALLBACK_TAG]


async def test_second_sync_is_a_no_op(db):
    service, forum, _, _ = _service(db, _missions())
    await service.sync()
    summary = await service.sync()
    assert summary["created"] == 0 and summary["updated"] == 0
    assert summary["skipped"] == 3
    assert len(forum.threads) == 3
    assert forum.threads[0].starter.edits == 0


async def test_non_http_error_on_one_mission_does_not_abort_the_run(db):
    """A crashed mission used to abort the whole run — stalling the
    backfill until the next day's sync. It must count as failed and the
    run must carry on."""
    service, forum, _, _ = _service(db, _missions())
    original = forum.create_thread
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("embed exploded")  # NOT a discord.HTTPException
        return await original(**kwargs)

    forum.create_thread = flaky
    summary = await service.sync()
    assert summary["failed"] == 1
    assert summary["created"] == 2  # the rest still posted
    # The failed mission is retried (and succeeds) on the next run.
    summary = await service.sync()
    assert summary["created"] == 1 and summary["failed"] == 0
    assert len(forum.threads) == 3


async def test_status_reports_backfill_progress(db):
    service, _, _, _ = _service(db, _missions())
    lines = await service.status_lines()
    assert any("backfill: ⏳" in line for line in lines)
    await service.sync()  # completes uncapped → backfill done
    lines = await service.status_lines()
    assert any("backfill: ✅" in line for line in lines)


async def test_changed_mission_is_edited_in_place(db):
    payload = _missions()
    service, forum, _, _ = _service(db, payload)
    await service.sync()
    payload[0]["average_credits"] = 99999
    summary = await service.sync()
    assert summary["updated"] == 1 and summary["created"] == 0
    assert len(forum.threads) == 3  # no new post
    edited = next(t for t in forum.threads if "#297" in t.name)
    assert edited.starter.edits == 1
    fields = {f.name: f.value for f in edited.starter.embeds[0].fields}
    assert fields["💰 Credits"] == "99,999 average"


async def test_post_cap_is_respected(db):
    service, forum, _, _ = _service(db, _missions())
    summary = await service.sync(limit=1)
    assert summary["created"] == 1 and summary["capped"] is True
    assert len(forum.threads) == 1
    # The next run picks up the rest.
    summary = await service.sync()
    assert summary["created"] == 2
    assert len(forum.threads) == 3


async def test_new_mission_announced_when_enabled(db):
    payload = _missions()[:2]
    cfg = _cfg(announce_new=True)
    service, forum, announce, _ = _service(db, payload, cfg)
    await service.sync()
    assert announce.sent == []  # initial fill stays quiet
    payload.append(json.loads(json.dumps(OVERLAY)))
    summary = await service.sync()
    assert summary["created"] == 1 and summary["announced"] == 1
    assert len(announce.sent) == 1
    assert "Brush Fire (Heat Wave)" in announce.sent[0]
    new_thread = next(t for t in forum.threads if "700/heat_wave" in t.name)
    assert new_thread.jump_url in announce.sent[0]


async def test_backfill_continuation_runs_never_announce(db):
    """The initial backfill spans several capped runs; none of them may
    ping — announcements arm only after one full uncapped pass."""
    payload = _missions()
    cfg = _cfg(announce_new=True)
    service, forum, announce, _ = _service(db, payload, cfg)
    assert (await service.sync(limit=1))["announced"] == 0  # run 1 (capped)
    assert (await service.sync(limit=1))["announced"] == 0  # run 2 (capped)
    assert (await service.sync())["announced"] == 0         # run 3 completes
    assert announce.sent == []
    # Now the backfill is done: a genuinely new mission announces.
    payload.append({"id": "999", "name": "Fresh Mission", "average_credits": 1})
    summary = await service.sync()
    assert summary["announced"] == 1 and len(announce.sent) == 1


async def test_orphan_thread_is_reclaimed_not_duplicated(db):
    """Crash between create_thread and the DB write: the next run reclaims
    the marker-titled thread instead of posting a duplicate. The crash
    happens before the archive step, so the orphan is still active."""
    service, forum, _, _ = _service(db, _missions())
    await service.sync()
    # Simulate the lost bookkeeping for one mission (crash window: the
    # thread exists, is not yet archived, and has no row).
    await db.execute(
        "DELETE FROM missions_forum_posts WHERE mission_key = '297'"
    )
    next(t for t in forum.threads if "#297" in t.name).archived = False
    summary = await service.sync()
    assert summary["created"] == 0
    assert summary["updated"] == 1  # reclaimed + content refreshed
    assert len(forum.threads) == 3
    assert await MissionsForumRepo(db).count() == 3


async def test_posts_are_archived_to_respect_the_active_thread_cap(db):
    """Discord caps a guild at 1000 ACTIVE threads and the game has well
    over that many missions — every post is archived right after writing
    (like the reference bot), and an update re-archives afterwards."""
    payload = _missions()
    service, forum, _, _ = _service(db, payload)
    await service.sync()
    assert all(t.archived for t in forum.threads)
    payload[0]["average_credits"] = 42  # trigger an in-place update
    await service.sync()
    edited = next(t for t in forum.threads if "#297" in t.name)
    assert edited.starter.edits == 1
    assert edited.archived is True  # re-archived after the edit


async def test_deleted_starter_message_heals_by_reposting(db):
    payload = _missions()
    service, forum, _, _ = _service(db, payload)
    await service.sync()
    victim = next(t for t in forum.threads if "#297" in t.name)
    victim.starter_deleted = True
    forum.threads.remove(victim)  # keep the orphan map from reclaiming it
    payload[0]["average_credits"] = 1  # trigger an update attempt
    summary = await service.sync()
    assert summary["created"] == 1 and summary["failed"] == 0
    replacement = next(t for t in forum.threads if "#297" in t.name)
    assert replacement.id != victim.id
    # And the next run is clean, not a repeating failure.
    summary = await service.sync()
    assert summary["skipped"] == 3 and summary["failed"] == 0


async def test_duplicate_keys_in_payload_are_skipped(db):
    payload = _missions()
    payload.append(json.loads(json.dumps(FUEL_TRUCK)))  # same id twice
    service, forum, _, _ = _service(db, payload)
    summary = await service.sync()
    assert summary["created"] == 3
    assert sum(1 for t in forum.threads if "#297" in t.name) == 1
    # No flip-flopping on the next run either.
    summary = await service.sync()
    assert summary["updated"] == 0 and summary["skipped"] == 3


async def test_announce_off_by_default(db):
    payload = _missions()[:2]
    service, _, announce, _ = _service(db, payload)
    await service.sync()
    payload.append(json.loads(json.dumps(OVERLAY)))
    summary = await service.sync()
    assert summary["created"] == 1 and summary["announced"] == 0
    assert announce.sent == []


async def test_existing_posts_are_adopted_not_duplicated(db):
    """DB loss: threads already exist on the forum → adopt, don't repost."""
    service, forum, _, bot = _service(db, _missions())
    await service.sync()
    assert len(forum.threads) == 3

    # Same forum, emptied mapping table (simulated DB loss).
    await db.execute("DELETE FROM missions_forum_posts")
    summary = await service.sync()
    assert summary["adopted"] == 3
    assert summary["created"] == 0
    assert len(forum.threads) == 3  # still no duplicates
    # Adopted rows had no hash, so content was refreshed in place.
    assert summary["updated"] == 3


async def test_deleted_thread_is_reposted(db):
    payload = _missions()
    service, forum, _, bot = _service(db, payload)
    await service.sync()
    victim = next(t for t in forum.threads if "#297" in t.name)
    forum.threads.remove(victim)
    bot.remove_channel(victim.id)
    payload[0]["average_credits"] = 1  # force a content change
    summary = await service.sync()
    assert summary["created"] == 1
    assert any("#297" in t.name for t in forum.threads)


async def test_unconfigured_forum_reports_instead_of_crashing(db):
    cfg = _cfg()
    cfg.discord.channels.missions_forum = 0
    bot = FakeBot()
    service = MissionsForumService(cfg, FakeMC(_missions()), db, bot)
    summary = await service.sync()
    assert summary["error"]
    assert "not configured" in summary["lines"][0]


def test_settings_expose_the_new_keys():
    from fra_bot.core import settings as rt

    assert rt.resolve("missions_forum").path == "discord.channels.missions_forum"
    assert rt.resolve("mission_announce").path == "discord.channels.mission_announce"
    assert rt.resolve("announce_new").path == "automation.missions_forum.announce_new"
    assert (
        rt.resolve("max_posts_per_run").path
        == "automation.missions_forum.max_posts_per_run"
    )
    assert rt.resolve("sync_time").path == "automation.missions_forum.sync_time"
