"""MemberSync: roster lookup, verify flow, retry queue, and the prune
safety gate — all against our own members table."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MembersRepo
from fra_bot.services.membersync import (
    MIN_SAFE_ROSTER_COUNT,
    QUEUE_MAX_ATTEMPTS,
    MemberSyncService,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "ms.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _seed_member(db, mc_id, name, *, active=1):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, is_active, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, '2026-01-01', '2026-07-01')",
        (mc_id, name, active),
    )


async def test_lookup_by_name_is_case_insensitive_and_active_only(db):
    svc = MemberSyncService(db)
    await _seed_member(db, 42, "DutchFireFighter")
    await _seed_member(db, 43, "GoneMember", active=0)
    row = await svc.lookup("dutchfirefighter", None)
    assert row is not None and row["mc_user_id"] == 42
    assert await svc.lookup("GoneMember", None) is None       # left = no match
    assert await svc.lookup(None, 43) is None                 # by id: also active-only
    assert (await svc.lookup(None, 42))["name"] == "DutchFireFighter"


async def test_verify_approves_on_match_and_queues_on_miss(db):
    svc = MemberSyncService(db)
    await _seed_member(db, 42, "Alice")
    ok = await svc.request_verification(100, "Alice", None, 1)
    assert ok.outcome == "approved" and ok.mc_user_id == 42
    # Second call: already verified.
    again = await svc.request_verification(100, "Alice", None, 1)
    assert again.outcome == "already_verified"

    miss = await svc.request_verification(200, "Bob", None, 1)
    assert miss.outcome == "queued"
    twice = await svc.request_verification(200, "Bob", None, 1)
    assert twice.outcome == "already_queued"


async def test_backfill_matches_links_only_unlinked_roster_matches(db):
    """`!verifyall`: existing Discord members whose nickname matches the
    roster are picked up; already-linked members and non-members are not."""
    svc = MemberSyncService(db)
    await _seed_member(db, 42, "Alice")
    await _seed_member(db, 43, "Bob")
    await _seed_member(db, 44, "GoneMember", active=0)
    # Alice is already linked (she verified herself earlier).
    await svc.request_verification(100, "Alice", None, 1)

    matches = await svc.backfill_matches({
        100: "Alice",          # already linked → skipped
        200: "bob",            # case-insensitive roster match → linked
        300: "Charlie",        # not in the alliance → skipped
        400: "GoneMember",     # left the alliance → skipped
        500: None,             # no nickname available → skipped
    })
    assert [(d, row["mc_user_id"]) for d, row in matches] == [(200, 43)]


def test_queue_window_straddles_one_full_members_sweep():
    """A fresh alliance join appears in the roster up to ~75 min later
    (hourly sweep + jitter + sweep runtime); the retry window must be
    comfortably longer, or verification expires right before the roster
    catches up."""
    assert QUEUE_MAX_ATTEMPTS * 2 >= 90  # minutes at the 2-minute loop


async def test_queue_approves_when_roster_catches_up(db):
    svc = MemberSyncService(db)
    await svc.request_verification(200, "Bob", None, 1)
    # Roster sync lands between polls.
    await _seed_member(db, 77, "Bob")
    results = await svc.process_queue({200: "Bob"})
    assert results == [(200, "approved", 77)]
    link = await svc.links.get_by_discord(200)
    assert link["status"] == "approved" and link["mc_user_id"] == 77
    assert await svc.links.queue_get(200) is None


async def test_queue_expires_after_max_attempts_and_drops_leavers(db):
    svc = MemberSyncService(db)
    await svc.request_verification(200, "Bob", None, 1)
    await svc.request_verification(300, "Carol", None, 1)
    # Carol left Discord: dropped as 'gone' on the very first pass.
    first = await svc.process_queue({200: "Bob"})
    assert (300, "gone", None) in first
    # Bob is never found: expires within the bounded attempts.
    expired = False
    for _ in range(QUEUE_MAX_ATTEMPTS + 1):
        results = await svc.process_queue({200: "Bob"})
        if (200, "expired", None) in results:
            expired = True
            break
    assert expired
    assert await svc.links.queue_get(200) is None


async def test_reverify_steals_mc_id_from_stale_link(db):
    """People re-verify after renames/new accounts: the MC id moves to the
    new Discord account instead of tripping the UNIQUE index."""
    svc = MemberSyncService(db)
    await _seed_member(db, 42, "Alice")
    await svc.request_verification(100, "Alice", None, 1)
    ok = await svc.request_verification(999, "Alice", None, 1)
    assert ok.outcome == "approved" and ok.mc_user_id == 42
    assert await svc.links.get_by_discord(100) is None        # stale link gone
    assert (await svc.links.get_by_discord(999))["mc_user_id"] == 42


async def test_prune_flags_leavers_with_safety_gate(db):
    svc = MemberSyncService(db)
    # A healthy roster: enough active members for the gate.
    for i in range(MIN_SAFE_ROSTER_COUNT + 5):
        await _seed_member(db, 1000 + i, f"Member{i}")
    await _seed_member(db, 42, "Alice")
    await svc.request_verification(100, "Alice", None, 1)
    assert await svc.prune_candidates() == []                 # still active

    await db.execute("UPDATE members SET is_active = 0 WHERE mc_user_id = 42")
    assert await svc.prune_candidates() == [(100, 42)]        # left -> derole

    # A collapsed roster (broken scrape) must never mass-derole.
    await db.execute("UPDATE members SET is_active = 0")
    assert await svc.prune_candidates() == []


def test_setup_hook_imports_every_cog_it_registers():
    """Regression for the NameError crash: every Cog class used in
    setup_hook's add_cog calls must be imported there too."""
    import re
    from pathlib import Path

    src = Path("fra_bot/bot.py").read_text()
    registered = set(re.findall(r"add_cog\((\w+)\(self\)\)", src))
    imported = set(re.findall(r"from \.cogs\.\w+ import ([\w, ]+)", src))
    imported_names = {
        name.strip() for group in imported for name in group.split(",")
    }
    missing = registered - imported_names
    assert not missing, f"cogs registered but not imported: {missing}"


def test_membersync_cog_module_imports():
    from fra_bot.cogs.membersync import MemberSyncCog  # noqa: F401
