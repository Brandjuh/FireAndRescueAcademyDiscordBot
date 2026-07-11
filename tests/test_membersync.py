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
    # A self-supplied MC id is NOT proof of ownership — anyone could claim
    # any account with it. The id path is dead; admins use !link instead.
    assert await svc.lookup(None, 42) is None


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


async def _seed_join_log(db, name, mc_id, *, hours_ago=0):
    import datetime as dt

    event_at = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    ).isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO alliance_logs (signature, occurrence_index, raw_timestamp, "
        "event_at, action_key, description, affected_name, affected_mc_id, "
        "scraped_at) VALUES (?, 1, 'now', ?, 'added_to_alliance', "
        "'added to the alliance', ?, ?, ?)",
        (f"sig-{name}-{hours_ago}", event_at, name, mc_id, event_at),
    )


class _LogsPage:
    def __init__(self, rows, has_table=True):
        self.rows = rows
        self.has_table = has_table


class _FakeMC:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.fetches = 0

    async def fetch_page(self, path, **kwargs):
        self.fetches += 1
        if self.fail:
            raise RuntimeError("circuit open")
        return "<html>logs</html>"


async def test_fresh_join_verifies_instantly_from_stored_logs(db):
    """Roster hasn't swept yet, but the join is in our stored logs (synced
    every 15 min) → instant verification, no queue, no polling."""
    svc = MemberSyncService(db)
    await _seed_join_log(db, "FreshJoiner", 777)
    outcome = await svc.request_verification(100, "freshjoiner", None, 1)
    assert outcome.outcome == "approved_from_logs"
    assert outcome.mc_user_id == 777
    assert await svc.links.queue_get(100) is None


async def test_fresh_join_verifies_via_live_log_check(db, monkeypatch):
    """Not in the roster, not in stored logs (log sync hasn't run) → one
    live fetch of the newest log page settles it."""
    svc = MemberSyncService(db, mc=_FakeMC())
    monkeypatch.setattr(
        "fra_bot.services.membersync.parse_logs_page",
        lambda html: _LogsPage([
            {"action_key": "added_to_alliance", "affected_name": "LiveJoiner",
             "affected_mc_id": 888},
        ]),
    )
    outcome = await svc.request_verification(200, "LiveJoiner", None, 1)
    assert outcome.outcome == "approved_from_logs"
    assert outcome.mc_user_id == 888


async def test_definitive_log_miss_reports_name_mismatch(db, monkeypatch):
    """Logs readable, no join by this name → the nickname is wrong; say so
    instead of queueing a check that cannot succeed."""
    svc = MemberSyncService(db, mc=_FakeMC())
    monkeypatch.setattr(
        "fra_bot.services.membersync.parse_logs_page",
        lambda html: _LogsPage([
            {"action_key": "added_to_alliance", "affected_name": "SomeoneElse",
             "affected_mc_id": 1},
        ]),
    )
    outcome = await svc.request_verification(300, "WrongNick", None, 1)
    assert outcome.outcome == "name_mismatch"
    assert await svc.links.queue_get(300) is None  # NOT parked in the queue


async def test_unreachable_logs_fall_back_to_queue_with_eta(db):
    """Circuit breaker / network down → the old queue takes over, with an
    ETA computed from the actual members-sweep schedule."""
    import datetime as dt

    svc = MemberSyncService(db, mc=_FakeMC(fail=True))
    # A members sweep finished 10 minutes ago.
    run_id = await svc.runs.start("members")
    await svc.runs.finish(run_id, status="success")
    outcome = await svc.request_verification(400, "Somebody", None, 1)
    assert outcome.outcome == "queued"
    assert outcome.roster_eta is not None
    now = dt.datetime.now(dt.timezone.utc)
    assert now < outcome.roster_eta < now + dt.timedelta(hours=2)
    assert await svc.links.queue_get(400) is not None


async def test_low_contribution_travels_with_the_approval(db):
    svc = MemberSyncService(db)
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (50, 'Cheapskate', 0.0, 1, "
        "'2026-01-01', '2026-07-01')"
    )
    outcome = await svc.request_verification(500, "Cheapskate", None, 1)
    assert outcome.outcome == "approved"
    assert outcome.contribution_rate == 0.0


async def test_prune_spares_links_fresher_than_the_roster_sweep(db):
    """A link made from the join logs predates its roster row; the hourly
    prune must not strip the role during that gap."""
    svc = MemberSyncService(db)
    for mc_id in range(1000, 1000 + 150):  # healthy roster (> safety floor)
        await _seed_member(db, mc_id, f"Member{mc_id}")
    await _seed_join_log(db, "JustJoined", 9999)
    outcome = await svc.request_verification(600, "JustJoined", None, 1)
    assert outcome.outcome == "approved_from_logs"
    # 9999 is not in the roster yet — but the link is fresh: spared.
    assert await svc.prune_candidates() == []
    # Once the link is older than the grace window, it prunes normally.
    await db.execute(
        "UPDATE member_links SET updated_at = '2026-01-01T00:00:00' "
        "WHERE discord_id = 600"
    )
    assert (600, 9999) in await svc.prune_candidates()


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
    # Fresh links sit in the prune grace window (see the log-verify flow);
    # age the link past it — leavers then derole as before.
    await db.execute(
        "UPDATE member_links SET updated_at = '2026-01-01T00:00:00' "
        "WHERE discord_id = 100"
    )
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
