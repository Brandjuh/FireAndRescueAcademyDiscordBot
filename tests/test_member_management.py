"""Member management: self-managed profiles + the central action log."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.profile import SECTIONS, validate_birthday
from fra_bot.db.database import Database
from fra_bot.db.repos import (
    PROFILE_FIELDS,
    MemberActionsRepo,
    MemberProfilesRepo,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "mm.sqlite3")
    await database.connect()
    yield database
    await database.close()


# -- profiles -----------------------------------------------------------------

async def test_profile_upsert_partial_and_clear(db):
    repo = MemberProfilesRepo(db)
    await repo.set_fields(1, bio="Hoi!", timezone="Europe/Amsterdam")
    row = await repo.get(1)
    assert row["bio"] == "Hoi!" and row["timezone"] == "Europe/Amsterdam"
    assert row["vehicles"] is None
    # Partial update leaves the other fields alone.
    await repo.set_fields(1, vehicles="42 engines")
    row = await repo.get(1)
    assert row["bio"] == "Hoi!" and row["vehicles"] == "42 engines"
    # An empty string clears a field.
    await repo.set_fields(1, bio="")
    assert (await repo.get(1))["bio"] is None


async def test_profile_rejects_unknown_fields(db):
    repo = MemberProfilesRepo(db)
    with pytest.raises(ValueError):
        await repo.set_fields(1, hacker="nope")


async def test_profile_sections_cover_all_columns():
    # Every DB profile column is editable through exactly the modal
    # sections /profile-edit offers.
    section_fields = {
        field
        for _, fields in SECTIONS.values()
        for field, *_ in fields
    }
    assert section_fields == set(PROFILE_FIELDS)


async def test_birthday_validation():
    assert validate_birthday("17-07") == "17-07"
    assert validate_birthday("7-3") == "07-03"
    assert validate_birthday("17-07-1990") == "17-07-1990"
    assert validate_birthday("") == ""          # clears
    assert validate_birthday("31-02") == "31-02"  # loose: day bound only
    assert validate_birthday("32-01") is None
    assert validate_birthday("17-13") is None
    assert validate_birthday("morgen") is None


# -- action log ---------------------------------------------------------------

async def test_action_log_history_and_feed_watermark(db):
    repo = MemberActionsRepo(db)
    a1 = await repo.log(
        discord_user_id=1, mc_user_id=101, actor_name="Alice",
        action="training_requested", detail="HazMat ×1",
    )
    await repo.log(
        discord_user_id=1, mc_user_id=None, actor_name="Alice",
        action="profile_updated", detail="section: Over mij",
    )
    rows = await repo.for_member(discord_user_id=1)
    assert [r["action"] for r in rows] == ["profile_updated", "training_requested"]
    # Matching on MC id alone also finds the actions.
    assert len(await repo.for_member(mc_user_id=101)) == 1

    pending = await repo.pending_feed()
    assert [r["id"] for r in pending] == [a1, a1 + 1]
    await repo.mark_posted(a1)
    assert [r["id"] for r in await repo.pending_feed()] == [a1 + 1]


async def test_bot_helper_never_raises(db):
    from fra_bot.bot import FRABot

    bot = SimpleNamespace(db=db)
    await FRABot.log_member_action(
        bot, action="training_requested", detail="x",
        discord_user_id=5, actor_name="Bob",
    )
    assert len(await MemberActionsRepo(db).for_member(discord_user_id=5)) == 1
    # A broken DB must be swallowed, not raised into the member's action.
    bot_broken = SimpleNamespace(db=SimpleNamespace(conn=None))
    await FRABot.log_member_action(bot_broken, action="x")


# -- timeline merge -------------------------------------------------------------

async def test_timeline_includes_bot_actions(db):
    from fra_bot.services.timeline import build_timeline

    await MemberActionsRepo(db).log(
        discord_user_id=1, mc_user_id=101, actor_name="Alice",
        action="training_requested", detail="HazMat ×1 (request #9)",
    )
    events = await build_timeline(db, mc_user_id=101, discord_user_id=1)
    assert any(
        e.source == "bot" and "training requested" in e.title for e in events
    )


# -- feed publisher --------------------------------------------------------------

async def test_feed_suppresses_when_channel_off(db):
    from fra_bot.cogs.notifications import NotificationsCog

    cog = NotificationsCog.__new__(NotificationsCog)
    cog.bot = SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(discord=SimpleNamespace(
            channels=SimpleNamespace(member_actions=0),
        )),
        get_channel=lambda cid: None,
    )
    repo = MemberActionsRepo(db)
    await repo.log(discord_user_id=1, mc_user_id=None, actor_name="A",
                   action="x", detail=None)
    await cog._publish_member_actions()
    # Feed off: suppressed (marked posted), so enabling later won't flood.
    assert await repo.pending_feed() == []


async def test_feed_posts_to_channel_and_marks(db):
    from fra_bot.cogs.notifications import NotificationsCog

    class FakeChannel:
        def __init__(self):
            self.sent = []

        async def send(self, embed=None, **kwargs):
            self.sent.append(embed)

    channel = FakeChannel()
    cog = NotificationsCog.__new__(NotificationsCog)
    cog.bot = SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(discord=SimpleNamespace(
            channels=SimpleNamespace(member_actions=99),
        )),
        get_channel=lambda cid: channel if cid == 99 else None,
    )
    repo = MemberActionsRepo(db)
    await repo.log(discord_user_id=1, mc_user_id=None, actor_name="Alice",
                   action="training_requested", detail="HazMat ×1")
    await cog._publish_member_actions()
    assert len(channel.sent) == 1
    assert "training requested" in channel.sent[0].description
    assert await repo.pending_feed() == []
