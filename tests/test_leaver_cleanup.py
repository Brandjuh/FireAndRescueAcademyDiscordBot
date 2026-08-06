"""Open requests of members who left the alliance are cleaned up on the
next roster sweep — the queue, the recurring rotation list and the
training/building requests, with an admin line naming what went."""

import pytest_asyncio

from fra_bot.db.database import Database, utcnow_iso
from fra_bot.db.repos import (
    AutomationRepo,
    MissionsRepo,
    RotationRepo,
)
from fra_bot.services.leaver_cleanup import LeaverCleanupService
from fra_bot.services.membersync import MIN_SAFE_ROSTER_COUNT


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "leaver.sqlite3")
    await database.connect()
    yield database
    await database.close()


class FakeBot:
    def __init__(self):
        self.admin_messages: list[str] = []

    async def notify_admin(self, text: str) -> None:
        self.admin_messages.append(text)


async def _member(db, mc_id, name, *, active=True):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at, left_at) VALUES (?, ?, 10.0, ?, ?, ?, ?)",
        (mc_id, name, 1 if active else 0, utcnow_iso(), utcnow_iso(),
         None if active else utcnow_iso()),
    )


async def _healthy_roster(db, count=MIN_SAFE_ROSTER_COUNT + 20):
    for i in range(count):
        await _member(db, 1000 + i, f"Active{i}")


async def _queued_mission(db, **overrides):
    fields = dict(
        source="board", kind="large", mission_source="preset",
        location_text="NYC", requester_name="Leaver", requester_mc_id=42,
        status="pending",
    )
    fields.update(overrides)
    return await MissionsRepo(db).create(**fields)


async def _request(db, **overrides):
    fields = dict(
        kind="training", thread_id=1, post_id=2,
        requester_name="Leaver", requester_mc_id=42, status="pending",
    )
    fields.update(overrides)
    return await AutomationRepo(db).create(**fields)


def _service(db):
    bot = FakeBot()
    return LeaverCleanupService(db, bot), bot


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

async def test_leavers_queue_rotation_and_requests_all_go(db):
    await _healthy_roster(db)
    await _member(db, 42, "Leaver", active=False)
    mission_id = await _queued_mission(db)
    rotation_id = await RotationRepo(db).add(
        location_text="NYC", kind="large", mission_source="preset",
        active=1, created_by="Leaver",
    )
    request_id = await _request(db)

    service, bot = _service(db)
    lines = await service.run()

    assert len(lines) == 3
    assert (await MissionsRepo(db).get(mission_id))["status"] == "cancelled"
    assert (await MissionsRepo(db).get(mission_id))["status_detail"] == (
        "requester left the alliance"
    )
    assert await RotationRepo(db).get(rotation_id) is None
    assert (await AutomationRepo(db).get(request_id))["status"] == "cancelled"
    assert len(bot.admin_messages) == 1
    assert "Leaver" in bot.admin_messages[0]
    assert f"queue #{mission_id}" in bot.admin_messages[0]
    assert f"rotation #{rotation_id}" in bot.admin_messages[0]


async def test_active_members_are_untouched(db):
    await _healthy_roster(db)
    await _member(db, 42, "Stayer")
    mission_id = await _queued_mission(db, requester_name="Stayer")
    rotation_id = await RotationRepo(db).add(
        location_text="NYC", kind="large", mission_source="preset",
        active=1, created_by="Stayer",
    )
    request_id = await _request(db, requester_name="Stayer")

    service, bot = _service(db)
    assert await service.run() == []
    assert (await MissionsRepo(db).get(mission_id))["status"] == "pending"
    assert await RotationRepo(db).get(rotation_id) is not None
    assert (await AutomationRepo(db).get(request_id))["status"] == "pending"
    assert bot.admin_messages == []


async def test_unknown_requester_is_not_a_leaver(db):
    # An admin-created rotation entry ("Web console") and a board post by a
    # name the roster never saw must both survive: absence from the roster
    # is not proof that somebody left.
    await _healthy_roster(db)
    rotation_id = await RotationRepo(db).add(
        location_text="NYC", kind="large", mission_source="preset",
        active=1, created_by="Web console",
    )
    mission_id = await _queued_mission(db, requester_name="Stranger",
                                       requester_mc_id=None)

    service, _ = _service(db)
    assert await service.run() == []
    assert await RotationRepo(db).get(rotation_id) is not None
    assert (await MissionsRepo(db).get(mission_id))["status"] == "pending"


async def test_mc_id_beats_a_reused_name(db):
    # The leaver's row is matched on mc id, so a namesake still in the
    # alliance keeps their request.
    await _healthy_roster(db)
    await _member(db, 42, "Twin", active=False)
    await _member(db, 43, "Twin")
    gone = await _queued_mission(db, requester_mc_id=42, requester_name="Twin")
    stays = await _queued_mission(db, requester_mc_id=43, requester_name="Twin")

    service, _ = _service(db)
    await service.run()
    assert (await MissionsRepo(db).get(gone))["status"] == "cancelled"
    assert (await MissionsRepo(db).get(stays))["status"] == "pending"


async def test_running_requests_are_left_alone(db):
    await _healthy_roster(db)
    await _member(db, 42, "Leaver", active=False)
    mid = await _queued_mission(db, status="processing")
    rid = await _request(db, status="processing")

    service, _ = _service(db)
    assert await service.run() == []
    assert (await MissionsRepo(db).get(mid))["status"] == "processing"
    assert (await AutomationRepo(db).get(rid))["status"] == "processing"


async def test_a_broken_scrape_cannot_empty_the_queue(db):
    # Far too few active members = the members page did not parse; the
    # safety floor keeps every request in place.
    await _member(db, 1, "Someone")
    await _member(db, 42, "Leaver", active=False)
    mission_id = await _queued_mission(db)

    service, bot = _service(db)
    assert await service.sweep() == []
    assert (await MissionsRepo(db).get(mission_id))["status"] == "pending"
    assert bot.admin_messages == []


async def test_cancelled_requests_are_never_announced(db):
    # 'cancelled' sits outside the automation publisher's status set, so a
    # member who left is never DM'd about a request that was dropped.
    await _healthy_roster(db)
    await _member(db, 42, "Leaver", active=False)
    await _request(db)
    service, _ = _service(db)
    await service.run()
    assert await AutomationRepo(db).pending_announcements() == []
