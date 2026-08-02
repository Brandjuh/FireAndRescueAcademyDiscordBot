"""The web console's sanctions register: list with filters and stats,
detail with history, and the add/edit/resolve/revoke actions — all
offline via aiohttp's test client (no sanction_service on the fake bot:
the repo fallback records without touching the game)."""

import datetime as dt

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database, utcnow_iso
from fra_bot.db.repos import SanctionsRepo
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
        self.logged: list[dict] = []

    async def log_member_action(self, **kwargs) -> None:
        self.logged.append(kwargs)

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    db = Database(tmp_path / "websanc.sqlite3")
    await db.connect()
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    test_client.db = db
    yield test_client
    await test_client.close()
    await db.close()


async def _seed_member(db, mc_id=101, name="Alice"):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, 10.0, 1, ?, ?)",
        (mc_id, name, utcnow_iso(), utcnow_iso()),
    )


async def _seed_sanction(db, **overrides) -> int:
    values = dict(
        mc_user_id=101, mc_username="Alice", discord_user_id=None,
        admin_discord_id=1, admin_name="Boss",
        sanction_type="Warning - Official 1st warning",
        reason="<script>alert(1)</script> flaming", status="active",
        source="manual",
    )
    values.update(overrides)
    return await SanctionsRepo(db).add(**values)


async def test_list_page_renders_filters_and_escapes(client):
    await _seed_sanction(client.db)
    await _seed_sanction(
        client.db, mc_username="Bob", mc_user_id=202,
        sanction_type="Kick", source="tax",
    )
    resp = await client.get("/sanctions")
    text = await resp.text()
    assert resp.status == 200
    assert "Alice" in text and "Bob" in text
    assert "<script>alert(1)</script>" not in text     # escaped
    assert "&lt;script&gt;" in text
    # Source filter narrows the table (dropdowns list every type, so
    # assert on the member names in the rows).
    resp = await client.get("/sanctions?source=tax")
    text = await resp.text()
    assert "Bob" in text and "Alice" not in text
    # Member filter matches name substring.
    resp = await client.get("/sanctions?member=ali")
    text = await resp.text()
    assert "Alice" in text and "Bob" not in text


async def test_detail_shows_history_and_advice(client):
    sid = await _seed_sanction(
        client.db, reason="1.6. Racism/Bullying", reason_category="1.6",
    )
    resp = await client.get(f"/sanctions/{sid}")
    text = await resp.text()
    assert resp.status == 200
    assert "created" in text                    # history trail
    assert "CoC advice for rule 1.6" in text
    assert (await client.get("/sanctions/99999")).status == 404


async def test_add_via_register_page_uses_the_catalogue(client):
    await _seed_member(client.db)
    resp = await client.post(
        "/sanctions/new",
        data={"member": "Alice", "rule": "1.4", "type": "Mute 1d",
              "notes": "cool down"},
        allow_redirects=False,
    )
    assert resp.status == 302
    rows = await SanctionsRepo(client.db).for_member(mc_user_id=101)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"].startswith("1.4.")
    assert row["reason_category"] == "1.4"
    assert row["source"] == "web"
    assert row["expires_at"] is not None        # timed mute got a real expiry
    assert client.bot.logged and (
        client.bot.logged[0]["action"] == "sanction_received"
    )


async def test_add_rejects_unknown_member_and_ambiguity(client):
    await _seed_member(client.db, 101, "Alice")
    await _seed_member(client.db, 102, "Alina")
    resp = await client.post(
        "/sanctions/new",
        data={"member": "Nobody", "rule": "1.4", "type": "Kick"},
        allow_redirects=False,
    )
    assert "No%20member%20found" in resp.headers["Location"]
    resp = await client.post(
        "/sanctions/new",
        data={"member": "Ali", "rule": "1.4", "type": "Kick"},
        allow_redirects=False,
    )
    assert "ambiguous" in resp.headers["Location"]
    assert await SanctionsRepo(client.db).for_member(mc_user_id=101) == []


async def test_edit_updates_fields_with_audit(client):
    sid = await _seed_sanction(client.db)
    resp = await client.post(
        f"/sanctions/{sid}/edit",
        data={"type": "Mute 6h", "rule": "2.1", "reason": "2.1. Foul language",
              "notes": "updated"},
        allow_redirects=False,
    )
    assert resp.status == 302
    repo = SanctionsRepo(client.db)
    row = await repo.get(sid)
    assert row["sanction_type"] == "Mute 6h"
    assert row["reason_category"] == "2.1"
    assert row["expires_at"] is not None
    assert row["edited_by"] == "Web console"
    assert any(h["action"] == "edited" for h in await repo.history(sid))


async def test_resolve_approve_and_dismiss(client):
    repo = SanctionsRepo(client.db)
    sid = await _seed_sanction(client.db, status="unverified", source="game_log")
    resp = await client.post(
        f"/sanctions/{sid}/resolve", data={"action": "approve"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert (await repo.get(sid))["status"] == "active"
    assert client.bot.logged[-1]["action"] == "sanction_received"
    sid2 = await _seed_sanction(client.db, status="unverified", source="game_log")
    await client.post(
        f"/sanctions/{sid2}/resolve", data={"action": "dismiss"},
        allow_redirects=False,
    )
    assert (await repo.get(sid2))["status"] == "dismissed"
    # Settled records can't be resolved again.
    resp = await client.post(
        f"/sanctions/{sid}/resolve", data={"action": "dismiss"},
        allow_redirects=False,
    )
    assert "nothing%20changed" in resp.headers["Location"]


async def test_revoke_from_register_and_member_page(client):
    repo = SanctionsRepo(client.db)
    sid = await _seed_sanction(client.db)
    resp = await client.post(
        f"/sanctions/{sid}/revoke", data={}, allow_redirects=False,
    )
    assert resp.status == 302
    assert (await repo.get(sid))["status"] == "revoked"
    # The member page's form passes mc_id and lands back there.
    sid2 = await _seed_sanction(client.db)
    resp = await client.post(
        f"/sanctions/{sid2}/revoke", data={"mc_id": "101"},
        allow_redirects=False,
    )
    assert resp.headers["Location"].startswith("/members/101")
    assert (await repo.get(sid2))["status"] == "revoked"


async def test_expired_mute_displays_derived_status(client):
    past = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    ).isoformat(timespec="seconds")
    sid = await _seed_sanction(
        client.db, sanction_type="Mute 1h", expires_at=past,
    )
    resp = await client.get(f"/sanctions/{sid}")
    text = await resp.text()
    assert ">expired<" in text
