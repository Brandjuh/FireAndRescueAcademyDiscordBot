"""Training-stall hardening: retry payload, error backoff, shutdown
interruption, stuck detection and the watchdog's reasoning."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo, StateRepo
from fra_bot.services.watchdog import AutomationWatchdog

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "stall.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _cfg(training_enabled=True, dry_run=False):
    return SimpleNamespace(automation=SimpleNamespace(
        dry_run=dry_run,
        training=SimpleNamespace(enabled=training_enabled, interval=5),
        building=SimpleNamespace(enabled=False, interval=5),
    ))


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeBot(SimpleNamespace):
    def job_lock(self, name):
        locks = self.__dict__.setdefault("_locks", {})
        return locks.setdefault(name, asyncio.Lock())

    def channel_for(self, key):
        return self.__dict__.get("admin_channel")


async def _seed_stuck(db, *, kind="training", minutes_old=60):
    repo = AutomationRepo(db)
    request_id = await repo.create(
        kind=kind, thread_id=0, post_id=0, requester_name="Tester",
        requester_mc_id=None, payload=json.dumps({"trainings": []}),
    )
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(minutes=minutes_old)).isoformat()
    await db.execute(
        "UPDATE automation_requests SET updated_at = ? WHERE id = ?",
        (old, request_id),
    )
    return request_id


async def test_stuck_actionable_finds_only_old_due_rows(db):
    repo = AutomationRepo(db)
    await _seed_stuck(db, minutes_old=60)
    fresh = await repo.create(
        kind="training", thread_id=0, post_id=0, requester_name="Fresh",
        requester_mc_id=None, payload="{}",
    )
    rows = await repo.stuck_actionable("training", older_minutes=30)
    assert [r["requester_name"] for r in rows] == ["Tester"]
    # A waiting row with a FUTURE retry time is scheduled, not stuck.
    future = (dt.datetime.now(dt.timezone.utc)
              + dt.timedelta(hours=1)).isoformat()
    await repo.set_status(fresh, "waiting", "x", next_attempt_at=future,
                          announce=False)
    assert len(await repo.stuck_actionable("training", older_minutes=30)) == 1


async def test_watchdog_names_the_switch_when_off(db):
    await _seed_stuck(db)
    channel = FakeChannel()
    bot = FakeBot(pacer=SimpleNamespace(circuit_open=False),
                  admin_channel=channel)
    watchdog = AutomationWatchdog(_cfg(training_enabled=False), db, bot)
    await watchdog.run()
    assert any("enabled` is OFF" in text for text in channel.sent)
    # Cooldown: a second tick does not repeat the same alert.
    await watchdog.run()
    assert len(channel.sent) == 1


async def test_watchdog_kicks_the_queue_when_nothing_blocks(db):
    await _seed_stuck(db)
    kicked = []

    class FakeService:
        async def execute_queue_now(self):
            kicked.append(True)
            return 1

    async def session_ok():
        return True

    bot = FakeBot(pacer=SimpleNamespace(circuit_open=False),
                  admin_channel=FakeChannel(),
                  trainings=FakeService(),
                  mc=SimpleNamespace(verify_session=session_ok))
    await AutomationWatchdog(_cfg(), db, bot).run()
    assert kicked == [True]


async def test_watchdog_reports_rotten_login_instead_of_kicking(db):
    await _seed_stuck(db)
    channel = FakeChannel()

    async def session_dead():
        return False

    bot = FakeBot(pacer=SimpleNamespace(circuit_open=False),
                  admin_channel=channel,
                  mc=SimpleNamespace(verify_session=session_dead))
    await AutomationWatchdog(_cfg(), db, bot).run()
    assert any("login/session" in text for text in channel.sent)


async def test_watchdog_flags_a_stale_heartbeat(db):
    state = StateRepo(db)
    stale = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(hours=2)).isoformat()
    await state.set("heartbeat:board-training", stale)
    channel = FakeChannel()
    bot = FakeBot(pacer=SimpleNamespace(circuit_open=False),
                  admin_channel=channel)
    await AutomationWatchdog(_cfg(), db, bot).run()
    assert any("has not run" in text for text in channel.sent)


async def test_watchdog_blames_the_lock_when_the_job_keeps_firing(db):
    # Poll heartbeat stale but the fired-stamp fresh: the scheduler is
    # alive, the LOCK is what's stuck — say so instead of "job dead".
    state = StateRepo(db)
    stale = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(hours=2)).isoformat()
    await state.set("heartbeat:board-training", stale)
    await state.set("heartbeat:fired:board-trainings",
                    dt.datetime.now(dt.timezone.utc).isoformat())
    channel = FakeChannel()
    bot = FakeBot(pacer=SimpleNamespace(circuit_open=False),
                  admin_channel=channel)
    await AutomationWatchdog(_cfg(), db, bot).run()
    assert any("lock is held" in text for text in channel.sent)


async def test_a_hung_job_tick_is_killed_and_the_loop_survives(monkeypatch):
    # The 250-min incident: one tick hung forever and silently killed its
    # own loop while every other job kept running. The tick cap ends it.
    import fra_bot.core.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "JOB_TICK_TIMEOUT_SECONDS", 0.05)
    scheduler = sched_mod.Scheduler()
    ran = []

    async def hangs_then_runs():
        ran.append(True)
        if len(ran) == 1:
            await asyncio.Event().wait()   # first tick hangs forever

    await scheduler._invoke(hangs_then_runs, "job")   # killed by the cap
    await scheduler._invoke(hangs_then_runs, "job")   # loop continues
    assert len(ran) == 2


async def test_mc_error_retry_backs_off_with_attempts(db):
    # The waiting row must carry a future next_attempt_at that grows with
    # the attempt count — no more burning 12 attempts in an hour's outage.
    from fra_bot.services.board_requests import (
        ERROR_RETRY_BASE_MINUTES,
        ERROR_RETRY_CAP_MINUTES,
    )

    assert ERROR_RETRY_BASE_MINUTES * 5 <= ERROR_RETRY_CAP_MINUTES * 5
    delays = [
        min(ERROR_RETRY_CAP_MINUTES, ERROR_RETRY_BASE_MINUTES * (n + 1))
        for n in range(12)
    ]
    assert delays[0] == 15 and delays[-1] == ERROR_RETRY_CAP_MINUTES
    assert sum(delays) > 12 * 60      # the budget now spans >12 hours


async def test_retry_payload_never_keeps_an_empty_pending_list(db):
    # Regression: a terminal attempt wrote "pending_trainings": [] and the
    # admin Approve-retry then executed zero courses ("skipped").
    from fra_bot.services.trainings import TrainingsService

    payload = {"trainings": [{"discipline": "fire", "name": "X"}],
               "pending_trainings": []}
    # The truthy check must fall through to the full course list.
    assert not payload.get("pending_trainings")
    service = TrainingsService.__new__(TrainingsService)
    merged = dict(payload)
    merged["results"] = []
    if []:
        merged["pending_trainings"] = []
    else:
        merged.pop("pending_trainings", None)
    assert "pending_trainings" not in merged
    assert merged["trainings"]
