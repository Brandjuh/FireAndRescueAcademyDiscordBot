"""The class-availability panel: cache-reusing hourly refresh, the embed,
and the keeper registration."""

import asyncio
import json
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.cogs.classes_panel import ClassesPanelCog
from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo
from fra_bot.services.trainings import (
    AVAILABILITY_STATE_KEY,
    TrainingsService,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "classes.sqlite3")
    await database.connect()
    yield database
    await database.close()


class FakeClient:
    """Serves the academy walk and counts every page fetch."""

    def __init__(self, pages):
        self.pages = pages
        self.fetches = 0

    async def fetch_page(self, path, *, referer=None):
        self.fetches += 1
        return self.pages.get(path, "<html></html>")


ACADEMY_LIST = (
    "<table><tr search_attribute='Fire Academy'>"
    "<td><img building_id='4951748' src='/img/fire.png' alt='Fire'/></td>"
    "<td><a href='/buildings/4951748' class='btn btn-success'>"
    "Start a new training course</a></td></tr></table>"
)
ACADEMY_PAGE = (
    "<form action='/buildings/4951748/education' method='post'>"
    "<input type='hidden' name='authenticity_token' value='tok'/>"
    "<select name='building_rooms_use'><option value='1'>1</option>"
    "<option value='2'>2</option></select>"
    "<select name='alliance[cost]'><option value='0'>Free</option></select>"
    "<select name='education_select'><option value='12'>HazMat</option></select>"
    "<input type='submit' value='Educate'/></form>"
)


def _service(db):
    svc = TrainingsService.__new__(TrainingsService)
    svc.cfg = SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=True, reply_to_board=False,
            training=SimpleNamespace(
                thread_id=5935, interval=5, min_contribution_rate=5.0,
                preferred_academies={},
            ),
        )
    )
    svc.client = FakeClient({
        "/verband/gebauede": ACADEMY_LIST,
        "/buildings/4951748": ACADEMY_PAGE,
    })
    svc.state = StateRepo(db)
    return svc


# ---------------------------------------------------------------------------
# refresh_availability: walk, cache, reuse
# ---------------------------------------------------------------------------

async def test_refresh_walks_and_caches_when_cold(db):
    svc = _service(db)
    data = await svc.refresh_availability()
    assert data["counts"]["fire"] == 2  # two free rooms in the fixture page
    assert data["at"] > 0
    assert svc.client.fetches > 0
    # The cache landed in state for the cog / chooser to read.
    assert await StateRepo(db).get(AVAILABILITY_STATE_KEY) is not None


async def test_refresh_reuses_a_fresh_cache_without_game_traffic(db):
    svc = _service(db)
    first = await svc.refresh_availability()
    fetched = svc.client.fetches
    second = await svc.refresh_availability()
    assert second == first
    assert svc.client.fetches == fetched  # no new page fetches


async def test_refresh_walks_again_once_the_cache_is_stale(db):
    svc = _service(db)
    await StateRepo(db).set(AVAILABILITY_STATE_KEY, json.dumps({
        "counts": {"fire": 9}, "at": 1,  # ancient
    }))
    data = await svc.refresh_availability()
    assert data["counts"]["fire"] == 2  # re-walked, not the stale 9
    assert svc.client.fetches > 0


async def test_failed_walk_keeps_the_last_good_numbers(db):
    svc = _service(db)
    await StateRepo(db).set(AVAILABILITY_STATE_KEY, json.dumps({
        "counts": {"fire": 3}, "at": 1,  # stale, forces a walk
    }))

    async def broken():
        return None

    svc._collect_availability = broken
    data = await svc.refresh_availability()
    assert data["counts"]["fire"] == 3  # stale beats nothing


# ---------------------------------------------------------------------------
# The panel embed
# ---------------------------------------------------------------------------

class FakeBot(SimpleNamespace):
    async def wait_until_ready(self):
        await asyncio.Event().wait()


async def test_panel_embed_renders_counts_and_timestamp(db):
    svc = _service(db)
    await svc.refresh_availability()
    cog = ClassesPanelCog(FakeBot(db=db, trainings=svc))
    await cog.reload_snapshot()
    embed = cog.panel_embed()
    assert "🚒 Fire Station: **2** classes" in embed.description
    assert "Last updated <t:" in embed.description
    assert "Next update expected <t:" in embed.description
    assert "/training" in embed.description


async def test_panel_embed_placeholder_before_first_walk(db):
    cog = ClassesPanelCog(FakeBot(db=db, trainings=_service(db)))
    await cog.reload_snapshot()
    description = cog.panel_embed().description
    assert "No availability numbers yet" in description
    assert "update is expected within the hour" in description


def test_keeper_registers_the_classes_panel():
    from fra_bot.cogs.panels import PanelKeeperCog

    cfg = SimpleNamespace(
        automation=SimpleNamespace(mission=SimpleNamespace(panel_channel_id=0)),
        discord=SimpleNamespace(channels=SimpleNamespace(
            request_panel=0, member_panel=0, dm_panel=0, class_panel=42,
        )),
    )
    keeper = PanelKeeperCog.__new__(PanelKeeperCog)  # skip the loop start
    keeper.bot = SimpleNamespace(cfg=cfg)
    spec = keeper._spec("classes")
    assert spec is not None
    assert spec.cog_name == "ClassesPanelCog"
    assert spec.channel_id() == 42
