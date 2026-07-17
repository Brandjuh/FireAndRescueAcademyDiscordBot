"""Web console alliance-log feed: newest-first pagination, action-key
filter, free-text search, member links and per-action counts, all
offline via aiohttp's test client against a real migrated database."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.web.server import build_app

pytestmark = pytest.mark.asyncio

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""

SCRAPED_AT = "2026-07-12T00:00:00+00:00"


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
        self.actions = []

    async def log_member_action(self, **kwargs) -> None:
        self.actions.append(kwargs)

    def get_cog(self, name):
        return None


async def insert_log(db, *, signature, action_key, description,
                     raw_timestamp="07/10/2026 10:00", event_at=None,
                     executed_name=None, executed_mc_id=None,
                     affected_name=None, affected_type=None,
                     affected_mc_id=None, contribution_amount=None):
    await db.execute(
        "INSERT INTO alliance_logs (signature, raw_timestamp, event_at, "
        "action_key, description, executed_name, executed_mc_id, "
        "affected_name, affected_type, affected_mc_id, "
        "contribution_amount, scraped_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (signature, raw_timestamp, event_at, action_key, description,
         executed_name, executed_mc_id, affected_name, affected_type,
         affected_mc_id, contribution_amount, SCRAPED_AT),
    )


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    db = Database(tmp_path / "web_logs.sqlite3")
    await db.connect()
    await insert_log(
        db, signature="sig-1", action_key="added_to_alliance",
        description="Added to the alliance",
        event_at="2026-07-10T10:00:00+00:00",
        executed_name="Alice", executed_mc_id=101,
    )
    await insert_log(
        db, signature="sig-2", action_key="contributed_to_alliance",
        description="Contributed to the alliance",
        event_at="2026-07-11T09:00:00+00:00",
        executed_name="Bob", executed_mc_id=202, contribution_amount=5000,
    )
    await insert_log(
        db, signature="sig-3", action_key="kicked_from_alliance",
        description="Kicked from the alliance",
        event_at="2026-07-11T12:00:00+00:00",
        executed_name="Alice", executed_mc_id=101,
        affected_name="Charlie", affected_type="user", affected_mc_id=303,
    )
    await insert_log(
        db, signature="sig-4", action_key="building_constructed",
        description="Constructed a building (Fire station)",
        event_at="2026-07-12T08:00:00+00:00",
        executed_name="Bob", executed_mc_id=202,
        affected_name="Fire Station 7", affected_type="building",
        affected_mc_id=999,
    )
    # A key the display map does not know, with an unparseable timestamp:
    # must render with the fallback title and the raw timestamp, not crash.
    await insert_log(
        db, signature="sig-5", action_key="totally_new_thing",
        description="Mystery event", raw_timestamp="07/12/2026 09:00",
    )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def test_feed_renders_newest_first_with_member_links(client):
    response = await client.get("/logs")
    text = await response.text()
    assert response.status == 200
    assert ">Logs<" in text  # joined the top nav
    assert "Added to the alliance" in text
    assert "/members/101" in text  # executed_mc_id links to the member page
    assert "/members/303" in text  # affected user links too
    # Newest event (building, Jul 12) renders above the kick (Jul 11) —
    # compare the row timestamps; titles also appear in dropdown/chips.
    assert text.index("2026-07-12 08:00") < text.index("2026-07-11 12:00")
    # Unknown key: fallback title + raw timestamp, no crash.
    assert "Mystery event" in text and "07/12/2026 09:00" in text
    assert "Alliance log" in text


async def test_affected_non_users_are_not_linked(client):
    text = await (await client.get("/logs")).text()
    assert "Fire Station 7" in text
    # A building id must never be presented as a member link.
    assert "/members/999" not in text


async def test_filter_by_action_key(client):
    text = await (await client.get(
        "/logs?action=contributed_to_alliance")).text()
    assert "Bob" in text and "+5,000" in text
    assert "Charlie" not in text and "Fire Station 7" not in text
    # The filtered window's summary shows only the one key.
    assert "Contributed to the alliance × 1" in text
    assert "Kicked from the alliance ×" not in text
    # The dropdown keeps the selection.
    assert "<option value='contributed_to_alliance' selected>" in text


async def test_free_text_search_over_names_and_description(client):
    text = await (await client.get("/logs?q=Charlie")).text()
    assert "Kicked from the alliance" in text and "Charlie" in text
    assert "Bob" not in text
    text = await (await client.get("/logs?q=Mystery")).text()
    assert "Mystery event" in text and "Alice" not in text


async def test_summary_counts_for_the_filtered_window(client):
    text = await (await client.get("/logs")).text()
    for label in ("Added to the alliance × 1", "Kicked from the alliance × 1",
                  "Building constructed × 1"):
        assert label in text
    # Chips link back into the action filter.
    assert "href='/logs?action=building_constructed'" in text


async def test_dropdown_lists_known_keys(client):
    text = await (await client.get("/logs")).text()
    assert "All types" in text
    assert "<option value='building_constructed'" in text
    assert "<option value='unknown'" in text


async def test_pagination_100_per_page(client):
    db = client.bot.db
    for i in range(120):
        await insert_log(
            db, signature=f"bulk-{i}", action_key="course_completed",
            description=f"Course completed (Bulk drill {i})",
            event_at=f"2026-06-01T10:{i // 60:02d}:{i % 60:02d}+00:00",
            executed_name="Alice", executed_mc_id=101,
        )
    # 125 rows total: the 5 July fixtures plus the 95 newest June rows.
    text = await (await client.get("/logs")).text()
    assert text.count("Bulk drill") == 95
    assert "Page 1 of 2" in text and "125 entries" in text
    assert "page=2" in text and "← Newer" not in text

    text = await (await client.get("/logs?page=2")).text()
    assert text.count("Bulk drill") == 25
    assert "Page 2 of 2" in text and "← Newer" in text and "Older →" not in text

    # Out-of-range and garbage page values clamp instead of erroring.
    response = await client.get("/logs?page=999")
    assert response.status == 200 and "Page 2 of 2" in await response.text()
    response = await client.get("/logs?page=abc")
    assert response.status == 200 and "Page 1 of 2" in await response.text()


async def test_empty_filter_window_renders_cleanly(client):
    text = await (await client.get("/logs?q=nomatchxyz")).text()
    assert "No log entries match" in text
    assert "0 entries" in text and "Page 1 of 1" in text
