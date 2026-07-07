"""Regression tests for the post-review hardening batch."""

import asyncio

import pytest
import pytest_asyncio

from fra_bot.core.pacing import HumanPacer
from fra_bot.db.database import Database
from fra_bot.db.repos import RunsRepo


# ---------------------------------------------------------------------
# L18: windowed circuit breaker trips on intermittent failures
# ---------------------------------------------------------------------

def test_circuit_breaker_trips_on_intermittent_failures():
    pacer = HumanPacer(
        min_delay=0.01, max_delay=0.02, max_per_minute=100,
        failure_threshold=5, cooldown_seconds=1.0, failure_window_seconds=600.0,
    )
    # Interleave successes between failures — the old consecutive-counter
    # breaker would never trip; the windowed one must.
    for _ in range(5):
        pacer.record_failure()
        pacer.record_success()
    assert pacer.circuit_open


def test_circuit_breaker_stays_closed_below_threshold():
    pacer = HumanPacer(
        min_delay=0.01, max_delay=0.02, max_per_minute=100,
        failure_threshold=5, cooldown_seconds=1.0,
    )
    for _ in range(4):
        pacer.record_failure()
    assert not pacer.circuit_open


async def test_old_failures_expire_by_time():
    pacer = HumanPacer(
        min_delay=0.01, max_delay=0.02, max_per_minute=100,
        failure_threshold=3, cooldown_seconds=1.0, failure_window_seconds=0.08,
    )
    pacer.record_failure()
    pacer.record_failure()
    await asyncio.sleep(0.12)  # first two age out of the window
    pacer.record_failure()
    assert not pacer.circuit_open  # only 1 failure inside the window now


# ---------------------------------------------------------------------
# L19: orphaned 'running' scrape runs are closed at startup
# ---------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "h.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_close_orphans_marks_running_as_failed(db):
    runs = RunsRepo(db)
    r1 = await runs.start("members")   # left running
    r2 = await runs.start("logs")
    await runs.finish(r2, status="success")

    closed = await runs.close_orphans()
    assert closed == 1  # only the unfinished one

    rows = {r["id"]: r for r in await runs.recent(limit=5)}
    assert rows[r1]["status"] == "failed"
    assert rows[r1]["finished_at"] is not None
    assert rows[r2]["status"] == "success"  # untouched


# ---------------------------------------------------------------------
# H2: migrations are atomic + idempotent (replay-safe)
# ---------------------------------------------------------------------

async def test_migrations_replay_is_idempotent(tmp_path):
    path = tmp_path / "m.sqlite3"
    db1 = Database(path)
    await db1.connect()
    async with db1.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations") as cur:
        first = (await cur.fetchone())["n"]
    await db1.close()

    # Reopen: migrations must not re-run or error.
    db2 = Database(path)
    await db2.connect()
    async with db2.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations") as cur:
        second = (await cur.fetchone())["n"]
    await db2.close()

    assert first == second >= 2
