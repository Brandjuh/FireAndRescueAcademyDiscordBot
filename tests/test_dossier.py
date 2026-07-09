"""The member dossier: cross-identity search and data aggregation."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.services.dossier import DossierService

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "dos.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _seed(db):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, role, earned_credits, "
        "contribution_rate, raw_member_since, is_active, first_seen_at, last_seen_at) "
        "VALUES (42, 'DutchFireFighter', 'Admin', 123456789, 12.5, "
        "'Jan 2024', 1, '2026-01-01', '2026-07-09')"
    )
    await db.execute(
        "INSERT INTO members (mc_user_id, name, is_active, first_seen_at, "
        "last_seen_at, left_at) VALUES (43, 'GoneMember', 0, '2026-01-01', "
        "'2026-06-01', '2026-06-01')"
    )
    # Treasury contributions (income snapshots).
    await db.execute(
        "INSERT INTO income_snapshots (period, period_key, taken_at, rank, "
        "username, mc_user_id, amount) VALUES "
        "('daily', '2026-07-09', '2026-07-09', 1, 'DutchFireFighter', 42, 110828), "
        "('monthly', '2026-07', '2026-07-09', 1, 'DutchFireFighter', 42, 2500000)"
    )
    # Requests: one training, one building; one large mission.
    await db.execute(
        "INSERT INTO automation_requests (kind, thread_id, post_id, "
        "requester_name, requester_mc_id, status, created_at, updated_at) VALUES "
        "('training', 5935, 1, 'DutchFireFighter', 42, 'done', '2026-07-01', '2026-07-01'), "
        "('building', 6165, 2, 'DutchFireFighter', 42, 'failed', '2026-07-08', '2026-07-08')"
    )
    await db.execute(
        "INSERT INTO scheduled_missions (source, kind, requester_name, "
        "requester_mc_id, status, created_at, updated_at) VALUES "
        "('board', 'large', 'DutchFireFighter', 42, 'done', '2026-07-05', '2026-07-05')"
    )


async def test_search_finds_by_id_name_and_substring(db):
    await _seed(db)
    svc = DossierService(db)
    assert (await svc.search("42"))[0].mc_user_id == 42
    assert (await svc.search("dutchfirefighter"))[0].mc_user_id == 42
    hits = await svc.search("fire")
    assert any(c.mc_user_id == 42 for c in hits)
    # Former members are searchable too (history stays reachable).
    assert (await svc.search("GoneMember"))[0].is_active is False
    assert await svc.search("nobody-here") == []


async def test_search_includes_verified_discord_identity(db):
    await _seed(db)
    svc = DossierService(db)
    await svc.links.upsert(1000, 42, status="approved")
    hit = (await svc.search("DutchFireFighter"))[0]
    assert hit.discord_id == 1000
    assert await svc.resolve_discord(1000) == 42
    assert await svc.resolve_discord(9999) is None


async def test_dossier_aggregates_everything(db):
    await _seed(db)
    svc = DossierService(db)
    await svc.links.upsert(1000, 42, status="approved")
    d = await svc.build(42)
    assert d.name == "DutchFireFighter" and d.is_active
    assert d.earned_credits == 123456789
    assert d.contribution_rate == 12.5
    assert d.contributed_daily == 110828
    assert d.contributed_monthly == 2500000
    assert d.discord_id == 1000 and d.link_status == "approved"
    assert d.requests["training"]["count"] == 1
    assert d.requests["training"]["last_status"] == "done"
    assert d.requests["building"]["count"] == 1
    assert d.requests["building"]["last_status"] == "failed"
    assert d.missions["large"]["count"] == 1
    assert await svc.build(999999) is None


async def test_dossier_embed_renders(db):
    """The embed builder must handle a full and a former-member dossier."""
    from fra_bot.cogs.dossier import dossier_embed

    await _seed(db)
    svc = DossierService(db)
    embed = dossier_embed(await svc.build(42))
    assert "DutchFireFighter" in embed.title
    text = " ".join(f.value for f in embed.fields)
    assert "123,456,789" in text and "110,828" in text
    gone = dossier_embed(await svc.build(43))
    assert "Left the alliance" in gone.description
