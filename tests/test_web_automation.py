"""Web console automation page: operational status renders from config,
state and repos; the academy build action enqueues through the SAME
service path as the Discord panel (no MissionChief calls from the
handler — offline, the committed row waits for the queue poller)."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.db.repos import MemberActionsRepo
from fra_bot.services.academy import AcademyService
from fra_bot.web.server import build_app

pytestmark = pytest.mark.asyncio

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from fra_bot.config import load_config

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("MC_EMAIL", "x@example.com")
    monkeypatch.setenv("MC_PASSWORD", "x")
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return load_config(path)


class FakeBot:
    """Offline bot: db/cfg/action log plus the live gauges the page reads
    (bot.pacer / bot.mc as plain namespaces) and a REAL AcademyService so
    the web enqueue exercises the exact Discord-panel path. Deliberately
    no ``job_lock``: the immediate-execution kick must then be skipped,
    leaving the committed row for the scheduled poller."""

    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self.actions = []
        self.pacer = SimpleNamespace(circuit_open=False)
        self.mc = SimpleNamespace(pacer_backlog=0, pacer_backlog_bulk=0)
        self.academy = AcademyService(cfg, db, buildings=None)

    async def log_member_action(self, **kwargs) -> None:
        self.actions.append(kwargs)
        await MemberActionsRepo(self.db).log(
            action=kwargs.get("action", "?"), detail=kwargs.get("detail"),
            discord_user_id=kwargs.get("discord_user_id"),
            mc_user_id=kwargs.get("mc_user_id"),
            actor_name=kwargs.get("actor_name"),
        )

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    db = Database(tmp_path / "web.sqlite3")
    await db.connect()
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def _academy_rows(db):
    async with db.conn.execute(
        "SELECT * FROM automation_requests WHERE kind = 'academy' "
        "ORDER BY id"
    ) as cur:
        return list(await cur.fetchall())


async def test_status_page_dry_run_banner_and_switches(client):
    response = await client.get("/automation")
    text = await response.text()
    assert response.status == 200
    # Default config is dry_run=True: the calm banner, never the red one.
    assert "DRY-RUN is ON" in text and "LIVE MODE" not in text
    for label in ("Trainings", "Buildings", "Events", "Missions",
                  "Academy queue", "Chat bridge", "Application auto-accept",
                  "Board replies"):
        assert label in text
    assert "Circuit breaker" in text and "closed" in text
    assert "No academy builds yet." in text


async def test_live_banner_when_dry_run_off(client):
    # Flip the frozen dataclass the same way core.settings.apply does.
    object.__setattr__(client.bot.cfg.automation, "dry_run", False)
    text = await (await client.get("/automation")).text()
    assert "LIVE MODE" in text and "dry-run is OFF" in text
    assert "DRY-RUN is ON" not in text


async def test_circuit_open_and_backlog_render(client):
    client.bot.pacer.circuit_open = True
    client.bot.mc.pacer_backlog = 7
    client.bot.mc.pacer_backlog_bulk = 2
    text = await (await client.get("/automation")).text()
    assert "OPEN — MC traffic paused" in text
    assert "7 MC request(s) waiting" in text
    assert "5 interactive · 2 bulk" in text


async def test_board_reply_failures_listed(client):
    await client.bot.db.execute(
        "INSERT INTO scraper_state (key, value) VALUES (?, ?)",
        (
            "board_reply_last_failure:training",
            json.dumps({"at": "2026-07-16T10:00:00+00:00",
                        "detail": "post not confirmed"}),
        ),
    )
    text = await (await client.get("/automation")).text()
    assert "post not confirmed" in text and "2026-07-16T10:00" in text
    # A healthy board setup shows no blanket warning.
    assert "reply_to_board OFF" not in text


async def test_academy_queue_and_funds_gate(client):
    await client.bot.db.execute(
        "INSERT INTO automation_requests (kind, thread_id, post_id, "
        "requester_name, payload, status, status_detail, attempts, "
        "created_at, updated_at) VALUES ('academy', 0, 0, 'Alice', ?, "
        "'waiting', 'alliance funds 1,000 below floor 2,000,000; queued', "
        "2, '2026-07-15T09:00:00', '2026-07-15T09:05:00')",
        (json.dumps({"academy": "fire"}),),
    )
    await client.bot.db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) "
        "VALUES (1000, '2026-07-16T00:00:00')"
    )
    text = await (await client.get("/automation")).text()
    assert "Fire academy" in text and "waiting" in text
    assert "below floor" in text  # the funds gate badge
    # Funds recover -> the gate flips to OK on the next render.
    await client.bot.db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) "
        "VALUES (5000000, '2026-07-17T00:00:00')"
    )
    text = await (await client.get("/automation")).text()
    assert "funds OK" in text and "5,000,000" in text


async def test_job_runs_render(client):
    await client.bot.db.execute(
        "INSERT INTO scrape_runs (scraper, started_at, finished_at, status, "
        "rows_new, message) VALUES ('board_training', '2026-07-16T08:00:00', "
        "'2026-07-16T08:01:00', 'failed', 0, 'thread unreachable')"
    )
    text = await (await client.get("/automation")).text()
    assert "board_training" in text and "thread unreachable" in text


async def test_academy_build_enqueues_like_the_panel(client):
    response = await client.post(
        "/automation/academy", data={"academy": "police"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    rows = await _academy_rows(client.bot.db)
    assert len(rows) == 1
    row = rows[0]
    # The exact shape AcademyService.enqueue writes for the Discord panel.
    assert row["kind"] == "academy" and row["thread_id"] == 0
    assert row["requester_name"] == "Web console"
    assert json.loads(row["payload"])["academy"] == "police"
    # No job_lock on the fake bot -> no immediate kick: the row stays
    # pending for the scheduled queue poller instead of half-executing.
    assert row["status"] == "pending"
    assert any(
        a["action"] == "academy_build_clicked"
        and "police" in (a["detail"] or "")
        for a in client.bot.actions
    )


async def test_academy_build_rejects_unknown_kind(client):
    response = await client.post(
        "/automation/academy", data={"academy": "space"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert await _academy_rows(client.bot.db) == []
    assert client.bot.actions == []


async def test_academy_build_without_service_flashes_error(client):
    client.bot.academy = None
    response = await client.post(
        "/automation/academy", data={"academy": "fire"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert await _academy_rows(client.bot.db) == []
