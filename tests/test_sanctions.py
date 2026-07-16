"""Sanctions register (reference: sanctionmanager) — repo + type mapping."""

import pytest
import pytest_asyncio

from fra_bot.cogs.sanctions import SANCTION_TYPE_KEYS, resolve_type, type_colour
from fra_bot.db.database import Database
from fra_bot.db.repos import SanctionsRepo

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "sanctions.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _add(repo, *, type_key="w1", mc=101, name="Alice", discord_id=None):
    return await repo.add(
        mc_user_id=mc, mc_username=name, discord_user_id=discord_id,
        admin_discord_id=1, admin_name="Admin",
        sanction_type=resolve_type(type_key), reason="testing",
    )


async def test_type_keys_cover_all_reference_labels():
    # Every one of the reference bot's 16 sanction types is addressable.
    assert len(SANCTION_TYPE_KEYS) == 16
    assert resolve_type("W1") == "Warning - Official 1st warning"
    assert resolve_type("mute14d") == "Mute 14d"
    assert resolve_type("nope") is None
    # Colour mapping never crashes on any known type.
    for label in SANCTION_TYPE_KEYS.values():
        type_colour(label)


async def test_add_get_and_member_history(db):
    repo = SanctionsRepo(db)
    sid = await _add(repo, type_key="verbal")
    await _add(repo, type_key="w1")
    row = await repo.get(sid)
    assert row["mc_username"] == "Alice" and row["status"] == "active"
    rows = await repo.for_member(mc_user_id=101)
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]  # newest first


async def test_official_warning_count_ignores_verbal_and_revoked(db):
    repo = SanctionsRepo(db)
    await _add(repo, type_key="verbal")
    first = await _add(repo, type_key="w1")
    await _add(repo, type_key="w2")
    assert await repo.official_warning_count(mc_user_id=101) == 2
    # Revoking one drops the escalation counter.
    assert await repo.revoke(first, revoked_by="Admin") is True
    assert await repo.official_warning_count(mc_user_id=101) == 1
    # Double revoke is rejected.
    assert await repo.revoke(first, revoked_by="Admin") is False


async def test_for_member_matches_name_case_insensitive(db):
    repo = SanctionsRepo(db)
    await repo.add(
        mc_user_id=None, mc_username="Bob", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type=resolve_type("kick"), reason="left mid-mission",
    )
    rows = await repo.for_member(name="bob")
    assert len(rows) == 1 and rows[0]["sanction_type"] == "Kick"


async def test_stats_groups_by_type_and_status(db):
    repo = SanctionsRepo(db)
    await _add(repo, type_key="w1", mc=1, name="A")
    await _add(repo, type_key="w1", mc=2, name="B")
    kicked = await _add(repo, type_key="kick", mc=3, name="C")
    await repo.revoke(kicked, revoked_by="Admin")
    stats = {(r["sanction_type"], r["status"]): r["n"] for r in await repo.stats()}
    assert stats[("Warning - Official 1st warning", "active")] == 2
    assert stats[("Kick", "revoked")] == 1
