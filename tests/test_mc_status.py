"""MissionChief outage watcher: what counts as "the game is down", the
patience before announcing, and one message per transition."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.mc.health import MissionChiefHealth
from fra_bot.services.mc_status import (
    STATE_DOWN_SINCE,
    MissionChiefStatusService,
    _human_duration,
)

# asyncio_mode = auto (pytest.ini) runs the async tests without a mark —
# and this file mixes in plain sync tests for the health maths.


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "mcstatus.sqlite3")
    await database.connect()
    yield database
    await database.close()


class FakeChannel:
    def __init__(self):
        self.embeds = []

    async def send(self, *, embed=None, allowed_mentions=None):
        self.embeds.append(embed)


class FakeBot(SimpleNamespace):
    def channel_for(self, key):
        return self.__dict__.get("channels", {}).get(key)


def _cfg(*, enabled=True, outage_minutes=15, channel=900):
    return SimpleNamespace(
        discord=SimpleNamespace(channels=SimpleNamespace(mc_status=channel)),
        automation=SimpleNamespace(
            mc_status=SimpleNamespace(
                enabled=enabled, outage_minutes=outage_minutes,
            ),
        ),
    )


def _service(db, clock, **cfg_kwargs):
    channel = FakeChannel()
    bot = FakeBot(channels={"mc_status": channel})
    client = SimpleNamespace(health=MissionChiefHealth(clock=clock))
    svc = MissionChiefStatusService(_cfg(**cfg_kwargs), client, db, bot)
    return svc, client.health, channel


# -- what counts as an outage ------------------------------------------------

def test_health_only_counts_site_is_down_signals():
    clock = _Clock()
    health = MissionChiefHealth(clock=clock)
    # A quiet bot makes no claims: nothing has failed, so no outage.
    clock.advance(3600)
    assert health.outage_seconds() == 0.0
    assert health.down_since() is None

    health.note_reachable()
    clock.advance(600)
    health.note_unreachable("HTTP 502")
    clock.advance(300)
    # Measured from the last proof of reachability, not from the failure.
    assert health.outage_seconds() == pytest.approx(900)
    assert health.last_reason == "HTTP 502"

    # Any answer from the server clears it — a 4xx/429/sign-in redirect
    # all route here, which is what keeps "we were refused" or "we were
    # throttled" from being announced as "the game is down".
    health.note_reachable()
    assert health.outage_seconds() == 0.0


def test_health_measures_from_boot_when_it_never_reached_the_site():
    clock = _Clock()
    health = MissionChiefHealth(clock=clock)      # started_at = now
    clock.advance(1200)
    health.note_unreachable("ClientConnectorError")
    assert health.outage_seconds() == pytest.approx(1200)


def test_human_duration_reads_naturally():
    assert _human_duration(30) == "1 minute"
    assert _human_duration(120) == "2 minutes"
    assert _human_duration(3600) == "1 hour"
    assert _human_duration(5400) == "1 hour 30 min"
    assert _human_duration(90000) == "1 day 1 h"


# -- announcements -----------------------------------------------------------

async def test_outage_is_announced_once_after_the_patience_window(db):
    clock = _Clock()
    svc, health, channel = _service(db, clock)
    health.note_reachable()
    clock.advance(300)
    health.note_unreachable("HTTP 503")

    # 5 minutes in: below the 15-minute threshold, stay quiet.
    assert await svc.check() is None
    assert channel.embeds == []

    clock.advance(900)
    line = await svc.check()
    assert line is not None and "unreachable" in line
    assert len(channel.embeds) == 1
    assert "down" in channel.embeds[0].title
    # Still down on the next pass: no second notice.
    clock.advance(600)
    assert await svc.check() is None
    assert len(channel.embeds) == 1


async def test_recovery_is_announced_with_the_duration(db):
    clock = _Clock()
    svc, health, channel = _service(db, clock)
    health.note_reachable()
    clock.advance(1200)
    health.note_unreachable("TimeoutError")
    await svc.check()                                  # outage announced
    assert len(channel.embeds) == 1

    clock.advance(1800)
    health.note_reachable()                            # the game is back
    line = await svc.check()
    assert line is not None and "reachable again" in line
    assert len(channel.embeds) == 2
    recovery = channel.embeds[1]
    assert "back online" in recovery.title
    assert "50 minutes" in recovery.description        # 20 down + 30 more
    # The open outage is cleared, so a later blip starts fresh.
    assert await svc.state.get(STATE_DOWN_SINCE) is None
    assert await svc.check() is None


async def test_restart_mid_outage_neither_repeats_nor_loses_the_start(db):
    clock = _Clock()
    svc, health, channel = _service(db, clock)
    health.note_reachable()
    clock.advance(1200)
    health.note_unreachable("HTTP 502")
    await svc.check()
    assert len(channel.embeds) == 1

    # A restart: fresh service and fresh health (no memory of the outage),
    # same database.
    clock.advance(600)
    svc2, health2, channel2 = _service(db, clock)
    health2.note_unreachable("HTTP 502")
    assert await svc2.check() is None                  # already announced
    assert channel2.embeds == []

    clock.advance(600)
    health2.note_reachable()
    await svc2.check()
    assert len(channel2.embeds) == 1
    # Duration counts from the ORIGINAL outage start, not from the restart.
    assert "40 minutes" in channel2.embeds[0].description


async def test_watcher_can_be_switched_off_and_channel_zero_is_silent(db):
    clock = _Clock()
    svc, health, channel = _service(db, clock, enabled=False)
    health.note_reachable()
    clock.advance(3600)
    health.note_unreachable("HTTP 500")
    assert await svc.check() is None
    assert channel.embeds == []

    # Enabled but no channel: the state still tracks, nothing is posted.
    clock2 = _Clock()
    svc2, health2, _ = _service(db, clock2, channel=0)
    health2.note_reachable()
    clock2.advance(3600)
    health2.note_unreachable("HTTP 500")
    assert await svc2.check() is not None
    assert svc2.channel() is None


async def test_status_lines_report_the_current_belief(db):
    clock = _Clock()
    svc, health, _ = _service(db, clock)
    health.note_reachable()
    assert any("✅ reachable" in line for line in await svc.status_lines())

    clock.advance(1800)
    health.note_unreachable("HTTP 502")
    lines = await svc.status_lines()
    assert any("unreachable for 30 minutes" in line for line in lines)
    assert any("HTTP 502" in line for line in lines)
    assert any("<#900>" in line for line in lines)


# -- the client feeds it -----------------------------------------------------

async def test_client_marks_5xx_down_but_4xx_reachable(monkeypatch):
    """The taxonomy that keeps a permissions problem from being announced
    as an outage: only a failing SERVER counts."""
    from fra_bot.mc.client import MissionChiefClient
    from fra_bot.mc.errors import FetchError

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.url = "https://www.missionchief.com/x"
            self.headers = {}

        async def text(self):
            return "<html></html>"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        closed = False

        def __init__(self, status):
            self._status = status

        def get(self, *a, **k):
            return _Resp(self._status)

    class _Pacer:
        async def wait_turn(self):
            pass

        def record_failure(self):
            pass

        def record_success(self):
            pass

    cfg = SimpleNamespace(
        base_url="https://www.missionchief.com", alliance_id=1,
        cookie_path=None, email="x", password="y",
    )

    client = MissionChiefClient(cfg, _Pacer())
    client._session = _Session(403)
    with pytest.raises(FetchError):
        await client.fetch_page("/x")
    assert client.health.unreachable_count == 0        # answered = up

    client = MissionChiefClient(cfg, _Pacer())
    client._session = _Session(503)
    with pytest.raises(FetchError):
        await client.fetch_page("/x")
    assert client.health.unreachable_count == 3        # 3 attempts, all 5xx
    assert client.health.last_reason == "HTTP 503"
