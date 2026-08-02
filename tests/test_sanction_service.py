"""SanctionService: real mute execution behind the route switch, expiry
bookkeeping, chat-ban log verification, early unmute on revoke, and the
CoC-5 escalation engine (auto mode + button executors)."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database, utcnow_iso
from fra_bot.db.repos import SanctionsRepo
from fra_bot.services.sanctions import SanctionService

pytestmark = pytest.mark.asyncio


def _cfg(*, dry_run=False, mode="button", threshold=3, gap=24,
         notice=True, mute_type="Mute 1d", mute_exec=False):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            sanctions=SimpleNamespace(
                game_log_review_enabled=True,
                escalation_mode=mode,
                escalation_offense_threshold=threshold,
                escalation_gap_hours=gap,
                escalation_notice=notice,
                escalation_mute_type=mute_type,
                mute_execution_enabled=mute_exec,
                reapply_block_days=60,
            ),
        ),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "svc.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _svc(db, **kwargs) -> SanctionService:
    return SanctionService(_cfg(**kwargs), SimpleNamespace(), db)


async def _issue_warning(svc, *, mc=42, name="Slacker", days_ago=2.0):
    result = await svc.issue(
        mc_user_id=mc, mc_username=name, discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Warning - Official 1st warning", reason="testing",
    )
    if days_ago:
        backdated = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
        ).isoformat(timespec="seconds")
        await svc._db.execute(
            "UPDATE sanctions SET created_at = ? WHERE id = ?",
            (backdated, result["sanction_id"]),
        )
    return result


async def _add_roster_member(db, mc_id, name):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, 10.0, 1, ?, ?)",
        (mc_id, name, utcnow_iso(), utcnow_iso()),
    )


async def _log_row(db, *, action_key, mc_id=42, name="Slacker"):
    await db.execute(
        "INSERT INTO alliance_logs (signature, raw_timestamp, event_at, "
        "action_key, description, affected_name, affected_type, "
        "affected_mc_id, scraped_at) VALUES (?, 't', ?, ?, 'x', ?, 'user', ?, ?)",
        (f"sig-{action_key}-{utcnow_iso()}", utcnow_iso(), action_key,
         name, mc_id, utcnow_iso()),
    )


# -- mute execution ---------------------------------------------------------

async def test_mute_records_expiry_but_stays_offline_until_route_verified(
    db, monkeypatch,
):
    async def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("chat ban route called while execution is off")

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", boom)
    svc = _svc(db, mute_exec=False)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    assert result["expires_at"] is not None
    assert "NOT set" in result["mute_note"]
    row = await svc.repo.get(result["sanction_id"])
    assert row["expires_at"] == result["expires_at"]
    history = await svc.repo.history(result["sanction_id"])
    assert [h["action"] for h in history] == ["created", "mute_execution"]


async def test_mute_sets_real_chat_ban_and_arms_verification(db, monkeypatch):
    calls = []

    async def fake_set(client, mc_user_id, *, duration_minutes=None):
        calls.append((mc_user_id, duration_minutes))
        return True, "chat ban confirmed"

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", fake_set)
    svc = _svc(db, mute_exec=True)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    assert calls == [(42, 1440)]
    assert "chat ban set" in result["mute_note"]
    armed = await svc.state.get(
        f"sanction_mute_verify:{result['sanction_id']}"
    )
    assert armed is not None and armed.startswith("chat_ban_set|")


async def test_dry_run_never_touches_the_game(db, monkeypatch):
    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("game touched in dry-run")

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", boom)
    svc = _svc(db, mute_exec=True, dry_run=True)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    assert "dry-run" in result["mute_note"]


async def test_expiry_sweep_books_the_stored_transition(db):
    svc = _svc(db)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 5m", reason="spamming",
    )
    past = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    ).isoformat(timespec="seconds")
    await db.execute(
        "UPDATE sanctions SET expires_at = ? WHERE id = ?",
        (past, result["sanction_id"]),
    )
    lines = await svc.sweep()
    assert any("expired" in line for line in lines)
    row = await svc.repo.get(result["sanction_id"])
    assert row["status"] == "expired"
    # Booked once — the next sweep stays quiet.
    assert await svc.sweep() == []


async def test_verification_confirms_via_the_alliance_log(db, monkeypatch):
    async def fake_set(client, mc_user_id, *, duration_minutes=None):
        return True, "ok"

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", fake_set)
    svc = _svc(db, mute_exec=True)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    await _log_row(db, action_key="chat_ban_set")
    assert await svc.sweep() == []
    history = await svc.repo.history(result["sanction_id"])
    assert any(h["action"] == "verified" for h in history)
    assert await svc.state.get(
        f"sanction_mute_verify:{result['sanction_id']}"
    ) is None


async def test_verification_alerts_when_the_log_never_confirms(db, monkeypatch):
    async def fake_set(client, mc_user_id, *, duration_minutes=None):
        return True, "ok"

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", fake_set)
    svc = _svc(db, mute_exec=True)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    # Force the deadline into the past — the log row never appeared.
    past = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    ).isoformat(timespec="seconds")
    key = f"sanction_mute_verify:{result['sanction_id']}"
    await svc.state.set(key, f"chat_ban_set|{past}|{past}")
    lines = await svc.sweep()
    assert any("Moderator action rights" in line for line in lines)
    assert await svc.state.get(key) is None
    history = await svc.repo.history(result["sanction_id"])
    assert any(h["action"] == "verify_failed" for h in history)


async def test_revoke_lifts_a_running_mute_first(db, monkeypatch):
    async def fake_set(client, mc_user_id, *, duration_minutes=None):
        return True, "ok"

    removed = []

    async def fake_remove(client, mc_user_id):
        removed.append(mc_user_id)
        return True, "lifted"

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", fake_set)
    monkeypatch.setattr(
        "fra_bot.services.sanctions.remove_chat_ban", fake_remove
    )
    svc = _svc(db, mute_exec=True)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    ok, note = await svc.revoke(result["sanction_id"], revoked_by="Boss")
    assert ok and removed == [42] and "lifted" in note
    assert (await svc.repo.get(result["sanction_id"]))["status"] == "revoked"


async def test_revoke_keeps_the_sanction_when_unmute_fails(db, monkeypatch):
    async def fake_set(client, mc_user_id, *, duration_minutes=None):
        return True, "ok"

    async def fake_remove(client, mc_user_id):
        return False, "HTTP 500"

    monkeypatch.setattr("fra_bot.services.sanctions.set_chat_ban", fake_set)
    monkeypatch.setattr(
        "fra_bot.services.sanctions.remove_chat_ban", fake_remove
    )
    svc = _svc(db, mute_exec=True)
    result = await svc.issue(
        mc_user_id=42, mc_username="Slacker", discord_user_id=None,
        admin_discord_id=1, admin_name="Admin",
        sanction_type="Mute 1d", reason="spamming",
    )
    ok, note = await svc.revoke(result["sanction_id"], revoked_by="Boss")
    assert not ok and "stays active" in note
    assert (await svc.repo.get(result["sanction_id"]))["status"] == "active"


# -- escalation -------------------------------------------------------------

async def test_escalation_info_follows_the_offense_count(db):
    svc = _svc(db)
    first = await _issue_warning(svc)
    assert first["escalation"] is None and first["offense_count"] == 1
    second = await _issue_warning(svc)
    assert second["escalation"]["step"] == "second"
    third = await _issue_warning(svc)
    assert third["escalation"]["step"] == "final"
    assert third["offense_count"] == 3


async def test_tax_and_escalation_records_never_count(db):
    svc = _svc(db)
    for source in ("tax", "escalation"):
        await svc.issue(
            mc_user_id=42, mc_username="Slacker", discord_user_id=None,
            admin_discord_id=0, admin_name="FRA Bot",
            sanction_type="Warning - Official 1st warning",
            reason="mirror", source=source,
        )
    result = await _issue_warning(svc)
    assert result["offense_count"] == 1
    assert result["escalation"] is None


async def test_auto_escalation_mutes_at_the_second_step(db):
    svc = _svc(db, mode="auto", gap=24)
    await _issue_warning(svc, days_ago=3.0)
    await _issue_warning(svc, days_ago=2.0)
    lines = await svc.sweep()
    assert any("escalation-muted" in line for line in lines)
    rows = await svc.repo.for_member(mc_user_id=42)
    mute = next(r for r in rows if str(r["sanction_type"]).startswith("Mute"))
    assert mute["source"] == "escalation"
    assert mute["sanction_type"] == "Mute 1d"
    # The consequence is on record — no repeat next pass, and it never
    # counts as a new offense.
    assert await svc.sweep() == []
    assert await svc.repo.offense_count(mc_user_id=42) == 2


async def test_auto_escalation_waits_for_the_gap(db):
    svc = _svc(db, mode="auto", gap=24)
    await _issue_warning(svc, days_ago=3.0)
    await _issue_warning(svc, days_ago=0.0)   # fresh — inside the window
    assert await svc.sweep() == []


async def test_auto_escalation_kicks_at_the_threshold(db, monkeypatch):
    kicked = []

    async def fake_kick(client, mc_user_id):
        kicked.append(mc_user_id)
        return True, "kick confirmed"

    monkeypatch.setattr(
        "fra_bot.services.sanctions.kick_alliance_member", fake_kick
    )
    await _add_roster_member(db, 42, "Slacker")
    svc = _svc(db, mode="auto", gap=24)
    for days in (4.0, 3.0, 2.0):
        await _issue_warning(svc, days_ago=days)
    lines = await svc.sweep()
    assert kicked == [42]
    assert any("removed from the alliance" in line for line in lines)
    rows = await svc.repo.for_member(mc_user_id=42)
    kick = next(r for r in rows if r["sanction_type"] == "Kick")
    assert kick["source"] == "escalation"
    assert await svc.sweep() == []


async def test_auto_kick_gives_up_after_repeated_failures(db, monkeypatch):
    async def fake_kick(client, mc_user_id):
        return False, "no rights"

    monkeypatch.setattr(
        "fra_bot.services.sanctions.kick_alliance_member", fake_kick
    )
    await _add_roster_member(db, 42, "Slacker")
    svc = _svc(db, mode="auto", gap=24, notice=False)
    for days in (4.0, 3.0, 2.0):
        await _issue_warning(svc, days_ago=days)
    assert any("failed" in line for line in await svc.sweep())
    assert any("failed" in line for line in await svc.sweep())
    assert any("giving up" in line for line in await svc.sweep())
    # Gave up: silent from here on.
    assert await svc.sweep() == []


async def test_auto_mode_dry_run_only_reports(db, monkeypatch):
    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("game touched in dry-run")

    monkeypatch.setattr(
        "fra_bot.services.sanctions.kick_alliance_member", boom
    )
    svc = _svc(db, mode="auto", gap=24, dry_run=True)
    for days in (4.0, 3.0, 2.0):
        await _issue_warning(svc, days_ago=days)
    lines = await svc.sweep()
    assert any("[dry-run]" in line for line in lines)
    assert all(r["sanction_type"] != "Kick"
               for r in await svc.repo.for_member(mc_user_id=42))


async def test_button_kick_reports_when_member_already_gone(db):
    svc = _svc(db)   # roster is empty
    line = await svc.execute_escalation_kick(
        mc_user_id=42, name="Slacker", discord_user_id=None,
        count=3, by="Boss",
    )
    assert line is not None and "cannot be kicked" in line
