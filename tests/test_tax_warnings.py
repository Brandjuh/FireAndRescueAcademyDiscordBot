"""Member tax (5% donation) warnings: escalation ladder, grace period,
per-run cap, kick flagging, and the reset the moment a member fixes it."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database, utcnow_iso
from fra_bot.services.tax_warnings import MAX_WARNINGS, TaxWarningService


@pytest.fixture(autouse=True)
def _no_send_spacing():
    """Tests don't wait out the 90s anti-burst spacing between PMs."""
    original = TaxWarningService.send_spacing
    TaxWarningService.send_spacing = 0
    yield
    TaxWarningService.send_spacing = original


def _as_new_message(fake_send):
    """Adapt a bool-returning fake to send_new_message's (ok, detail, conv)."""
    async def wrapper(client, recipient, subject, body):
        ok = await fake_send(client, recipient, subject, body)
        return ok, "sent" if ok else "refused", "9001" if ok else None
    return wrapper


class FakeClient:
    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None, ajax=False):
        return "<html></html>"

    async def post_form(self, path, data, **kwargs):
        return (200, {}, "")


def _cfg(*, enabled=True, dry_run=False, auto_kick=False, max_per_run=5,
         min_days_between=7, grace_hours=24):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            tax_warnings=SimpleNamespace(
                enabled=enabled, min_rate=5.0,
                min_days_between=min_days_between, grace_hours=grace_hours,
                max_per_run=max_per_run, auto_kick=auto_kick, interval_hours=6,
            ),
        ),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "tax.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _iso(days_ago: float) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    ).isoformat(timespec="seconds")


async def _add_member(db, mc_id, name, rate, *, days_member=30.0):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, ?, ?)",
        (mc_id, name, rate, _iso(days_member), utcnow_iso()),
    )


async def test_low_rate_member_gets_reminder_then_official_warnings(db, monkeypatch):
    sent = []

    async def fake_send(client, recipient, subject, body):
        sent.append((recipient, subject, body))
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Slacker", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)

    lines = await svc.scan()
    assert any("warning 1/3 sent to Slacker" in line for line in lines)
    assert sent[0][0] == "Slacker"
    assert "Reminder" in sent[0][1]
    assert "Hello Slacker" in sent[0][2]

    # Same day again: not due (7-day gap) — nothing sent.
    assert await svc.scan() == []
    assert len(sent) == 1

    # 8 days later: warning 2 (official).
    await db.execute(
        "UPDATE tax_warnings SET last_warning_at = ? WHERE mc_user_id = 1",
        (_iso(8),),
    )
    lines = await svc.scan()
    assert any("warning 2/3" in line for line in lines)
    assert "Warning" in sent[1][1]


async def test_sent_warning_mirrors_to_the_dm_forum_at_send_time(db, monkeypatch):
    """Outgoing-only conversations may never appear on the inbox page the
    mirror scan reads — every sent warning mirrors immediately via the
    hook, like the reference bot's _send_message_and_link."""
    async def fake_send(client, recipient, subject, body):
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    mirrored = []

    async def fake_mirror(conversation_id, username, subject):
        mirrored.append((conversation_id, username, subject))

    await _add_member(db, 1, "Slacker", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)
    svc.mirror = fake_mirror
    lines = await svc.scan()
    assert any("conv #9001" in line for line in lines)
    assert mirrored == [("9001", "Slacker",
                         "Reminder: Please set your alliance donation to 5%")]


async def test_fixed_donation_resets_warnings_immediately(db, monkeypatch):
    async def fake_send(client, recipient, subject, body):
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Reformed", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)
    await svc.scan()                                   # warning 1 sent
    assert (await svc.warnings.get(1))["warning_count"] == 1

    # The member fixes their donation; the next scan resets and stops.
    await db.execute(
        "UPDATE members SET contribution_rate = 7.5 WHERE mc_user_id = 1"
    )
    lines = await svc.scan()
    assert any("donation fixed" in line and "reset" in line for line in lines)
    assert (await svc.warnings.get(1))["warning_count"] == 0
    # And stays quiet afterwards — no more warnings for a fixed member.
    assert await svc.scan() == []

    # A later dip starts over at warning 1, not at 2.
    await db.execute(
        "UPDATE members SET contribution_rate = 2.0 WHERE mc_user_id = 1"
    )
    lines = await svc.scan()
    assert any("warning 1/3" in line for line in lines)


async def test_new_member_grace_period(db, monkeypatch):
    async def fake_send(client, recipient, subject, body):
        raise AssertionError("must not message a brand-new member")

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Newbie", 0.0, days_member=0.5)   # 12h old
    svc = TaxWarningService(_cfg(), FakeClient(), db)
    assert await svc.scan() == []


async def test_max_per_run_cap(db, monkeypatch):
    sent = []

    async def fake_send(client, recipient, subject, body):
        sent.append(recipient)
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    for i in range(4):
        await _add_member(db, i + 1, f"Member{i}", 1.0)
    svc = TaxWarningService(_cfg(max_per_run=2), FakeClient(), db)
    lines = await svc.scan()
    assert len(sent) == 2
    assert sum("sent to" in line for line in lines) == 2


async def test_dry_run_reports_without_sending(db, monkeypatch):
    async def fake_send(client, recipient, subject, body):
        raise AssertionError("dry-run must not send")

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Slacker", 1.0)
    svc = TaxWarningService(_cfg(dry_run=True), FakeClient(), db)
    lines = await svc.scan()
    assert any("[dry-run] would send warning 1" in line for line in lines)
    assert (await svc.warnings.get(1)) is None       # nothing recorded


async def test_third_unresolved_warning_flags_kick_once(db):
    await _add_member(db, 1, "Stubborn", 1.0)
    svc = TaxWarningService(_cfg(auto_kick=False), FakeClient(), db)
    await svc.warnings.record_warning(1, "Stubborn", count=MAX_WARNINGS)
    await db.execute(
        "UPDATE tax_warnings SET last_warning_at = ? WHERE mc_user_id = 1",
        (_iso(8),),
    )
    lines = await svc.scan()
    assert any("kick is due" in line.lower() for line in lines)
    # Flagged once — the next scan doesn't repeat it inside the gap window.
    assert await svc.scan() == []


async def test_auto_kick_kicks_and_records(db):
    await _add_member(db, 1, "Stubborn", 1.0)
    svc = TaxWarningService(_cfg(auto_kick=True), FakeClient(), db)
    await svc.warnings.record_warning(1, "Stubborn", count=MAX_WARNINGS)
    await db.execute(
        "UPDATE tax_warnings SET last_warning_at = ? WHERE mc_user_id = 1",
        (_iso(8),),
    )
    lines = await svc.scan()
    assert any("kicked after" in line for line in lines)
    assert (await svc.warnings.get(1))["kicked_at"] is not None
    # Already kicked: never again.
    assert await svc.scan() == []


async def test_member_who_left_is_cleared(db, monkeypatch):
    async def fake_send(client, recipient, subject, body):
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Gone", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)
    await svc.scan()
    await db.execute("UPDATE members SET is_active = 0 WHERE mc_user_id = 1")
    lines = await svc.scan()
    assert any("left the alliance" in line for line in lines)
    assert await svc.warnings.get(1) is None


async def test_disabled_scan_is_noop_but_force_runs(db, monkeypatch):
    async def fake_send(client, recipient, subject, body):
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Slacker", 1.0)
    svc = TaxWarningService(_cfg(enabled=False), FakeClient(), db)
    assert await svc.scan() == []                    # switch off -> nothing
    lines = await svc.scan(force=True)               # manual command works
    assert any("warning 1/3" in line for line in lines)


async def test_overview_lists_low_members(db):
    await _add_member(db, 1, "Slacker", 1.0)
    await _add_member(db, 2, "Saint", 10.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)
    lines = await svc.overview()
    assert len(lines) == 1
    assert "Slacker" in lines[0] and "0/3" in lines[0]
