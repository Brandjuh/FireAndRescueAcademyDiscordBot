"""New-member welcome: join detection, one-time greeting, dedupe, dry-run
and the baseline that stops enabling from greeting the whole roster."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MembersRepo, RunsRepo
from fra_bot.services.welcome import WelcomeService

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "welcome.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _member(mc_id, name, role="Member"):
    return {
        "mc_user_id": mc_id, "name": name, "role": role,
        "earned_credits": 100, "contribution_rate": 5.0,
        "raw_member_since": "01/01/2025",
    }


def _cfg(enabled=True, dry_run=False,
         message="@{name} Welcome! Please check your DM for more information."):
    return SimpleNamespace(automation=SimpleNamespace(
        dry_run=dry_run,
        welcome=SimpleNamespace(enabled=enabled, message=message),
    ))


class FakeChat:
    def __init__(self, fail=False):
        self.posted = []
        self._fail = fail

    async def post_message(self, text):
        if self._fail:
            from fra_bot.mc.errors import FetchError

            raise FetchError("/", 503, "chat down")
        self.posted.append(text)
        return text


async def _join(db, mc_id, name):
    """Run a roster sync that makes (mc_id, name) a fresh join."""
    members = MembersRepo(db)
    runs = RunsRepo(db)
    # Baseline first (silent), then a second sync where the member appears.
    r1 = await runs.start("members")
    existing = [_member(999, "Seed")]
    await members.apply_roster(r1, existing, detect_changes=False)
    r2 = await runs.start("members")
    await members.apply_roster(
        r2, existing + [_member(mc_id, name)], detect_changes=True
    )


async def test_new_join_is_welcomed_once(db):
    await _join(db, 101, "Rookie")
    chat = FakeChat()
    service = WelcomeService(_cfg(), db, chat)

    assert await service.run() == 1
    assert chat.posted == [
        "@Rookie Welcome! Please check your DM for more information."
    ]
    # A second tick greets nobody — the event is marked welcomed.
    assert await service.run() == 0
    assert len(chat.posted) == 1


async def test_switch_off_is_a_no_op(db):
    await _join(db, 101, "Rookie")
    chat = FakeChat()
    assert await WelcomeService(_cfg(enabled=False), db, chat).run() == 0
    assert chat.posted == []


async def test_dry_run_marks_without_posting(db):
    await _join(db, 101, "Rookie")
    chat = FakeChat()
    service = WelcomeService(_cfg(dry_run=True), db, chat)
    assert await service.run() == 1        # counted as handled
    assert chat.posted == []               # but nothing hit the game
    assert await service.run() == 0        # and not repeated


async def test_a_failed_post_is_retried_next_tick(db):
    await _join(db, 101, "Rookie")
    service = WelcomeService(_cfg(), db, FakeChat(fail=True))
    assert await service.run() == 0        # post failed, nothing marked
    # Now the chat recovers: the same join is still pending and gets greeted.
    ok_chat = FakeChat()
    service.chat = ok_chat
    assert await service.run() == 1
    assert ok_chat.posted


async def test_existing_members_are_not_greeted_on_enable(db):
    # The migration baselines existing joined events as welcomed. Simulate
    # that: a join event that predates the feature is already welcomed.
    await _join(db, 101, "Old")
    await db.execute(
        "UPDATE member_events SET welcomed_at = occurred_at "
        "WHERE welcomed_at IS NULL"
    )
    chat = FakeChat()
    assert await WelcomeService(_cfg(), db, chat).run() == 0
    assert chat.posted == []


async def test_a_member_who_left_again_is_not_greeted(db):
    await _join(db, 101, "Fleeting")
    # They leave before the welcome tick: is_active goes 0.
    await db.execute(
        "UPDATE members SET is_active = 0 WHERE mc_user_id = 101"
    )
    chat = FakeChat()
    assert await WelcomeService(_cfg(), db, chat).run() == 0
    assert chat.posted == []


async def test_stale_joins_are_skipped_but_marked(db):
    await _join(db, 101, "LateComer")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
    await db.execute(
        "UPDATE member_events SET occurred_at = ? WHERE event_type = 'joined'",
        (old,),
    )
    chat = FakeChat()
    service = WelcomeService(_cfg(), db, chat)
    assert await service.run() == 0        # too old to greet
    assert chat.posted == []
    # Marked, so it doesn't linger forever in the pending set.
    assert await MembersRepo(db).pending_welcomes() == []


async def test_message_template_without_name_field_still_posts(db):
    await _join(db, 101, "Rookie")
    chat = FakeChat()
    service = WelcomeService(_cfg(message="Welcome aboard!"), db, chat)
    assert await service.run() == 1
    assert chat.posted == ["Welcome aboard!"]
