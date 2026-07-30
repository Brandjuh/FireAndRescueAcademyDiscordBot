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


async def test_undeliverable_warning_gives_up_after_max_attempts(db, monkeypatch):
    """An unconfirmed/undeliverable PM (ghost or blocked account) is retried a
    few times, then abandoned — never retried every pass forever."""
    from fra_bot.services.tax_warnings import MAX_SEND_ATTEMPTS

    calls = {"n": 0}

    async def fake_send(client, recipient, subject, body):
        calls["n"] += 1
        return False  # the game never confirms delivery

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Ghost", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)

    for i in range(1, MAX_SEND_ATTEMPTS + 1):
        lines = await svc.scan()
        assert calls["n"] == i
        if i < MAX_SEND_ATTEMPTS:
            assert any(f"attempt {i}/{MAX_SEND_ATTEMPTS}" in ln for ln in lines)
        else:
            assert any("giving up" in ln for ln in lines)

    # Given up: later scans neither send nor spam the admin.
    assert await svc.scan() == []
    assert calls["n"] == MAX_SEND_ATTEMPTS


async def test_giveup_clears_when_member_fixes_rate(db, monkeypatch):
    """After giving up, a member who fixes their donation and later dips again
    gets a fresh set of attempts (the give-up is not permanent)."""
    from fra_bot.services.tax_warnings import MAX_SEND_ATTEMPTS

    result = {"ok": False}

    async def fake_send(client, recipient, subject, body):
        return result["ok"]

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Ghost", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)

    for _ in range(MAX_SEND_ATTEMPTS):
        await svc.scan()          # burn through the attempts -> give up
    assert await svc.scan() == []  # silent now

    # Donation fixed -> give-up cleared; a later dip warns again (now sending works).
    await db.execute("UPDATE members SET contribution_rate = 10 WHERE mc_user_id = 1")
    await svc.scan()
    await db.execute("UPDATE members SET contribution_rate = 1 WHERE mc_user_id = 1")
    result["ok"] = True
    lines = await svc.scan()
    assert any("warning 1/3 sent to Ghost" in ln for ln in lines)


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


# ---------------------------------------------------------------------------
# Stale-roster gate, unconfirmed sends, kick verification
# ---------------------------------------------------------------------------

async def _record_members_run(db, *, hours_ago: float) -> None:
    iso = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    ).isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO scrape_runs (scraper, status, started_at, finished_at) "
        "VALUES ('members', 'success', ?, ?)",
        (iso, iso),
    )


async def test_stale_roster_skips_warnings_but_still_resolves(db, monkeypatch):
    # A failing members sync freezes the stored rates; warning on them hits
    # members who already fixed their donation days ago. Resolving stays
    # allowed (it only STOPS warnings).
    sent = []

    async def fake_send(client, recipient, subject, body):
        sent.append(recipient)
        return True

    monkeypatch.setattr(
        "fra_bot.mc.messages.send_new_message", _as_new_message(fake_send)
    )
    await _add_member(db, 1, "Slacker", 1.0)
    await _add_member(db, 2, "Fixed", 10.0)
    await _record_members_run(db, hours_ago=26)
    svc = TaxWarningService(_cfg(), FakeClient(), db)
    await svc.warnings.record_warning(2, "Fixed", count=1)

    lines = await svc.scan()
    assert any("skipped" in line and "roster" in line for line in lines)
    assert any("donation fixed" in line for line in lines)  # resolve still ran
    assert sent == [] and await svc.warnings.get(1) is None

    # A fresh successful sync re-enables the pass.
    await _record_members_run(db, hours_ago=0)
    lines = await svc.scan()
    assert any("warning 1/3 sent to Slacker" in line for line in lines)
    assert sent == ["Slacker"]


async def test_unconfirmed_send_is_recorded_not_resent(db, monkeypatch):
    # The game regularly delivers a PM without showing its success flash.
    # Resending "until confirmed" gave members the same "no contribution
    # set" warning several times — record it once and move on instead.
    from fra_bot.mc.messages import UNCONFIRMED_PREFIX

    calls = []

    async def fake_send(client, recipient, subject, body):
        calls.append(recipient)
        return False, f"{UNCONFIRMED_PREFIX} (no success flash)", None

    monkeypatch.setattr("fra_bot.mc.messages.send_new_message", fake_send)
    await _add_member(db, 1, "Quiet", 1.0)
    svc = TaxWarningService(_cfg(), FakeClient(), db)

    lines = await svc.scan()
    assert any("did not confirm" in line and "recorded" in line for line in lines)
    state = await svc.warnings.get(1)
    assert state["warning_count"] == 1                    # ladder advanced
    assert await svc.state.get("taxwarn_send_attempts:1") is None
    assert await svc.scan() == []                         # gap applies
    assert len(calls) == 1                                # no duplicate PM


async def test_kick_that_did_not_stick_is_reopened_and_retried(db):
    # The kick route answers 200 even when the game silently refuses; only
    # the roster proves a kick. Still listed hours later -> reopen + retry.
    await _add_member(db, 1, "Stubborn", 1.0)             # still on the roster
    svc = TaxWarningService(_cfg(auto_kick=True), FakeClient(), db)
    await svc.warnings.record_warning(1, "Stubborn", count=MAX_WARNINGS)
    await db.execute(
        "UPDATE tax_warnings SET last_warning_at = ?, kicked_at = ? "
        "WHERE mc_user_id = 1",
        (_iso(8), _iso(0.2)),                             # "kicked" ~5h ago
    )

    lines = await svc.scan()
    assert any("did NOT stick" in line for line in lines)
    assert any("kicked after" in line for line in lines)  # retried right away
    assert (await svc.warnings.get(1))["kicked_at"] is not None


async def test_kick_verification_waits_for_the_roster(db):
    await _add_member(db, 1, "JustKicked", 1.0)
    svc = TaxWarningService(_cfg(auto_kick=True), FakeClient(), db)
    await svc.warnings.record_warning(1, "JustKicked", count=MAX_WARNINGS)

    # Kicked 30 minutes ago: the hourly roster can't have observed it yet.
    await db.execute(
        "UPDATE tax_warnings SET last_warning_at = ?, kicked_at = ? "
        "WHERE mc_user_id = 1",
        (_iso(8), _iso(0.5 / 24)),
    )
    assert not any("did NOT stick" in line for line in await svc.scan())

    # Kicked 2h ago, but the roster last SAW them before the kick: no verdict.
    await db.execute(
        "UPDATE tax_warnings SET kicked_at = ? WHERE mc_user_id = 1",
        (_iso(2 / 24),),
    )
    await db.execute(
        "UPDATE members SET last_seen_at = ? WHERE mc_user_id = 1",
        (_iso(3 / 24),),
    )
    assert not any("did NOT stick" in line for line in await svc.scan())
    assert (await svc.warnings.get(1))["kicked_at"] is not None


async def test_confirmed_kick_stays_closed(db):
    # Member really gone from the roster: the kicked trail stays closed.
    await _add_member(db, 1, "Gone", 1.0)
    await db.execute("UPDATE members SET is_active = 0 WHERE mc_user_id = 1")
    svc = TaxWarningService(_cfg(auto_kick=True), FakeClient(), db)
    await svc.warnings.record_warning(1, "Gone", count=MAX_WARNINGS)
    await db.execute(
        "UPDATE tax_warnings SET kicked_at = ? WHERE mc_user_id = 1",
        (_iso(0.5),),
    )
    assert await svc.scan() == []
    assert (await svc.warnings.get(1))["kicked_at"] is not None
