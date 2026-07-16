"""Member audit timeline (reference: MemberManager audit helpers)."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import LogsRepo, SanctionsRepo
from fra_bot.services.timeline import (
    EXCLUDED_ACTION_KEYS,
    PERSON_AUDIT_ACTION_KEYS,
    build_timeline,
    render_timeline,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "timeline.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _seed_member_event(db, *, mc=42, name="Alice", etype="joined",
                             old=None, new=None, at="2026-07-01T10:00:00"):
    await db.execute(
        "INSERT INTO member_events (mc_user_id, name, event_type, old_value, "
        "new_value, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
        (mc, name, etype, old, new, at),
    )


def _log(sig, action, *, affected="Alice", affected_id=42, at="2026-07-02T09:00:00"):
    return {
        "signature": sig, "raw_timestamp": "02 Jul 09:00", "event_at": at,
        "action_key": action, "description": f"{action} for {affected}",
        "executed_name": "AdminGuy", "executed_mc_id": 7,
        "affected_name": affected, "affected_type": "profile",
        "affected_mc_id": affected_id, "contribution_amount": None,
    }


async def test_course_completions_never_pollute_a_person_timeline():
    # The reference bot's load-bearing exclusion.
    assert "course_completed" in EXCLUDED_ACTION_KEYS
    assert "course_completed" not in PERSON_AUDIT_ACTION_KEYS
    assert "contributed_to_alliance" not in PERSON_AUDIT_ACTION_KEYS


async def test_timeline_merges_sources_newest_first(db):
    await _seed_member_event(db, at="2026-07-01T10:00:00")           # joined
    await LogsRepo(db).insert_batch([
        _log("s1", "kicked_from_alliance", at="2026-07-03T12:00:00"),
        _log("s2", "course_completed", at="2026-07-03T13:00:00"),    # excluded
    ], mark_posted=True)
    await SanctionsRepo(db).add(
        mc_user_id=42, mc_username="Alice", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Warning - Official 1st warning", reason="spam",
    )  # created_at = now (newest)

    events = await build_timeline(db, mc_user_id=42, name="Alice")
    titles = [e.title for e in events]
    assert titles[0].startswith("Warning")            # newest first
    assert "kicked from alliance" in titles
    assert "joined" in titles
    assert not any("course" in t for t in titles)     # excluded stays out

    text = render_timeline("Alice", events)
    assert "Timeline for **Alice**" in text
    assert "🚨" in text and "🎮" in text and "✅" in text


async def test_timeline_matches_logs_on_name_when_id_missing(db):
    await LogsRepo(db).insert_batch([
        _log("s3", "chat_ban_set", affected="Bob", affected_id=None),
    ], mark_posted=True)
    events = await build_timeline(db, name="bob")
    assert [e.title for e in events] == ["chat ban set"]


async def test_timeline_shows_role_change_values(db):
    await _seed_member_event(
        db, etype="role_changed", old="Member", new="Staff",
        at="2026-07-04T08:00:00",
    )
    events = await build_timeline(db, mc_user_id=42)
    assert events[0].detail == ": Member → Staff"


async def test_empty_timeline_renders_friendly_message(db):
    events = await build_timeline(db, mc_user_id=999)
    assert events == []
    assert "No recorded history" in render_timeline("Ghost", events)
