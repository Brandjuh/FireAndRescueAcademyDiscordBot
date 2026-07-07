"""Tests for the custom "Own mission" feature: the board spec parser, the
payload builder, the MissionsRepo queue and the scheduler's start logic."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MissionsRepo
from fra_bot.geo.geocoder import GeocodeResult
from fra_bot.mc.parsers.events import build_custom_mission_payload, parse_event_form
from fra_bot.mc.parsers.mission_spec import (
    MissionSpec,
    MissionSpecError,
    parse_mission_spec,
)
from fra_bot.services.missions import MissionScheduler


# --------------------------------------------------------------------------
# Board spec parsing
# --------------------------------------------------------------------------

def test_parses_full_mission_post():
    spec = parse_mission_spec(
        "Own mission: 350 5th Ave, New York\n"
        "type: 42\nsize: 3\namount: 4\npoi: 1\nshape: circle"
    )
    assert spec is not None
    assert spec.location_text == "350 5th Ave, New York"
    assert spec.mission_type_id == 42
    assert spec.size == 3
    assert spec.amount == 4
    assert spec.poi_type == 1


def test_non_mission_post_returns_none():
    # A bare location (an *event* request on the shared thread) must NOT be
    # picked up as a custom mission — no trigger word.
    assert parse_mission_spec("location: Amsterdam") is None
    assert parse_mission_spec("thanks everyone!") is None


def test_mission_post_without_location_raises():
    with pytest.raises(MissionSpecError):
        parse_mission_spec("own mission:\ntype: 5")


def test_out_of_range_size_raises():
    with pytest.raises(MissionSpecError):
        parse_mission_spec("own mission: NYC\nsize: 999")


def test_spec_defaults_and_validation():
    spec = MissionSpec(location_text="  NYC  ").validate()
    assert spec.location_text == "NYC"
    assert spec.mission_type_id is None
    assert spec.size == 1 and spec.amount == 1 and spec.shape == "circle"
    with pytest.raises(MissionSpecError):
        MissionSpec(location_text="").validate()
    with pytest.raises(MissionSpecError):
        MissionSpec(location_text="x", shape="triangle").validate()


# --------------------------------------------------------------------------
# Payload builder
# --------------------------------------------------------------------------

_ELIGIBLE_FORM = """
<html><body>
Last free mission: Mon, 01 Jul 2019 12:00
<form action="/missionAllianceCreate" method="post">
<input type="hidden" name="authenticity_token" value="tok123">
<input type="hidden" name="mission_position[latitude]" value="">
<input type="hidden" name="mission_position[longitude]" value="">
<input type="submit" value="Create mission">
</form>
</body></html>
"""

_STARTED_FORM = _ELIGIBLE_FORM.replace(
    "Mon, 01 Jul 2019 12:00", "Wed, 01 Jan 2025 12:00"
)

_WAITING_FORM = _ELIGIBLE_FORM.replace(
    "Mon, 01 Jul 2019 12:00", "Fri, 01 Jan 2100 12:00"
)

_COIN_FORM = _ELIGIBLE_FORM.replace(
    'value="Create mission"', 'value="Create mission (5 coins)"'
)


def test_build_payload_injects_params_and_pins_coins():
    form = parse_event_form(_ELIGIBLE_FORM)
    body = build_custom_mission_payload(
        form,
        latitude=40.5, longitude=-73.9, address="NYC",
        mission_type_id=42, poi_type=1, size=3, shape="circle", amount=4,
    )
    d = dict(body)
    assert d["mission_position[latitude]"] == "40.500000"
    assert d["mission_position[coins]"] == "0"
    assert d["mission_position[size]"] == "3"
    assert d["mission_position[amount]"] == "4"
    assert d["mission_position[mission_type_id]"] == "42"


# --------------------------------------------------------------------------
# Repo queue
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "missions.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_board_dedup_and_cancel(db):
    repo = MissionsRepo(db)
    fields = {"mission_type_id": 1, "size": 1, "amount": 1, "shape": "circle", "location_text": "x"}
    first = await repo.create_from_board(15293, 500, fields, requester_name="Bob", requester_mc_id=7)
    dup = await repo.create_from_board(15293, 500, fields, requester_name="Bob", requester_mc_id=7)
    assert first is not None and dup is None
    assert await repo.cancel(first) is True
    # Cancelling again (now cancelled) is a no-op.
    assert await repo.cancel(first) is False


async def test_sweep_processing_flags_stranded(db):
    repo = MissionsRepo(db)
    mid = await repo.create(source="discord", location_text="x")
    await repo.claim(mid)  # -> processing
    swept = await repo.sweep_processing()
    assert swept == 1
    row = await repo.get(mid)
    assert row["status"] == "failed"


# --------------------------------------------------------------------------
# Scheduler start logic
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, form_html, *, post_status=200):
        self.form_html = form_html
        self.post_status = post_status
        self.posted = False
        self.post_calls = 0

    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None):
        # After a start, show an advanced cooldown so verification passes.
        if self.posted:
            return _STARTED_FORM
        return self.form_html

    async def post_form(self, path, data, **kwargs):
        self.posted = True
        self.post_calls += 1
        return (self.post_status, {}, "")


class FakeGeo:
    def __init__(self):
        self.result = GeocodeResult(40.5, -73.9, "Resolved NYC", "nominatim_search")

    async def search(self, query):
        return self.result

    async def resolve_maps_link(self, url):
        return self.result


def _cfg(*, dry_run=True, min_rate=5.0):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            reply_to_board=True,
            mission=SimpleNamespace(
                enabled=True, board_enabled=False, thread_id=15293,
                interval=5, panel_channel_id=0, min_contribution_rate=min_rate,
            ),
        ),
    )


def _scheduler(cfg, client, db):
    return MissionScheduler(cfg, client, db, FakeGeo())


async def test_dry_run_marks_skipped_and_resolves(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_ELIGIBLE_FORM), db)
    mid = await sched.missions.create(source="discord", location_text="NYC", size=2, amount=3)
    await sched._process_queue()
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"
    assert row["latitude"] == 40.5           # geocoded
    assert "would start" in row["status_detail"]


async def test_cooldown_queues_as_waiting(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_WAITING_FORM), db)
    mid = await sched.missions.create(source="discord", location_text="NYC")
    await sched._process_queue()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    assert row["next_attempt_at"] is not None


async def test_coin_form_refused(db):
    sched = _scheduler(_cfg(dry_run=False), FakeClient(_COIN_FORM), db)
    mid = await sched.missions.create(source="discord", location_text="NYC")
    await sched._process_queue()
    row = await sched.missions.get(mid)
    assert row["status"] == "failed"
    assert "coins" in row["status_detail"]


async def test_live_start_verified_done(db):
    client = FakeClient(_ELIGIBLE_FORM)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await sched.missions.create(source="discord", location_text="NYC")
    await sched._process_queue()
    row = await sched.missions.get(mid)
    assert client.post_calls == 1
    assert row["status"] == "done"


async def test_board_contribution_gate_skips(db):
    # Seed a low-contribution member; a board request from them is skipped.
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (7, 'Bob', 1.0, 1, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    sched = _scheduler(_cfg(dry_run=True, min_rate=5.0), FakeClient(_ELIGIBLE_FORM), db)
    mid = await sched.missions.create_from_board(
        15293, 700, {"size": 1, "amount": 1, "shape": "circle", "location_text": "NYC"},
        requester_name="Bob", requester_mc_id=7,
    )
    await sched._process_queue()
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"
    assert "contribution" in row["status_detail"]
