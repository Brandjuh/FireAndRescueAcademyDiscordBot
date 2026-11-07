"""Leave feedback: one "why did you leave?" PM to members who LEFT (not
kicked), after a delay, unless they rejoined / opted out / were asked."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

import fra_bot.mc.messages as messages
from fra_bot.db.database import Database
from fra_bot.db.repos import MemberActionsRepo, MemberPrefsRepo, StateRepo
from fra_bot.services import leave_feedback as lf

NOW = dt.datetime.now(dt.timezone.utc)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "leave.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _cfg(*, enabled=True, delay=60, dry_run=False):
    return SimpleNamespace(automation=SimpleNamespace(
        dry_run=dry_run,
        leave_feedback=SimpleNamespace(enabled=enabled, delay_minutes=delay),
    ))


_sig = 0


async def _log(db, action, name, mc_id, *, minutes_ago):
    global _sig
    _sig += 1
    at = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO alliance_logs (signature, raw_timestamp, event_at, action_key, "
        "description, executed_name, executed_mc_id, scraped_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"s{_sig}", at, at, action, action, name, mc_id, at),
    )


async def _member(db, mc_id, name):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, first_seen_at, last_seen_at, is_active) "
        "VALUES (?, ?, '2026-01-01', '2026-11-06', 1)", (mc_id, name),
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(client, recipient, subject, body):
        calls.append((recipient, subject, body))
        return True, "sent", "555"

    monkeypatch.setattr(messages, "send_new_message", fake_send)
    return calls


async def _service(db, **cfg):
    service = lf.LeaveFeedbackService(_cfg(**cfg), client=None, db=db)
    service.send_spacing = 0
    # watermark as if the feature was enabled before the test rows
    await StateRepo(db).set(lf.WATERMARK_KEY, "0")
    return service


async def test_first_run_is_never_retroactive(db, sent):
    await _log(db, "left_alliance", "OldLeaver", 1, minutes_ago=90)
    service = lf.LeaveFeedbackService(_cfg(), client=None, db=db)
    assert await service.run() == []          # sets the watermark only
    assert await service.run() == []          # and the old leave stays unasked
    assert sent == []


async def test_a_member_who_left_is_asked_once(db, sent):
    await _log(db, "left_alliance", "Jcowley", 1303447, minutes_ago=90)
    service = await _service(db)
    lines = await service.run()
    assert lines == ["📨 asked Jcowley why they left"]
    assert len(sent) == 1
    recipient, subject, body = sent[0]
    assert recipient == "Jcowley"
    assert subject == lf.SUBJECT
    assert body.startswith("Hi Jcowley,") and "6. Something else" in body
    # recorded in the member's history
    actions = await MemberActionsRepo(db).for_member(mc_user_id=1303447)
    assert [a["action"] for a in actions] == ["leave_feedback_asked"]
    # a second "left" row from before we asked (left, rejoined and left
    # again inside the delay window): still the same PM, no second one
    await _log(db, "left_alliance", "Jcowley", 1303447, minutes_ago=70)
    assert await service.run() == []
    assert len(sent) == 1


async def test_left_came_back_and_left_again_is_asked_again(db, sent):
    await _log(db, "left_alliance", "Yoyo", 12, minutes_ago=600)
    service = await _service(db)
    service.settings.delay_minutes = 60
    # first leave: asked (pretend that happened 8 hours ago)
    await StateRepo(db).set(
        lf._asked_key(12),
        (NOW - dt.timedelta(hours=8)).isoformat(timespec="seconds"),
    )
    assert await service.run() == []          # the old leave: already asked
    # came back, left again 90 minutes ago — after we asked: ask again
    await _log(db, "left_alliance", "Yoyo", 12, minutes_ago=90)
    assert await service.run() == ["📨 asked Yoyo why they left"]
    assert [r for r, _, _ in sent] == ["Yoyo"]
    # and not a third time for that same leave
    assert await service.run() == []
    assert len(sent) == 1


async def test_kicked_members_are_never_asked(db, sent):
    await _log(db, "kicked_from_alliance", "Kicked", 2, minutes_ago=90)
    service = await _service(db)
    assert await service.run() == []
    assert sent == []


async def test_waits_for_the_delay(db, sent):
    await _log(db, "left_alliance", "Fresh", 3, minutes_ago=10)
    service = await _service(db)
    assert await service.run() == []
    assert sent == []
    # still pending: the watermark did not skip past it
    assert await StateRepo(db).get(lf.WATERMARK_KEY) == "0"
    service.settings.delay_minutes = 5
    assert await service.run() == ["📨 asked Fresh why they left"]


async def _members_sweep(db, *, minutes_ago):
    at = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO scrape_runs (scraper, started_at, finished_at, status) "
        "VALUES ('members', ?, ?, 'success')", (at, at),
    )


async def test_rejoined_members_are_not_asked(db, sent):
    await _log(db, "left_alliance", "Boomerang", 4, minutes_ago=90)
    await _member(db, 4, "Boomerang")
    await _members_sweep(db, minutes_ago=30)        # swept after the leave
    service = await _service(db)
    assert await service.run() == ["↩️ Boomerang left and is back already — not asked"]
    assert sent == []


async def test_a_roster_older_than_the_leave_is_no_proof_of_a_rejoin(db, sent):
    # The roster sweep runs every two hours: right after a leave the stored
    # roster still lists the player as active. That is not a rejoin.
    await _log(db, "left_alliance", "KingHarry", 10, minutes_ago=20)
    await _member(db, 10, "KingHarry")
    await _members_sweep(db, minutes_ago=110)       # before the leave
    service = await _service(db, delay=15)
    assert await service.run() == []                # waits for a new sweep
    assert sent == []
    assert await StateRepo(db).get(lf.WATERMARK_KEY) == "0"
    # the next sweep sees they are gone → asked
    await db.execute("UPDATE members SET is_active = 0 WHERE mc_user_id = 10")
    await _members_sweep(db, minutes_ago=5)
    assert await service.run() == ["📨 asked KingHarry why they left"]
    assert [r for r, _, _ in sent] == ["KingHarry"]


async def test_players_who_turned_bot_pms_off_are_skipped(db, sent):
    await _log(db, "left_alliance", "Quiet", 5, minutes_ago=90)
    await MemberPrefsRepo(db).set_ingame_pms(5, False)
    service = await _service(db)
    assert await service.run() == []
    assert sent == []


async def test_old_leaves_are_skipped(db, sent):
    await _log(db, "left_alliance", "Ancient", 6, minutes_ago=60 * (lf.MAX_AGE_HOURS + 1))
    service = await _service(db)
    assert await service.run() == []
    assert sent == []


async def test_dry_run_sends_nothing(db, sent):
    await _log(db, "left_alliance", "Tester", 7, minutes_ago=90)
    service = await _service(db, dry_run=True)
    assert await service.run() == ["🧪 dry run — would ask Tester why they left"]
    assert sent == []


async def test_disabled_does_nothing(db, sent):
    await _log(db, "left_alliance", "Someone", 8, minutes_ago=90)
    service = await _service(db, enabled=False)
    assert await service.run() == []
    assert sent == []


async def test_a_rush_of_leaves_is_paced(db, sent):
    for i in range(lf.MAX_PER_PASS + 2):
        await _log(db, "left_alliance", f"P{i}", 100 + i, minutes_ago=90)
    service = await _service(db)
    assert len(await service.run()) == lf.MAX_PER_PASS
    assert len(await service.run()) == 2
    assert len(sent) == lf.MAX_PER_PASS + 2
    assert len({r for r, _, _ in sent}) == lf.MAX_PER_PASS + 2   # nobody twice


async def test_rejected_pm_is_retried_then_given_up(db, monkeypatch):
    calls = []

    async def rejected(client, recipient, subject, body):
        calls.append(recipient)
        return False, "Recipient not found", None

    monkeypatch.setattr(messages, "send_new_message", rejected)
    await _log(db, "left_alliance", "Renamed", 9, minutes_ago=90)
    service = await _service(db)
    first = await service.run()
    assert first and "retry 1/" in first[0]
    await service.run()
    last = await service.run()
    assert last and "gave up" in last[0]
    assert await service.run() == []
    assert len(calls) == lf.MAX_SEND_ATTEMPTS


async def test_unconfirmed_delivery_is_not_resent(db, monkeypatch):
    calls = []

    async def unconfirmed(client, recipient, subject, body):
        calls.append(recipient)
        return False, messages.UNCONFIRMED_PREFIX + " (no flash)", "77"

    monkeypatch.setattr(messages, "send_new_message", unconfirmed)
    await _log(db, "left_alliance", "Maybe", 10, minutes_ago=90)
    service = await _service(db)
    lines = await service.run()
    assert "not confirmed" in lines[0]
    assert await service.run() == []
    assert calls == ["Maybe"]


async def test_the_sent_pm_is_mirrored(db, sent):
    mirrored = []

    async def mirror(conversation_id, username, subject):
        mirrored.append((conversation_id, username, subject))

    await _log(db, "left_alliance", "Mirror", 11, minutes_ago=90)
    service = await _service(db)
    service.mirror = mirror
    await service.run()
    assert mirrored == [("555", "Mirror", lf.SUBJECT)]


def test_the_settings_are_registered():
    from fra_bot.core.settings import SETTINGS

    paths = {s.path for s in SETTINGS}
    assert {"automation.leave_feedback.enabled",
            "automation.leave_feedback.delay_minutes"} <= paths


def test_config_defaults():
    from fra_bot.config import LeaveFeedbackConfig

    cfg = LeaveFeedbackConfig()
    assert cfg.enabled is True and cfg.delay_minutes == 60
