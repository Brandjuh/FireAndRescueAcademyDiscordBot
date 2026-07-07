"""Tests for the presence status logic (what the bot shows it's doing)."""

from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MembersRepo, RunsRepo
from fra_bot.presence import PresenceManager

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "p.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _fake_bot(db, *, circuit_open=False):
    return SimpleNamespace(db=db, pacer=SimpleNamespace(circuit_open=circuit_open))


async def _seed_members(db, n):
    runs = RunsRepo(db)
    members = MembersRepo(db)
    run = await runs.start("members")
    roster = [
        {"mc_user_id": i, "name": f"M{i}", "role": "Member",
         "earned_credits": 100, "contribution_rate": 5.0, "raw_member_since": "x"}
        for i in range(1, n + 1)
    ]
    await members.apply_roster(run, roster, detect_changes=False)


async def test_idle_summary_shows_member_count(db):
    await _seed_members(db, 3)
    pm = PresenceManager(_fake_bot(db))
    text, status = await pm._desired()
    assert status == discord.Status.online
    assert "3 members" in text
    assert text.startswith("👀")


async def test_running_job_shows_action(db):
    pm = PresenceManager(_fake_bot(db))
    pm.mark_running("members")
    text, status = await pm._desired()
    assert "syncing members" in text
    assert text.startswith("🔄")
    assert status == discord.Status.online


async def test_multiple_jobs_show_count(db):
    pm = PresenceManager(_fake_bot(db))
    pm.mark_running("members")
    pm.mark_running("logs")
    text, _ = await pm._desired()
    assert "running 2 tasks" in text


async def test_done_reverts_to_idle(db):
    await _seed_members(db, 1)
    pm = PresenceManager(_fake_bot(db))
    pm.mark_running("members")
    pm.mark_done("members")
    text, _ = await pm._desired()
    assert text.startswith("👀")
    assert "1 members" in text


async def test_circuit_breaker_shows_paused(db):
    pm = PresenceManager(_fake_bot(db, circuit_open=True))
    pm.mark_running("members")  # even with a job queued, paused wins
    text, status = await pm._desired()
    assert "paused" in text.lower()
    assert status == discord.Status.dnd


async def test_idle_summary_survives_empty_db(db):
    pm = PresenceManager(_fake_bot(db))
    text, _ = await pm._desired()
    assert "0 members" in text  # no crash on a fresh database
