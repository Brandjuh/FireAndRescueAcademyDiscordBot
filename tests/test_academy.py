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


def _cfg(*, dry_run=False, min_funds=2_000_000, address="Fixed Address",
         autoscale=False):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            academy=SimpleNamespace(
                enabled=True, role_id=0, interval=10,
                address=address, min_alliance_funds=min_funds,
                autoscale=autoscale,
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
    assert buildings._builder.calls[0]["name"] == "[AA] Fire academy #005"
    assert "#005" in row["status_detail"]


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
    assert "would build [AA] Police academy #002" in row["status_detail"]
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


# ---------------------------------------------------------------------------
# Naming + extensions
# ---------------------------------------------------------------------------

def test_coastal_label_uses_capital_school():
    assert acad.ACADEMIES["coastal"]["label"] == "Coastal Rescue School"


async def test_names_are_three_digit_padded(db):
    svc, _ = _make(db, names=["[AA] Fire academy #4"])
    assert await svc._next_name(acad.ACADEMIES["fire"]) == "[AA] Fire academy #005"
    svc2, _ = _make(db, names=[])
    assert await svc2._next_name(acad.ACADEMIES["police"]) == "[AA] Police academy #001"


class _FakeUpgrader:
    def __init__(self, bought=1):
        self.calls = []
        self._bought = bought

    async def upgrade_one(self, building_id, *, kind, name, enforce_floor=False):
        self.calls.append({"building_id": building_id, "kind": kind,
                           "name": name, "enforce_floor": enforce_floor})
        return SimpleNamespace(extensions_bought=self._bought)


async def test_successful_build_triggers_extension_finish(db):
    svc, _ = _make(db, funds=9_000_000, names=["[AA] Fire academy #4"])
    called = []

    async def spy(name):
        called.append(name)

    svc._finish_extensions = spy
    row = await _enqueue_run(svc)
    assert row["status"] == "done"
    assert called == ["[AA] Fire academy #005"]        # finisher gets the 3-digit name


async def test_finish_extensions_buys_on_the_new_building(db):
    up = _FakeUpgrader()
    svc, _ = _make(db, names=["[AA] Fire academy #007"])   # building_id 1000
    svc._upgrader = up
    await svc._finish_extensions("[AA] Fire academy #007")
    assert len(up.calls) == 1
    assert up.calls[0]["building_id"] == 1000
    assert up.calls[0]["kind"] == "academy"
    assert up.calls[0]["enforce_floor"] is False           # finish immediately


async def test_finish_extensions_without_upgrader_is_noop(db):
    svc, _ = _make(db, names=["[AA] Fire academy #007"])
    await svc._finish_extensions("[AA] Fire academy #007")  # must not raise


async def test_finish_extensions_skips_when_not_listed_yet(db):
    up = _FakeUpgrader()
    svc, _ = _make(db, names=["[AA] Fire academy #001"])
    svc._upgrader = up
    await svc._finish_extensions("[AA] Fire academy #999")  # not on the page
    assert up.calls == []


async def test_sweep_extensions_buys_on_each_academy(db):
    up = _FakeUpgrader()
    svc, _ = _make(db)
    svc._upgrader = up

    async def fake_list():
        return [(1000, "[AA] Fire academy #001"), (1001, "[AA] Police academy #002")]

    svc._list_our_academies = fake_list
    assert await svc.sweep_extensions() == 2                # one extension each
    assert {c["building_id"] for c in up.calls} == {1000, 1001}


async def test_sweep_extensions_is_noop_in_dry_run(db):
    up = _FakeUpgrader()
    svc, _ = _make(db, dry_run=True)
    svc._upgrader = up
    assert await svc.sweep_extensions() == 0
    assert up.calls == []


# ---------------------------------------------------------------------------
# Auto-scale: build a new academy when a discipline hits 0 free classrooms
# ---------------------------------------------------------------------------

import datetime as _dt


async def _set_availability(svc, counts, *, age_s=0, complete=None):
    at = int(_dt.datetime.now(_dt.timezone.utc).timestamp()) - age_s
    if complete is None:
        complete = {k: True for k in counts}       # every academy read OK
    await svc.state.set(acad.AVAILABILITY_STATE_KEY,
                        json.dumps({"counts": counts, "complete": complete, "at": at}))


async def _open_academy_disciplines(db):
    rows = await AutomationRepo(db).recent(50)
    return [
        json.loads(r["payload"])["academy"]
        for r in rows
        if r["kind"] == "academy" and r["status"] in ("pending", "waiting", "processing")
    ]


async def test_autoscale_off_is_noop(db):
    svc, _ = _make(db, autoscale=False)
    await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
    assert await svc.autoscale() == 0
    assert await _open_academy_disciplines(db) == []


async def test_autoscale_debounces_before_building(db):
    svc, _ = _make(db, autoscale=True)
    await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
    assert await svc.autoscale() == 0                 # 1st zero: debounce, no build
    assert await _open_academy_disciplines(db) == []
    await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
    assert await svc.autoscale() == 1                 # 2nd zero: builds
    assert await _open_academy_disciplines(db) == ["fire"]


async def test_autoscale_maps_ems_to_rescue(db):
    svc, _ = _make(db, autoscale=True)
    for _ in range(2):
        await _set_availability(svc, {"fire": 5, "police": 3, "ems": 0, "coastal": 1})
        await svc.autoscale()
    assert await _open_academy_disciplines(db) == ["rescue"]


async def test_autoscale_recovery_resets_the_streak(db):
    svc, _ = _make(db, autoscale=True)
    await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
    await svc.autoscale()                             # streak 1
    await _set_availability(svc, {"fire": 4, "police": 3, "ems": 2, "coastal": 1})
    await svc.autoscale()                             # recovered -> streak reset
    await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
    assert await svc.autoscale() == 0                 # only 1 fresh zero -> no build
    assert await _open_academy_disciplines(db) == []


async def test_autoscale_cooldown_blocks_immediate_rebuild(db):
    svc, _ = _make(db, autoscale=True)
    for _ in range(2):
        await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
        await svc.autoscale()
    assert await _open_academy_disciplines(db) == ["fire"]
    # Mark the pending build done so it isn't the thing blocking, then keep 0.
    for r in await AutomationRepo(db).recent(10):
        if r["kind"] == "academy":
            await AutomationRepo(db).set_status(r["id"], "done", "built")
    for _ in range(3):
        await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
        assert await svc.autoscale() == 0             # 24h cooldown holds
    assert await _open_academy_disciplines(db) == []


async def test_autoscale_defers_to_member_building_requests(db):
    svc, _ = _make(db, autoscale=True)
    await AutomationRepo(db).create(
        kind="building", thread_id=0, post_id=1,
        requester_name="member", requester_mc_id=1, payload="{}",
    )
    for _ in range(2):
        await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1})
        assert await svc.autoscale() == 0             # member build has priority
    assert await _open_academy_disciplines(db) == []


async def test_autoscale_one_build_in_flight_at_a_time(db):
    svc, _ = _make(db, autoscale=True)
    for _ in range(2):
        await _set_availability(svc, {"fire": 0, "police": 0, "ems": 2, "coastal": 1})
        await svc.autoscale()
    # Only one academy queued even though two disciplines are at zero.
    assert len(await _open_academy_disciplines(db)) == 1


async def test_autoscale_skips_stale_availability(db):
    svc, _ = _make(db, autoscale=True)
    for _ in range(2):
        await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1},
                                age_s=4 * 3600)       # 4h old > 3h cap
        assert await svc.autoscale() == 0
    assert await _open_academy_disciplines(db) == []


async def test_autoscale_ignores_incomplete_reading(db):
    # A partial outage: fire reads 0 but not every fire academy page was read.
    # That is "unknown", not "no classrooms" — it must never build.
    svc, _ = _make(db, autoscale=True)
    for _ in range(3):
        await _set_availability(svc, {"fire": 0, "police": 3, "ems": 2, "coastal": 1},
                                complete={"fire": False, "police": True,
                                          "ems": True, "coastal": True})
        assert await svc.autoscale() == 0
    assert await _open_academy_disciplines(db) == []


async def test_autoscale_build_defers_to_member_build_at_execute_time(db):
    # An autoscale academy already queued must still yield the funds to a
    # member hospital/prison request that arrives later.
    svc, _ = _make(db, autoscale=True, funds=9_000_000, names=[])
    rid = await svc.enqueue("fire", requester_name="autoscale",
                            discord_user_id=0, channel_id=0)
    await AutomationRepo(db).create(
        kind="building", thread_id=0, post_id=1,
        requester_name="member", requester_mc_id=1, payload="{}",
    )
    row = await svc.run_one(rid)
    assert row["status"] == "waiting"
    assert "priority" in row["status_detail"]
    # A manual panel build (not autoscale) does NOT defer.
    rid2 = await svc.enqueue("police", requester_name="Staffer",
                             discord_user_id=5, channel_id=5)
    row2 = await svc.run_one(rid2)
    assert row2["status"] == "done"


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
