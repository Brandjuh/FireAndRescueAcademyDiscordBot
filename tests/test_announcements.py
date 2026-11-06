"""The announcements line: a template with placeholders, checked against
the game's 200 characters, saved on demand and auto-refreshed."""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.services import announcements as an
from tests.test_alliance_text import FakeServer

NOW = dt.datetime(2026, 11, 6, 20, 15, tzinfo=dt.timezone.utc)
AVAILABILITY = {"counts": {"fire": 4, "police": 0, "ems": 32, "coastal": 1}, "at": 0}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "announce.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _service(db, server=None, *, enabled=True, interval=60, dry_run=False,
             availability=AVAILABILITY):
    cfg = SimpleNamespace(automation=SimpleNamespace(
        dry_run=dry_run,
        announcements=SimpleNamespace(enabled=enabled, interval_minutes=interval),
    ))

    async def provider():
        return availability

    return an.AnnouncementsService(cfg, server or FakeServer(), db,
                                   availability=provider)


def _live(server):
    return server.values["alliance_text[chat_header]"]


def test_free_class_values():
    values = an.free_class_values(AVAILABILITY)
    # Only academy types WITH a free class are listed.
    assert values["free_classes"] == "Fire 4 · EMS 32 · Water 1"
    assert values["free_total"] == "37"
    assert values["free_police"] == "0"
    assert an.free_class_values({"counts": {"fire": 0}})["free_classes"] == "none"
    assert an.free_class_values(None)["free_classes"] == "?"


def test_render_fills_known_and_keeps_unknown():
    text = an.render("Free: {free_total} {nope} {tip}", {"free_total": "3", "tip": ""})
    assert text == "Free: 3 {nope}"


async def test_set_template_saves_the_render_in_the_game(db):
    server = FakeServer()
    svc = _service(db, server)
    rendered, result = await svc.set_template("📚 Free classes: {free_classes}")
    assert result.ok and result.saved
    assert rendered == "📚 Free classes: Fire 4 · EMS 32 · Water 1"
    assert _live(server) == rendered
    assert await svc.template() == "📚 Free classes: {free_classes}"


async def test_unknown_placeholder_is_refused(db):
    server = FakeServer()
    svc = _service(db, server)
    with pytest.raises(an.TemplateError, match="free_clases"):
        await svc.set_template("Free: {free_clases}")
    assert server.posts == [] and await svc.template() == ""


async def test_too_long_counts_the_longest_tip(db):
    svc = _service(db)
    await svc.add_tip("short")
    await svc.add_tip("y" * 150)
    with pytest.raises(an.TemplateError, match="longest tip"):
        await svc.set_template("x" * 60 + " {tip}")  # 211 with the long tip


async def test_a_tip_that_would_overflow_the_saved_text_is_refused(db):
    svc = _service(db)
    await svc.set_template("x" * 150 + " {tip}")
    with pytest.raises(an.TemplateError, match="too long"):
        await svc.add_tip("z" * 60)
    assert await svc.tips() == []


async def test_tips_add_list_remove(db):
    svc = _service(db)
    assert await svc.add_tip("Use the Board for requests") == 1
    assert await svc.add_tip("Check #free-classes") == 2
    with pytest.raises(an.TemplateError):
        await svc.add_tip("has {braces}")
    assert await svc.remove_tip(1) == "Use the Board for requests"
    assert await svc.tips() == ["Check #free-classes"]
    with pytest.raises(an.TemplateError):
        await svc.remove_tip(5)


async def test_auto_refresh_off_does_nothing(db):
    server = FakeServer()
    svc = _service(db, server, enabled=False)
    await svc.state.set(an.TEMPLATE_KEY, "Free: {free_total}")
    assert await svc.run(NOW) == []
    assert server.posts == []


async def test_auto_refresh_waits_for_the_interval_and_rotates_tips(db):
    server = FakeServer()
    svc = _service(db, server, interval=60)
    await svc.add_tip("Tip one")
    await svc.add_tip("Tip two")
    await svc.state.set(an.TEMPLATE_KEY, "Free {free_total} • {tip}")

    assert await svc.run(NOW) == []           # first run: saves, quietly
    assert _live(server) == "Free 37 • Tip one"
    posts = len(server.posts)

    await svc.run(NOW + dt.timedelta(minutes=30))   # not due yet
    assert len(server.posts) == posts

    await svc.run(NOW + dt.timedelta(minutes=61))   # due: next tip
    assert _live(server) == "Free 37 • Tip two"
    await svc.run(NOW + dt.timedelta(minutes=122))  # and round again
    assert _live(server) == "Free 37 • Tip one"


async def test_auto_refresh_skips_the_save_when_nothing_changed(db):
    server = FakeServer()
    svc = _service(db, server)
    await svc.state.set(an.TEMPLATE_KEY, "Free {free_total}")
    await svc.run(NOW)
    posts = len(server.posts)
    await svc.run(NOW + dt.timedelta(minutes=61))
    assert len(server.posts) == posts          # read, compared, not re-saved


async def test_failure_is_reported_once_then_recovery(db):
    server = FakeServer()
    server.ignore_posts = True
    svc = _service(db, server, interval=10)
    await svc.state.set(an.TEMPLATE_KEY, "Free {free_total}")

    lines = await svc.run(NOW)
    assert len(lines) == 1 and "failed" in lines[0]
    assert await svc.run(NOW + dt.timedelta(minutes=11)) == []  # same error: quiet

    server.ignore_posts = False
    lines = await svc.run(NOW + dt.timedelta(minutes=22))
    assert lines == ["✅ auto-refresh works again."]


async def test_overflow_at_refresh_time_is_not_saved(db):
    server = FakeServer()
    big = {"counts": {"fire": 10**150}}
    svc = _service(db, server, availability=big)
    await svc.state.set(an.TEMPLATE_KEY, "Free {free_total} " + "x" * 60)
    lines = await svc.run(NOW)
    assert server.posts == []
    assert "not saved" in lines[0]


async def test_manual_set_restarts_the_auto_clock(db):
    server = FakeServer()
    svc = _service(db, server, interval=60)
    await svc.set_template("Free {free_total}")
    due = await svc.next_refresh_at()
    assert due is not None
    assert due > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=59)


async def test_dry_run_reports_without_saving(db):
    server = FakeServer()
    svc = _service(db, server, dry_run=True)
    _rendered, result = await svc.set_template("Free {free_total}")
    assert result.ok and not result.saved
    assert server.posts == []
