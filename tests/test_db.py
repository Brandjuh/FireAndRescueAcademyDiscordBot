import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import (
    ApplicationsRepo,
    LogsRepo,
    MembersRepo,
    RunsRepo,
    StateRepo,
    TreasuryRepo,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_migrations_apply_once(db):
    async with db.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations") as cur:
        row = await cur.fetchone()
    assert row["n"] >= 1
    # Re-running migrate must be a no-op.
    await db._migrate()


async def test_state_repo_roundtrip(db):
    state = StateRepo(db)
    assert await state.get("missing") is None
    assert await state.get("missing", "fallback") == "fallback"
    await state.set("k", "v1")
    await state.set("k", "v2")
    assert await state.get("k") == "v2"
    await state.delete("k")
    assert await state.get("k") is None


async def test_applications_received_counts_accepted_and_denied(db):
    logs = LogsRepo(db)
    now = "2026-07-07T12:00:00+00:00"
    for sig, action in (
        ("a1", "added_to_alliance"),
        ("a2", "added_to_alliance"),
        ("d1", "application_denied"),
        ("c1", "contributed_to_alliance"),  # not an application outcome
        ("l1", "left_alliance"),
    ):
        await db.execute(
            "INSERT INTO alliance_logs (signature, occurrence_index, raw_timestamp, "
            "action_key, description, scraped_at) VALUES (?, 1, ?, ?, ?, ?)",
            (sig, now, action, "x", now),
        )
    result = await logs.applications_received()
    assert result.get("added_to_alliance") == 2
    assert result.get("application_denied") == 1
    assert "contributed_to_alliance" not in result
    assert "left_alliance" not in result


def _member(mc_id, name, role="Member", credits=100, rate=5.0):
    return {
        "mc_user_id": mc_id,
        "name": name,
        "role": role,
        "earned_credits": credits,
        "contribution_rate": rate,
        "raw_member_since": "01/01/2025",
    }


async def test_members_roster_lifecycle(db):
    members = MembersRepo(db)
    runs = RunsRepo(db)

    run1 = await runs.start("members")
    events = await members.apply_roster(
        run1, [_member(1, "Alice"), _member(2, "Bob")], detect_changes=False
    )
    assert events == []  # first sync is silent
    assert await members.active_count() == 2

    # Second run: Bob leaves, Carol joins, Alice changes role + rate.
    run2 = await runs.start("members")
    events = await members.apply_roster(
        run2,
        [_member(1, "Alice", role="Admin", rate=7.5), _member(3, "Carol")],
        detect_changes=True,
    )
    types = sorted(e["event_type"] for e in events)
    assert types == ["contribution_changed", "joined", "left", "role_changed"]
    assert await members.active_count() == 2

    pending = await members.pending_events()
    assert len(pending) == 4
    await members.mark_event_posted(pending[0]["id"])
    assert len(await members.pending_events()) == 3

    # Bob returns: reactivation is a join again.
    run3 = await runs.start("members")
    events = await members.apply_roster(
        run3,
        [_member(1, "Alice", role="Admin", rate=7.5), _member(2, "Bob"), _member(3, "Carol")],
        detect_changes=True,
    )
    assert [e["event_type"] for e in events] == ["joined"]


async def test_applications_lifecycle(db):
    apps = ApplicationsRepo(db)

    new = await apps.upsert_seen(
        [{"application_id": 9001, "applicant_name": "NewGuy", "mc_user_id": 555}]
    )
    assert new == [9001]
    # Same listing again: not new.
    new = await apps.upsert_seen(
        [{"application_id": 9001, "applicant_name": "NewGuy", "mc_user_id": 555}]
    )
    assert new == []
    assert await apps.open_count() == 1

    pending = await apps.pending_announcements()
    assert len(pending) == 1
    await apps.mark_posted(9001)
    assert await apps.pending_announcements() == []

    # Application disappears -> resolved.
    await apps.upsert_seen([])
    assert await apps.open_count() == 0


def _log_row(sig, desc="X added to the alliance", ts="06 Jul 14:23"):
    return {
        "signature": sig,
        "raw_timestamp": ts,
        "event_at": None,
        "action_key": "added_to_alliance",
        "description": desc,
        "executed_name": "Admin",
        "executed_mc_id": 1,
        "affected_name": "X",
        "affected_type": "user",
        "affected_mc_id": 2,
        "contribution_amount": None,
    }


async def test_logs_occurrence_dedup(db):
    logs = LogsRepo(db)

    # Batch with two IDENTICAL rows (real repeated events) + one unique.
    batch = [_log_row("sigA"), _log_row("sigA"), _log_row("sigB")]
    assert await logs.insert_batch(batch) == 3

    # Re-scrape of the same window inserts nothing.
    assert await logs.insert_batch(batch) == 0

    # A third identical event appears: only the new occurrence lands.
    batch3 = [_log_row("sigA"), _log_row("sigA"), _log_row("sigA"), _log_row("sigB")]
    assert await logs.insert_batch(batch3) == 1
    assert await logs.count() == 4

    known = await logs.known_signatures(["sigA", "sigB", "sigC"])
    assert known == {"sigA", "sigB"}

    pending = await logs.pending_posts()
    assert len(pending) == 4
    await logs.mark_posted(pending[0]["id"])
    assert len(await logs.pending_posts()) == 3
    assert await logs.mark_all_posted() == 3


def _expense(sig, amount=1000, user="Carl"):
    return {
        "signature": sig,
        "raw_date": "06 Jul 14:23",
        "event_at": None,
        "username": user,
        "amount": amount,
        "description": "Course Lessons",
    }


async def test_expenses_staging_finalize_order(db):
    treasury = TreasuryRepo(db)

    # Walked pages newest->oldest: display order n3, n2, n1 (n3 newest).
    await treasury.staging_append([_expense("n3"), _expense("n2")])
    await treasury.staging_append([_expense("n1")])
    assert await treasury.staging_count() == 3
    assert await treasury.staging_tail_signatures(2) == ["n2", "n1"]

    copied = await treasury.staging_finalize("bf_done", "bf_next")
    assert copied == 3
    assert await treasury.staging_count() == 0
    # Chronological in expenses: oldest (n1) first.
    assert await treasury.newest_signatures(10) == ["n1", "n2", "n3"]
    # Done flag flipped atomically with the ledger commit.
    assert await StateRepo(db).get("bf_done") == "1"

    # Incremental append keeps chronological order.
    await treasury.insert_expenses_chronological([_expense("n4"), _expense("n5")])
    assert await treasury.newest_signatures(3) == ["n3", "n4", "n5"]
    assert await treasury.expense_count() == 5


async def test_income_snapshots_and_balance(db):
    treasury = TreasuryRepo(db)

    await treasury.record_balance(123456)
    balance = await treasury.latest_balance()
    assert balance["total_funds"] == 123456

    entries = [
        {"username": "Alice", "mc_user_id": 1, "amount": 500},
        {"username": "Bob", "mc_user_id": 2, "amount": 300},
    ]
    await treasury.store_income_snapshot("daily", "2026-07-06", entries)
    # A later snapshot for the same day supersedes the earlier one.
    entries2 = [{"username": "Alice", "mc_user_id": 1, "amount": 900}]
    await treasury.store_income_snapshot("daily", "2026-07-06", entries2)

    rows = await treasury.latest_snapshot("daily", "2026-07-06")
    assert len(rows) == 1
    assert rows[0]["amount"] == 900
    assert rows[0]["rank"] == 1

    assert await treasury.latest_snapshot("daily", "2026-07-07") == []
