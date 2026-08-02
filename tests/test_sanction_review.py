"""Game-log sanction review: checkpoint bootstrap, kick/chat-ban import,
the own-tax-kick exception, the manual-duplicate guard, and the
Confirm/Dismiss resolution."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database, utcnow_iso
from fra_bot.db.repos import MemberActionsRepo, SanctionsRepo
from fra_bot.services.sanction_review import (
    CHECKPOINT_KEY,
    SanctionReviewService,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "review.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _cfg(enabled=True):
    return SimpleNamespace(
        automation=SimpleNamespace(
            sanctions=SimpleNamespace(game_log_review_enabled=enabled),
        ),
    )


async def _log_row(db, *, action_key, affected_name="Slacker",
                   affected_mc_id=42, executed_name="AdminGuy",
                   executed_mc_id=7, event_at=None, description="kicked"):
    return await db.execute_returning_id(
        "INSERT INTO alliance_logs (signature, raw_timestamp, event_at, "
        "action_key, description, executed_name, executed_mc_id, "
        "affected_name, affected_type, affected_mc_id, scraped_at) "
        "VALUES (?, 't', ?, ?, ?, ?, ?, ?, 'user', ?, ?)",
        (f"sig-{action_key}-{affected_mc_id}-{utcnow_iso()}",
         event_at or utcnow_iso(), action_key, description,
         executed_name, executed_mc_id, affected_name, affected_mc_id,
         utcnow_iso()),
    )


async def test_first_scan_bootstraps_without_replaying_history(db):
    await _log_row(db, action_key="kicked_from_alliance")
    svc = SanctionReviewService(_cfg(), db)
    result = await svc.scan()
    assert result["bootstrapped"] is True
    assert result["created"] == []
    # The old kick is behind the checkpoint now — never imported.
    result = await svc.scan()
    assert result["created"] == []


async def test_manual_kick_imports_as_unverified_sanction(db):
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()                                   # bootstrap
    await _log_row(db, action_key="kicked_from_alliance")

    result = await svc.scan()
    assert len(result["created"]) == 1
    item = result["created"][0]
    assert item["sanction_type"] == "Kick"
    assert item["name"] == "Slacker"
    row = await SanctionsRepo(db).get(item["sanction_id"])
    assert row["status"] == "unverified"
    assert "AdminGuy" in row["admin_name"]
    # Checkpoint advanced: a second pass imports nothing new.
    assert (await svc.scan())["created"] == []


async def test_chat_ban_imports_as_mute(db):
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    await _log_row(db, action_key="chat_ban_set", description="chat ban")
    result = await svc.scan()
    assert result["created"][0]["sanction_type"] == "Mute"


async def test_own_tax_kick_is_skipped(db):
    # The whole point of the port: the bot's own documented auto-kick must
    # NOT trigger a review.
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    await MemberActionsRepo(db).log(
        discord_user_id=None, mc_user_id=42, actor_name="Slacker",
        action="tax_kicked", detail="auto-kicked after 3 warnings",
    )
    await _log_row(db, action_key="kicked_from_alliance",
                   executed_name="FRA Bot")
    result = await svc.scan()
    assert result["created"] == []
    assert result["skipped_own"] == 1


async def test_manually_recorded_kick_is_not_double_flagged(db):
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    await SanctionsRepo(db).add(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="AdminGuy",
        sanction_type="Kick", reason="rule breach",
    )
    await _log_row(db, action_key="kicked_from_alliance")
    result = await svc.scan()
    assert result["created"] == []
    assert result["skipped_recorded"] == 1


async def test_disabled_switch_is_a_noop(db):
    await _log_row(db, action_key="kicked_from_alliance")
    svc = SanctionReviewService(_cfg(enabled=False), db)
    result = await svc.scan()
    assert result["created"] == [] and result["bootstrapped"] is False
    # Not even a checkpoint: enabling later bootstraps cleanly.
    assert await svc.state.get(CHECKPOINT_KEY) is None


async def test_resolve_review_confirm_and_dismiss(db):
    repo = SanctionsRepo(db)
    sid = await repo.add(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=0, admin_name="MissionChief log: AdminGuy",
        sanction_type="Kick", reason="Kicked from the alliance",
        status="unverified",
    )
    assert await repo.resolve_review(sid, confirm=True, by="Boss") is True
    assert (await repo.get(sid))["status"] == "active"
    # Already settled: a second click changes nothing.
    assert await repo.resolve_review(sid, confirm=False, by="Boss") is False

    sid2 = await repo.add(
        mc_user_id=43, mc_username="Other", discord_user_id=None,
        admin_discord_id=0, admin_name="MissionChief log: AdminGuy",
        sanction_type="Mute", reason="Chat ban set", status="unverified",
    )
    assert await repo.resolve_review(sid2, confirm=False, by="Boss") is True
    row = await repo.get(sid2)
    assert row["status"] == "dismissed" and row["revoked_by"] == "Boss"


async def test_old_tax_kick_does_not_shield_a_new_manual_kick(db):
    # A tax_kicked action from long ago (member rejoined, later kicked
    # manually) must not suppress the new review.
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    old = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    ).isoformat(timespec="seconds")
    action_id = await MemberActionsRepo(db).log(
        discord_user_id=None, mc_user_id=42, actor_name="Slacker",
        action="tax_kicked", detail="old kick",
    )
    await db.execute(
        "UPDATE member_actions SET created_at = ? WHERE id = ?",
        (old, action_id),
    )
    await _log_row(db, action_key="kicked_from_alliance")
    result = await svc.scan()
    assert len(result["created"]) == 1
    assert result["skipped_own"] == 0


async def test_own_mute_is_skipped(db):
    # A chat ban the bot set itself (a real Mute sanction, type "Mute 1d")
    # must not trigger a review when its log row appears — the exact-type
    # duplicate guard can't catch it (log imports are bare "Mute").
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    await SanctionsRepo(db).add(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=0, admin_name="FRA Bot (escalation)",
        sanction_type="Mute 1d", reason="CoC 5.2", source="escalation",
    )
    await _log_row(db, action_key="chat_ban_set", executed_name="FRA Bot")
    result = await svc.scan()
    assert result["created"] == []
    assert result["skipped_own"] == 1


async def test_old_own_mute_does_not_shield_a_new_chat_ban(db):
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    repo = SanctionsRepo(db)
    sid = await repo.add(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=0, admin_name="FRA Bot (escalation)",
        sanction_type="Mute 1d", reason="CoC 5.2", source="escalation",
    )
    old = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    ).isoformat(timespec="seconds")
    await db.execute(
        "UPDATE sanctions SET created_at = ? WHERE id = ?", (old, sid),
    )
    await _log_row(db, action_key="chat_ban_set", executed_name="AdminGuy")
    result = await svc.scan()
    assert len(result["created"]) == 1
    assert result["skipped_own"] == 0


async def test_own_tax_kick_without_profile_link_is_skipped(db):
    # The kicked member's log row often shows their NAME without a profile
    # link (they're no longer in the alliance) — the skip must match on
    # name too, or exactly these reviews slip through.
    svc = SanctionReviewService(_cfg(), db)
    await svc.scan()
    await MemberActionsRepo(db).log(
        discord_user_id=None, mc_user_id=42, actor_name="Slacker",
        action="tax_kicked", detail="auto-kicked after 3 warnings",
    )
    await _log_row(db, action_key="kicked_from_alliance",
                   affected_mc_id=None, executed_name="FRA Bot")
    result = await svc.scan()
    assert result["created"] == []
    assert result["skipped_own"] == 1
