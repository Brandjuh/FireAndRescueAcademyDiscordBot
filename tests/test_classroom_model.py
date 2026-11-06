"""The log-based classroom model (shadow mode)."""

import datetime as dt
import json

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import LogsRepo, StateRepo
from fra_bot.services import classroom_model as cm

pytestmark = pytest.mark.asyncio

T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
CATALOG = {"fire": {"HazMat": 3, "Foam": 1}}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "cm.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _log(db, action, building, description, when, sig):
    await LogsRepo(db).insert_batch([{
        "raw_timestamp": when.isoformat(), "event_at": when.isoformat(),
        "action_key": action, "description": description,
        "executed_name": "Admin", "affected_name": "[AA] Fire academy #001",
        "affected_type": "building", "affected_mc_id": building,
        "signature": sig,
    }], mark_posted=True)


async def test_rooms_count_classroom_expansions_only(db):
    await _log(db, "expansion_finished", 1, "Expansion finished\n  (Additional classroom)", T0, "e1")
    await _log(db, "expansion_finished", 1, "Expansion finished\n  (More classrooms)", T0, "e2")
    await _log(db, "expansion_finished", 1, "Expansion finished\n  (Parking lot)", T0, "e3")
    models = await cm.build_models(db, {1: "fire"}, CATALOG, since=T0 - dt.timedelta(days=8))
    assert models[1].rooms == 3


async def test_class_holds_a_room_for_its_course_length(db):
    await _log(db, "created_course", 1, "Created a course\n  (HazMat)", T0, "c1")
    m = (await cm.build_models(db, {1: "fire"}, CATALOG, since=T0 - dt.timedelta(days=8)))[1]
    assert m.free_at(T0 - dt.timedelta(minutes=1)) == 1
    assert m.free_at(T0 + dt.timedelta(days=2)) == 0
    assert m.next_free_at(T0 + dt.timedelta(days=2)) == T0 + dt.timedelta(days=3)
    assert m.free_at(T0 + dt.timedelta(days=3)) == 1


async def test_logged_completion_frees_the_room_early(db):
    # Bought with coins: "Course completed" long before the planned end.
    await _log(db, "created_course", 1, "Created a course (HazMat)", T0, "c1")
    done = T0 + dt.timedelta(hours=5)
    await _log(db, "course_completed", 1, "Course completed (HazMat)", done, "d1")
    m = (await cm.build_models(db, {1: "fire"}, CATALOG, since=T0 - dt.timedelta(days=8)))[1]
    assert m.free_at(done - dt.timedelta(minutes=1)) == 0
    assert m.free_at(done) == 1


async def test_unknown_course_is_assumed_long(db):
    await _log(db, "created_course", 1, "Created a course (Brand New)", T0, "c1")
    m = (await cm.build_models(db, {1: "fire"}, CATALOG, since=T0 - dt.timedelta(days=8)))[1]
    assert m.free_at(T0 + dt.timedelta(days=cm.UNKNOWN_COURSE_DAYS - 1)) == 0


async def test_compare_with_walk_counts_and_stores(db):
    state = StateRepo(db)
    await _log(db, "expansion_finished", 1, "Expansion finished (Additional classroom)", T0, "e1")
    await _log(db, "created_course", 1, "Created a course (HazMat)", T0, "c1")
    await _log(db, "created_course", 2, "Created a course (Foam)", T0, "c2")
    read_at = (T0 + dt.timedelta(hours=2)).timestamp()
    readings = [
        (1, "fire", 1, read_at),   # 2 rooms, 1 busy: model 1 -> exact
        (2, "fire", 1, read_at),   # 1 room, 1 busy: model 0, walk 1 -> too low
        (3, "fire", 0, read_at),   # 1 room, nothing logged: model 1 -> too high
    ]
    summary = await cm.compare_with_walk(db, state, CATALOG, readings)
    assert (summary["exact"], summary["model_too_low"], summary["model_too_high"]) == (1, 1, 1)
    assert summary["by_discipline"]["fire"] == {"walk": 2, "model": 2}
    stored = json.loads(await state.get(cm.CHECK_STATE_KEY))
    assert stored[-1]["academies"] == 3


async def test_compare_never_raises(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("bad")

    monkeypatch.setattr(cm, "_compare", boom)
    assert await cm.compare_with_walk(db, StateRepo(db), CATALOG, [(1, "fire", 0, 0.0)]) is None


async def test_rooms_are_capped_at_four(db):
    # [AA] Rescue Academy #0006 logs four classroom expansions; its page
    # shows four rooms, not five.
    for i in range(4):
        await _log(db, "expansion_finished", 1,
                   "Expansion finished (Additional classroom)", T0, f"e{i}")
    models = await cm.build_models(db, {1: "fire"}, CATALOG, since=T0 - dt.timedelta(days=8))
    assert models[1].rooms == cm.MAX_ROOMS == 4
