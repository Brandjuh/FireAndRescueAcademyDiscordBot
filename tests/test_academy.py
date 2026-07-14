"""Academy-panel builds: the live #N scan, the funds-gated queue, dry-run,
success/failure recording, and the panel role gate. No network is touched —
the building service's browser builder / live-funds read / geocoder are faked.
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo
from fra_bot.mc.browser_builder import BuildResult
from fra_bot.services import academy as acad
from fra_bot.services.academy import KIND, AcademyService

pytestmark = pytest.mark.asyncio


def _alliance_html(names, *, next_page=None):
    rows = "".join(
        f"<tr search_attribute='{name}'>"
        f"<td><img building_id='{1000 + i}' src='/x.png'/></td></tr>"
        for i, name in enumerate(names)
    )
    nxt = f"<a rel='next' href='{next_page}'>next</a>" if next_page else ""
    return f"<table>{rows}</table>{nxt}"


class _FakeGeo:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return SimpleNamespace(latitude=40.81, longitude=-73.91, address=query)


class _FakeBuilder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def build(self, *, building_type, latitude, longitude, name, address,
                    dry_run=False):
        self.calls.append({"building_type": building_type, "name": name,
                           "lat": latitude, "lng": longitude, "address": address})
        return self.result


class _FakeClient:
    def __init__(self, pages):
        self.pages = pages          # path -> html
        self.fetches = []

    async def fetch_page(self, path, *, referer=None):
        self.fetches.append(path)
        return self.pages.get(path, self.pages.get("*", "<table></table>"))


class _FakeBuildings:
    """Stands in for BuildingsService — only the bits AcademyService reuses."""

    def __init__(self, *, funds, pages, result):
        self.client = _FakeClient(pages)
        self._geocoder = _FakeGeo()
        self._builder = _FakeBuilder(result)
        self.funds = funds          # int or None

    async def _live_funds(self):
        if self.funds is None:
            return None, "could not read"
        return self.funds, None


def _cfg(*, dry_run=False, min_funds=2_000_000, address="Fixed Address"):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            academy=SimpleNamespace(
                enabled=True, role_id=0, interval=10,
                address=address, min_alliance_funds=min_funds,
            ),
        ),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "academy.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _make(db, *, funds=9_000_000, names=(), result=None, pages=None, **cfg_over):
    if pages is None:
        pages = {"/verband/gebauede": _alliance_html(list(names))}
    if result is None:
        result = BuildResult(True, 555001, "created")
    buildings = _FakeBuildings(funds=funds, pages=pages, result=result)
    svc = AcademyService(_cfg(**cfg_over), db, buildings)
    return svc, buildings


@pytest.fixture(autouse=True)
def _browser_available(monkeypatch):
    # Pretend Playwright is installed so the build path runs (the host may not
    # have it); individual tests can override.
    monkeypatch.setattr(acad.BrowserBuilder, "available", staticmethod(lambda: True))


# ---------------------------------------------------------------------------
# Live #N scan
# ---------------------------------------------------------------------------

async def test_next_number_scans_existing_names(db):
    names = [
        "[AA] Fire academy #3", "[AA] Fire academy #7",   # highest fire = 7
        "[AA] Police academy #2",
        "Fire station 12", "[AA] Coastal Rescue school #4",
    ]
    svc, _ = _make(db, names=names)
    assert await svc._next_number(acad.ACADEMIES["fire"]) == 8
    assert await svc._next_number(acad.ACADEMIES["police"]) == 3
    assert await svc._next_number(acad.ACADEMIES["rescue"]) == 1   # none yet
    assert await svc._next_number(acad.ACADEMIES["coastal"]) == 5


async def test_next_number_walks_pages(db):
    pages = {
        "/verband/gebauede": _alliance_html(["[AA] Fire academy #2"], next_page="/p2"),
        "/p2": _alliance_html(["[AA] Fire academy #9"]),
    }
    svc, _ = _make(db, pages=pages)
    assert await svc._next_number(acad.ACADEMIES["fire"]) == 10


# ---------------------------------------------------------------------------
# Build / queue / dry-run
# ---------------------------------------------------------------------------

async def _enqueue_run(svc, kind="fire"):
    rid = await svc.enqueue(kind, requester_name="Alice",
                            discord_user_id=42, channel_id=7)
    return await svc.run_one(rid)


async def test_build_when_funds_ok(db):
    svc, buildings = _make(db, funds=9_000_000, names=["[AA] Fire academy #4"])
    row = await _enqueue_run(svc)
    assert row["status"] == "done"
    assert buildings._builder.calls[0]["building_type"] == "fire academy"
    assert buildings._builder.calls[0]["name"] == "[AA] Fire academy #5"
    assert "#5" in row["status_detail"]


async def test_low_funds_queues_then_builds_on_recovery(db):
    svc, buildings = _make(db, funds=1_000, min_funds=2_000_000)
    row = await _enqueue_run(svc)
    assert row["status"] == "waiting"          # queued, nothing built
    assert buildings._builder.calls == []
    assert "below floor" in row["status_detail"]

    # Funds recover -> the poller drains the queue and builds.
    buildings.funds = 9_000_000
    processed = await svc.process_queue()
    assert processed == 1
    done = await AutomationRepo(db).get(row["id"])
    assert done["status"] == "done"
    assert len(buildings._builder.calls) == 1


async def test_unreadable_funds_waits_and_bumps(db):
    svc, buildings = _make(db, funds=None)
    row = await _enqueue_run(svc)
    assert row["status"] == "waiting" and buildings._builder.calls == []
    assert row["attempts"] == 1                # transient -> bumped toward the cap


async def test_dry_run_reports_without_building(db):
    svc, buildings = _make(db, dry_run=True, names=["[AA] Police academy #1"])
    row = await _enqueue_run(svc, "police")
    assert row["status"] == "skipped"
    assert "would build [AA] Police academy #2" in row["status_detail"]
    assert buildings._builder.calls == []


async def test_no_browser_skips(db, monkeypatch):
    monkeypatch.setattr(acad.BrowserBuilder, "available", staticmethod(lambda: False))
    svc, buildings = _make(db)
    row = await _enqueue_run(svc)
    assert row["status"] == "skipped"
    assert "no browser" in row["status_detail"]
    assert buildings._builder.calls == []


async def test_build_failure_marks_failed(db):
    svc, _ = _make(db, result=BuildResult(False, None, "MissionChief rejected it"))
    row = await _enqueue_run(svc)
    assert row["status"] == "failed"
    assert "MissionChief rejected it" in row["status_detail"]


async def test_submitted_without_id_is_done(db):
    # Alliance builds don't redirect to /buildings/<id>; ok+no id = submitted.
    svc, _ = _make(db, result=BuildResult(True, None, "submitted (HTTP 200)"))
    row = await _enqueue_run(svc)
    assert row["status"] == "done" and "submitted" in row["status_detail"]


async def test_enqueue_rejects_unknown_kind(db):
    svc, _ = _make(db)
    with pytest.raises(ValueError):
        await svc.enqueue("navy", requester_name="x", discord_user_id=1, channel_id=1)


async def test_coords_cached_but_re_geocode_on_address_change(db):
    # Same address -> one lookup; changing the live address re-geocodes so a
    # build never silently lands at the old spot.
    svc, buildings = _make(db, address="First Address")
    await _enqueue_run(svc)
    await _enqueue_run(svc)
    assert buildings._geocoder.queries == ["First Address"]

    svc._auto.address = "Second Address"
    await _enqueue_run(svc)
    assert buildings._geocoder.queries == ["First Address", "Second Address"]
    assert buildings._builder.calls[-1]["address"] == "Second Address"


# ---------------------------------------------------------------------------
# Panel role gate
# ---------------------------------------------------------------------------

def _member(*, admin=False, role_ids=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=admin),
        roles=[SimpleNamespace(id=r) for r in role_ids],
    )


def test_role_gate_admins_and_configured_role():
    from fra_bot.cogs.academy import _member_may_build

    cfg = SimpleNamespace(
        discord=SimpleNamespace(admin_role_ids=(111,)),
        automation=SimpleNamespace(academy=SimpleNamespace(role_id=999)),
    )
    assert _member_may_build(_member(admin=True), cfg) is True          # server admin
    assert _member_may_build(_member(role_ids=(999,)), cfg) is True      # academy role
    assert _member_may_build(_member(role_ids=(111,)), cfg) is True      # admin role
    assert _member_may_build(_member(role_ids=(222,)), cfg) is False     # neither
    # role_id 0 = admins only: a random role is not enough.
    cfg.automation.academy.role_id = 0
    assert _member_may_build(_member(role_ids=(999,)), cfg) is False
