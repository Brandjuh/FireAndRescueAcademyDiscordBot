"""Member visibility fixes: tax warnings land in the action log (incl.
backfill), manual mission starts join the timeline, and training
reminders are DM-only."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import LogsRepo, MemberActionsRepo, RemindersRepo, TaxWarningsRepo
from fra_bot.services.timeline import PERSON_AUDIT_ACTION_KEYS, build_timeline

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "vis.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_manual_mission_starts_show_in_the_member_timeline(db):
    assert "large_mission_started" in PERSON_AUDIT_ACTION_KEYS
    assert "alliance_event_started" in PERSON_AUDIT_ACTION_KEYS
    # Benji starts a large mission HIMSELF (outside the bot): he is the
    # executor in the alliance log, nobody is 'affected'.
    await LogsRepo(db).insert_batch([{
        "signature": "m1", "raw_timestamp": "22 Jul 20:00",
        "event_at": "2026-07-22T20:00:00",
        "action_key": "large_mission_started",
        "description": "started the large scale mission Airport Fire",
        "executed_name": "BenjiBean1128", "executed_mc_id": 555,
        "affected_name": None, "affected_type": None, "affected_mc_id": None,
        "contribution_amount": None,
    }], mark_posted=True)
    events = await build_timeline(db, mc_user_id=555, name="BenjiBean1128",
                                  discord_user_id=None)
    assert any("large mission started" in e.title for e in events)


async def test_tax_warning_backfill_migration_semantics(db):
    # Simulate the pre-fix world: a warning exists only in tax_warnings.
    await TaxWarningsRepo(db).record_warning(555, "BenjiBean1128", count=1)
    sql = (Path("fra_bot/db/migrations/0021_tax_warning_actions.sql")
           .read_text(encoding="utf-8"))
    statement = re.sub(r"--[^\n]*", "", sql)  # strip comments
    await db.conn.executescript(statement)
    await db.conn.commit()
    rows = await MemberActionsRepo(db).for_member(mc_user_id=555)
    assert any(r["action"] == "tax_warning_sent" for r in rows)
    backfilled = [r for r in rows if r["action"] == "tax_warning_sent"]
    assert backfilled[0]["posted_at"] is not None    # never re-fed to Discord
    # Re-running the backfill must not duplicate.
    await db.conn.executescript(statement)
    await db.conn.commit()
    rows = await MemberActionsRepo(db).for_member(mc_user_id=555)
    assert len([r for r in rows if r["action"] == "tax_warning_sent"]) == 1


async def test_training_reminders_are_dm_only(db):
    from fra_bot.cogs.requests_panel import RequestsCog

    await RemindersRepo(db).add(
        discord_user_id=42, channel_id=7, training="HazMat",
        due_at="2000-01-01T00:00:00+00:00",
    )
    channel_sends, dm_sends = [], []

    class FakeUser:
        async def send(self, text):
            dm_sends.append(text)

    class FakeBot(SimpleNamespace):
        def get_channel(self, cid):
            return SimpleNamespace(send=channel_sends.append)

        async def fetch_user(self, uid):
            return FakeUser()

    cog = RequestsCog.__new__(RequestsCog)
    cog.bot = FakeBot(db=db)
    cog.reminders = RemindersRepo(db)
    await cog._send_due_reminders()
    assert channel_sends == []                # NEVER in the request channel
    assert dm_sends and "HazMat" in dm_sends[0]
    assert await RemindersRepo(db).due() == []


async def test_reminder_with_closed_dms_is_dropped_not_shouted(db):
    from fra_bot.cogs.requests_panel import RequestsCog

    await RemindersRepo(db).add(
        discord_user_id=42, channel_id=7, training="HazMat",
        due_at="2000-01-01T00:00:00+00:00",
    )
    channel_sends = []

    class FakeBot(SimpleNamespace):
        def get_channel(self, cid):
            return SimpleNamespace(send=channel_sends.append)

        async def fetch_user(self, uid):
            raise discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "closed"
            )

    cog = RequestsCog.__new__(RequestsCog)
    cog.bot = FakeBot(db=db)
    cog.reminders = RemindersRepo(db)
    await cog._send_due_reminders()
    assert channel_sends == []                # dropped, not redirected
    assert await RemindersRepo(db).due() == []   # and not retried forever
