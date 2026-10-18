"""``!fra classes <count> <course>``: the admin override that opens any
number of classes at once and reports how many fit."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo
from fra_bot.mc.trainings_catalog import TrainingMatch
from fra_bot.services.trainings import TrainingsService


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "override.sqlite3")
    await database.connect()
    yield database
    await database.close()


_MATCH = TrainingMatch(discipline="coastal", name="Ocean Navigation", duration_days=5)


def _service(outcomes):
    """A service whose single-class opener replays ``outcomes``."""
    svc = TrainingsService.__new__(TrainingsService)
    calls = []

    async def fake_open(match, *, duration):
        calls.append(match.name)
        return outcomes[len(calls) - 1]

    svc._open_training = fake_open
    return svc, calls


async def test_override_opens_past_the_request_cap():
    outcomes = [{"status": "opened", "building_id": 1 + i % 2} for i in range(7)]
    svc, calls = _service(outcomes)
    result = await svc.open_classes_override(_MATCH, 7)
    assert len(calls) == 7                  # no cap of 4
    assert result["opened"] == [1, 2, 1, 2, 1, 2, 1]
    assert result["stopped"] is None


async def test_override_stops_when_classrooms_run_out():
    outcomes = [
        {"status": "opened", "building_id": 1},
        {"status": "opened", "building_id": 1},
        {"status": "busy", "reason": "academy 1 has no free classroom"},
    ]
    svc, calls = _service(outcomes)
    result = await svc.open_classes_override(_MATCH, 10)
    assert len(calls) == 3                  # stops at the first busy, no retry
    assert result["opened"] == [1, 1]
    assert result["stopped"] == "busy"
    assert "no free classroom" in result["reason"]


async def test_override_counts_uncertain_without_reposting():
    outcomes = [
        {"status": "uncertain", "building_id": 3, "reason": "could not verify"},
        {"status": "opened", "building_id": 3},
    ]
    svc, calls = _service(outcomes)
    result = await svc.open_classes_override(_MATCH, 2)
    assert len(calls) == 2
    assert result["opened"] == [3]
    assert result["uncertain"] == [3]


# -- the command ----------------------------------------------------------------

class _Ctx:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)

    @asynccontextmanager
    async def typing(self):
        yield


def _admin(db, result, seen):
    from fra_bot.cogs.admin import AdminCog

    async def open_classes_override(match, count):
        seen.append((match.discipline, match.name, count))
        return result

    service = SimpleNamespace(
        state=StateRepo(db),
        open_classes_override=open_classes_override,
        _ambiguity_help=lambda a: f"{a.name} is ambiguous.",
    )
    cog = AdminCog.__new__(AdminCog)
    cog.bot = SimpleNamespace(trainings=service)
    return cog


async def test_command_reports_opened_count_when_rooms_run_out(db):
    from fra_bot.cogs.admin import AdminCog

    seen = []
    cog = _admin(db, {
        "opened": [11, 11, 12], "uncertain": [], "stopped": "busy",
        "reason": "academy 12 has no free classroom", "dry_run": False,
    }, seen)
    ctx = _Ctx()
    await AdminCog.open_classes.callback(cog, ctx, 8, course="Coastal - Ocean Navigation")
    assert seen == [("coastal", "Ocean Navigation", 8)]
    final = ctx.sent[-1]
    assert "3 of 8 classes opened" in final
    assert "No free classroom left" in final
    assert "buildings/11" in final and "buildings/12" in final


async def test_command_all_opened(db):
    from fra_bot.cogs.admin import AdminCog

    seen = []
    cog = _admin(db, {
        "opened": [11] * 6, "uncertain": [], "stopped": None,
        "reason": None, "dry_run": False,
    }, seen)
    ctx = _Ctx()
    await AdminCog.open_classes.callback(cog, ctx, 6, course="HazMat")
    assert seen == [("fire", "HazMat", 6)]
    assert "6 of 6 classes opened" in ctx.sent[-1]
    assert "No free classroom" not in ctx.sent[-1]


async def test_command_rejects_unknown_course_and_bad_count(db):
    from fra_bot.cogs.admin import AdminCog

    seen = []
    cog = _admin(db, {}, seen)
    ctx = _Ctx()
    await AdminCog.open_classes.callback(cog, ctx, 0, course="HazMat")
    await AdminCog.open_classes.callback(cog, ctx, 3, course="zzzz qqqq")
    assert seen == []
    assert "at least 1" in ctx.sent[0]
    assert "exactly one course" in ctx.sent[1]
