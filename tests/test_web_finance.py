"""The web console's finance page: funds, income top lists and the
expense ledger render from the treasury tables only — read-only, no
MissionChief calls. All offline via aiohttp's test client."""

import datetime as dt

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo, ny_period_keys
from fra_bot.services.treasury_sync import STATE_BACKFILL_DONE
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
    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg

    async def log_member_action(self, **kwargs) -> None:
        pass

    def get_cog(self, name):
        return None


async def _make_client(db_path, cfg):
    db = Database(db_path)
    await db.connect()
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    return test_client, db


async def _seed(db) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    recent = (now - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    old = (now - dt.timedelta(days=60)).isoformat(timespec="seconds")

    await db.execute(
        "INSERT INTO treasury_balance (total_funds, scraped_at) "
        "VALUES (12345678, ?)", (recent,),
    )
    day_key, month_key = ny_period_keys()
    for period, key, rank, name, amount in (
        ("daily", day_key, 1, "Alice", 250000),
        ("daily", day_key, 2, "Bob", 100000),
        ("monthly", month_key, 1, "Alice", 1234567),
    ):
        await db.execute(
            "INSERT INTO income_snapshots (period, period_key, taken_at, "
            "rank, username, mc_user_id, amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (period, key, now.isoformat(), rank, name, None, amount),
        )
    # Oldest first so ascending id == chronological order, like the sync.
    for raw, event_at, name, amount, description in (
        ("05-18 12:00", old, "Alice", 5000, "Old purchase"),
        ("05-18 12:05", old, "Eve", 1, "<script>alert(1)</script> gift"),
        ("today 10:00", recent, "Alice", 111000,
         "Extension at Fire Station Alpha"),
        ("today 10:05", recent, "Bob", 222000, "Custom mission payout"),
    ):
        await db.execute(
            "INSERT INTO expenses (signature, raw_date, event_at, username, "
            "amount, description, scraped_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"sig-{name}-{amount}", raw, event_at, name, amount,
             description, recent),
        )


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    test_client, db = await _make_client(tmp_path / "web.sqlite3", cfg)
    await _seed(db)
    yield test_client
    await test_client.close()
    await db.close()


async def test_finance_tiles_and_notes(client):
    response = await client.get("/finance")
    text = await response.text()
    assert response.status == 200
    # Latest balance plus its timestamp note.
    assert "12,345,678" in text and "Funds as of" in text
    # Spend totals via expense_summary (today = this month = 333,000 here;
    # the 60-day-old rows stay out of both windows).
    assert "333,000" in text
    assert "Expenses recorded" in text and ">4<" in text
    # Ledger still mid-backfill (state flag unset) — the page says so.
    assert "Expense backfill in progress" in text
    # Nav entry joined the shared layout.
    assert ">Finance</a>" in text


async def test_backfill_note_clears_when_done(client):
    await StateRepo(client.bot.db).set(STATE_BACKFILL_DONE, "1")
    text = await (await client.get("/finance")).text()
    assert "Expense backfill in progress" not in text


async def test_income_panels_reuse_report_builders(client):
    text = await (await client.get("/finance")).text()
    day_key, month_key = ny_period_keys()
    assert f"Daily top contributors ({day_key})" in text
    assert f"Monthly top contributors ({month_key})" in text
    # Report markdown becomes safe HTML.
    assert "<strong>Alice</strong>" in text
    assert "250,000 credits" in text and "1,234,567 credits" in text


async def test_expense_table_filter_and_counts(client):
    text = await (await client.get("/finance")).text()
    assert "Extension at Fire Station Alpha" in text
    assert "Custom mission payout" in text
    assert "4 shown" in text and "4 recorded in total" in text

    # Username filter narrows the list and the matching count.
    text = await (await client.get("/finance?q=Bob")).text()
    assert "Custom mission payout" in text
    assert "Extension at Fire Station Alpha" not in text
    assert "1 shown" in text and "1 matching" in text
    assert "4 recorded in total" in text

    # The filter also matches descriptions.
    text = await (await client.get("/finance?q=mission")).text()
    assert "Custom mission payout" in text and "Old purchase" not in text


async def test_expense_description_is_escaped(client):
    text = await (await client.get("/finance?q=gift")).text()
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; gift" in text


async def test_empty_database_renders_cleanly(tmp_path, cfg):
    test_client, db = await _make_client(tmp_path / "empty.sqlite3", cfg)
    try:
        response = await test_client.get("/finance")
        text = await response.text()
        assert response.status == 200
        assert "No balance recorded yet" in text
        assert "No expenses recorded" in text
        assert "No spend recorded this month" in text
    finally:
        await test_client.close()
        await db.close()
