"""In-game PM opt-out: the preference repo, the ``/ingame-pms`` reply and
every notification path that honours it."""

import json
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MemberPrefsRepo


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "prefs.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _member(db, mc_id, name, *, discord_id=None, status="approved"):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, 10.0, 1, '2026-01-01', "
        "'2026-07-01')",
        (mc_id, name),
    )
    if discord_id is not None:
        await db.execute(
            "INSERT INTO member_links (discord_id, mc_user_id, status, "
            "reviewer_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, '2026-01-01', '2026-01-01')",
            (discord_id, mc_id, status),
        )


# -- repo -------------------------------------------------------------------

async def test_default_is_on(db):
    prefs = MemberPrefsRepo(db)
    await _member(db, 42, "Alice")
    assert await prefs.ingame_pms_enabled(42) is True
    assert await prefs.ingame_pms_enabled(None) is True
    assert await prefs.ingame_pms_enabled_for_name("Alice") is True
    assert await prefs.ingame_pms_enabled_for_name("Nobody") is True
    assert await prefs.ingame_pms_enabled_for_name(None) is True


async def test_opt_out_and_back_in(db):
    prefs = MemberPrefsRepo(db)
    await _member(db, 42, "Alice")
    await prefs.set_ingame_pms(42, False)
    assert await prefs.ingame_pms_enabled(42) is False
    # Board posts carry the name in whatever case the member typed it.
    assert await prefs.ingame_pms_enabled_for_name("alice") is False
    await prefs.set_ingame_pms(42, True)
    assert await prefs.ingame_pms_enabled(42) is True
    assert await prefs.ingame_pms_enabled_for_name("Alice") is True


# -- /ingame-pms --------------------------------------------------------------

def _profile_cog(db, actions):
    from fra_bot.cogs.profile import ProfileCog

    async def log_member_action(**kwargs):
        actions.append(kwargs)

    cog = ProfileCog.__new__(ProfileCog)
    cog.bot = SimpleNamespace(db=db, log_member_action=log_member_action)
    return cog


async def test_ingame_pms_command_needs_a_verified_link(db):
    actions = []
    cog = _profile_cog(db, actions)
    await _member(db, 42, "Alice", discord_id=100, status="pending")
    reply = await cog.ingame_pms_reply(SimpleNamespace(id=100), "off")
    assert "verified" in reply
    assert await MemberPrefsRepo(db).ingame_pms_enabled(42) is True
    assert actions == []


async def test_ingame_pms_command_sets_and_shows(db):
    actions = []
    cog = _profile_cog(db, actions)
    await _member(db, 42, "Alice", discord_id=100)
    user = SimpleNamespace(id=100, display_name="Alice")

    assert "**on**" in await cog.ingame_pms_reply(user, None)
    assert "**off**" in await cog.ingame_pms_reply(user, "off")
    assert await MemberPrefsRepo(db).ingame_pms_enabled(42) is False
    assert "**off**" in await cog.ingame_pms_reply(user, None)
    assert "**on**" in await cog.ingame_pms_reply(user, "on")
    assert await MemberPrefsRepo(db).ingame_pms_enabled(42) is True
    assert [a["detail"] for a in actions] == ["in-game PMs off", "in-game PMs on"]


# -- notification paths -------------------------------------------------------

async def test_closed_dm_fallback_respects_opt_out(db):
    import discord

    from fra_bot.cogs.automation import AutomationCog

    await _member(db, 42, "Alice", discord_id=100)
    await MemberPrefsRepo(db).set_ingame_pms(42, False)

    class ClosedDmUser:
        async def send(self, text):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "DMs closed"
            )

    sent_pms = []

    async def send_new(name, subject, body):
        sent_pms.append(name)
        return {"ok": True}

    cog = AutomationCog.__new__(AutomationCog)
    cog.bot = SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(automation=SimpleNamespace(dry_run=False)),
        dm_mirror=SimpleNamespace(send_new=send_new),
        get_user=lambda uid: ClosedDmUser(),
    )
    row = {
        "id": 9, "kind": "building", "status": "done",
        "status_detail": "built hospital #5",
        "requester_mc_id": 42, "requester_name": "Discord Nick",
        "payload": json.dumps({
            "discord_user_id": 100, "building_type": "hospital",
            "address": "St Mary Hospital", "building_id": 5,
        }),
    }
    await cog._notify_requester(row)
    assert sent_pms == []


async def test_reminder_fallback_respects_opt_out(db):
    from fra_bot.cogs.requests_panel import RequestsCog

    await _member(db, 42, "Alice", discord_id=100)
    await MemberPrefsRepo(db).set_ingame_pms(42, False)
    sent_pms = []

    async def send_new(name, subject, body):
        sent_pms.append(name)
        return {"ok": True}

    cog = RequestsCog.__new__(RequestsCog)
    cog.bot = SimpleNamespace(db=db, dm_mirror=SimpleNamespace(send_new=send_new))
    await cog._reminder_ingame_fallback({"discord_user_id": 100}, "🔔 done")
    assert sent_pms == []

    await MemberPrefsRepo(db).set_ingame_pms(42, True)
    await cog._reminder_ingame_fallback({"discord_user_id": 100}, "🔔 done")
    assert sent_pms == ["Alice"]


async def test_training_pm_respects_opt_out(db, monkeypatch):
    from fra_bot.services.trainings import TrainingsService

    sent = []

    async def fake_send(client, recipient, subject, body):
        sent.append(recipient)
        return True

    monkeypatch.setattr("fra_bot.mc.messages.send_ingame_message", fake_send)
    await _member(db, 42, "Alice")
    svc = TrainingsService.__new__(TrainingsService)
    svc.client = object()
    svc.prefs = MemberPrefsRepo(db)
    opened = [{"name": "HazMat", "building_id": 1, "dry_run": False}]

    await svc._notify_ingame("Alice", opened)
    assert sent == ["Alice"]
    await svc.prefs.set_ingame_pms(42, False)
    await svc._notify_ingame("Alice", opened)
    assert sent == ["Alice"]


async def test_building_pm_respects_opt_out(db, monkeypatch):
    from fra_bot.services.buildings import BuildingsService

    sent = []

    async def fake_send(client, recipient, subject, body):
        sent.append(recipient)
        return True

    monkeypatch.setattr("fra_bot.mc.messages.send_ingame_message", fake_send)
    await _member(db, 42, "Alice")
    await MemberPrefsRepo(db).set_ingame_pms(42, False)
    svc = BuildingsService.__new__(BuildingsService)
    svc.client = object()
    svc.prefs = MemberPrefsRepo(db)
    await svc._notify_ingame_built("Alice", "hospital", "St Mary", None, None, None)
    assert sent == []
