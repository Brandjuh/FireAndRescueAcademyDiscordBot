"""Daily worldwide auto-build: Overpass query/parse, /api/buildings dedup,
and the funds-gated once-a-day build flow (all network mocked)."""

import random
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.geo.geocoder import GeocodeResult
from fra_bot.geo.overpass import (
    build_candidate_query,
    candidate_type_from_tags,
    parse_candidates,
)
from fra_bot.mc.buildings_api import (
    ExistingBuilding,
    haversine_meters,
    nearest_duplicate,
    parse_api_buildings,
)


# -- Overpass query + parse -------------------------------------------------

def test_query_covers_both_types_and_validates():
    q = build_candidate_query(40.0, -74.2, 40.4, -73.8, "both")
    assert 'nwr["amenity"="hospital"]' in q
    assert 'nwr["healthcare"="hospital"]' in q
    assert 'nwr["amenity"="prison"]' in q
    assert "out center tags;" in q
    # A hospital-only query omits the prison clause.
    assert 'prison' not in build_candidate_query(40.0, -74.2, 40.4, -73.8, "hospital")
    with pytest.raises(ValueError):
        build_candidate_query(40.4, -74.2, 40.0, -73.8)  # south >= north


def test_candidate_type_from_tags():
    assert candidate_type_from_tags({"amenity": "hospital"}) == "hospital"
    assert candidate_type_from_tags({"healthcare": "hospital"}) == "hospital"
    assert candidate_type_from_tags({"amenity": "prison"}) == "prison"
    assert candidate_type_from_tags({"amenity": "school"}) is None


def test_parse_candidates_filters_and_reads_center():
    data = {"elements": [
        {"type": "node", "id": 1, "lat": 40.1, "lon": -74.1,
         "tags": {"amenity": "hospital", "name": "City Hospital"}},
        {"type": "way", "id": 2, "center": {"lat": 40.2, "lon": -74.2},
         "tags": {"amenity": "prison", "name": "State Prison"}},
        {"type": "node", "id": 3, "lat": 40.3, "lon": -74.3,
         "tags": {"amenity": "hospital", "name": "Old Hospital",
                  "disused:amenity": "hospital"}},          # disused -> dropped
        {"type": "node", "id": 4, "lat": 40.4, "lon": -74.4,
         "tags": {"amenity": "hospital"}},                  # no name -> dropped
        {"type": "node", "id": 5, "tags": {"amenity": "prison", "name": "No Coords"}},
    ]}
    hospitals = parse_candidates(data, want="hospital")
    assert [c.name for c in hospitals] == ["City Hospital"]
    prisons = parse_candidates(data, want="prison")
    assert [c.name for c in prisons] == ["State Prison"]
    assert prisons[0].latitude == 40.2 and prisons[0].longitude == -74.2


# -- /api/buildings dedup ---------------------------------------------------

def test_parse_api_buildings_handles_list_and_wrapper_and_aliases():
    rows = parse_api_buildings('[{"id": 5, "building_type": 2, "latitude": "40.1", "lng": "-74.1"}]')
    assert rows == [ExistingBuilding(2, 40.1, -74.1, 5)]
    wrapped = parse_api_buildings({"buildings": [
        {"buildingId": 9, "buildingType": 10, "lat": 1.0, "lon": 2.0},
        {"id": 8, "building_type": 2},  # no coords -> skipped
    ]})
    assert wrapped == [ExistingBuilding(10, 1.0, 2.0, 9)]


def test_haversine_and_nearest_duplicate():
    assert haversine_meters(40.0, -74.0, 40.0, -74.0) == 0
    # ~0.0005 deg latitude is ~55 m — inside 250 m.
    existing = [ExistingBuilding(2, 40.0005, -74.0, 1),   # hospital, close
                ExistingBuilding(10, 40.0, -74.0, 2)]     # prison, same spot
    # A hospital candidate right here IS a duplicate (same-type within 250 m).
    assert nearest_duplicate(40.0, -74.0, "hospital", existing, radius_m=250) is not None
    # A prison candidate 5 km away is NOT (the only prison is far... it's here,
    # so move the candidate away):
    assert nearest_duplicate(40.05, -74.0, "prison", existing, radius_m=250) is None
    # Different type nearby doesn't count.
    assert nearest_duplicate(40.0005, -74.0, "prison", [existing[0]], radius_m=250) is None


# -- daily_build flow -------------------------------------------------------

OVERPASS_DATA = {"elements": [
    {"type": "node", "id": 1, "lat": 40.001, "lon": -74.001,
     "tags": {"amenity": "hospital", "name": "City Hospital"}},
    {"type": "node", "id": 2, "lat": 40.010, "lon": -74.010,
     "tags": {"amenity": "prison", "name": "State Prison"}},
]}


class FakeClient:
    def __init__(self, *, funds, api_json, alliance_json="[]"):
        self._funds_html = f"<div>Alliance Funds: {funds:,} Credits</div>"
        self._api_json = api_json
        self._alliance_json = alliance_json

    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None):
        if path == "/api/buildings":
            return self._api_json
        if path == "/api/alliance_buildings":
            return self._alliance_json
        if path == "/verband/kasse":
            return self._funds_html
        return "<html></html>"


class FakeGeo:
    async def search(self, query):
        return GeocodeResult(40.0, -74.0, f"Center of {query}", "nominatim_search")


class FakeOverpass:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    async def fetch(self, query):
        self.calls += 1
        return self.data


class FakeBuilder:
    def __init__(self):
        self.calls = []

    async def build(self, *, building_type, latitude, longitude, name, address, dry_run=False):
        from fra_bot.mc.browser_builder import BuildResult
        self.calls.append((building_type, name))
        return BuildResult(True, 500 + len(self.calls), "created")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "build.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _svc(db, *, dry_run=True, funds=5_000_000, api_json="[]", alliance_json="[]",
         overpass_data=None, enabled=True, min_funds=2_000_000, seed=0):
    from fra_bot.db.repos import RunsRepo, StateRepo
    from fra_bot.services.buildings import BuildingsService

    svc = BuildingsService.__new__(BuildingsService)
    svc.cfg = SimpleNamespace(
        automation=SimpleNamespace(dry_run=dry_run, reply_to_board=False),
        reports=SimpleNamespace(timezone="UTC"),
    )
    svc._auto = SimpleNamespace(
        daily_build_enabled=enabled, min_alliance_funds=min_funds,
        daily_build_time="03:00", thread_id=15304,
    )
    svc.client = FakeClient(funds=funds, api_json=api_json, alliance_json=alliance_json)
    svc.state = StateRepo(db)
    svc.runs = RunsRepo(db)
    svc._geocoder = FakeGeo()
    svc._overpass = FakeOverpass(OVERPASS_DATA if overpass_data is None else overpass_data)
    svc._builder = FakeBuilder()
    svc._rng = random.Random(seed)
    return svc


async def test_buildings_board_guide_defined_and_posts(db):
    from fra_bot.services.buildings import GUIDE_MARKER, BuildingsService, _building_guide

    assert BuildingsService.guide_marker == GUIDE_MARKER
    text = _building_guide(2_000_000)
    assert text.startswith(GUIDE_MARKER)
    assert "Google Maps" in text and "2,000,000" in text

    class _GuideBoard:
        def __init__(self):
            self.created = []

        async def find_bot_post(self, thread_id, marker, *, max_pages=None):
            return None

        async def create_post_get_id(self, thread_id, content):
            self.created.append((int(thread_id), content))
            return 88

    svc = _svc(db)
    svc.cfg.automation.reply_to_board = True
    svc.board = _GuideBoard()
    line = await svc.force_guide()
    assert line.startswith("✅") and "#88" in line
    assert svc.board.created[0][0] == 15304
    assert svc.board.created[0][1].startswith(GUIDE_MARKER)
    assert "Last updated:" in svc.board.created[0][1]


async def test_daily_build_disabled_is_noop(db):
    svc = _svc(db, enabled=False)
    assert await svc.daily_build() == []


async def test_daily_build_dry_run_reports_both_without_building(db):
    svc = _svc(db, dry_run=True)
    lines = await svc.daily_build()
    assert len(lines) == 2
    assert all(line.startswith("📝") for line in lines)
    assert any("City Hospital" in line for line in lines)
    assert any("State Prison" in line for line in lines)
    assert svc._builder.calls == []           # dry-run never builds


async def test_daily_build_runs_once_per_day(db):
    svc = _svc(db, dry_run=True)
    first = await svc.daily_build()
    assert len(first) == 2
    assert await svc.daily_build() == []       # same day -> guard blocks
    assert await svc.daily_build(force=True)    # force bypasses the guard


async def test_daily_build_skips_when_funds_below_floor(db):
    svc = _svc(db, dry_run=True, funds=1_000_000, min_funds=2_000_000)
    lines = await svc.daily_build()
    assert len(lines) == 2
    assert all("below floor" in line and line.startswith("⏳") for line in lines)


async def test_live_funds_reports_why_it_failed(db):
    from fra_bot.mc.errors import FetchError

    svc = _svc(db)
    svc.client._funds_html = "<div>no numbers anywhere</div>"
    funds, error = await svc._live_funds()
    assert funds is None and "no funds figure" in error

    class _DeadClient:
        async def fetch_page(self, path, *, referer=None):
            raise FetchError(path, 503)

    svc.client = _DeadClient()
    funds, error = await svc._live_funds()
    assert funds is None and "503" in error


async def test_daily_build_summary_carries_funds_failure_reason(db):
    svc = _svc(db, dry_run=True)
    svc.client._funds_html = "<div>layout changed</div>"
    lines = await svc.daily_build()
    assert len(lines) == 2
    assert all("no funds figure" in line for line in lines)


async def test_live_funds_falls_back_to_rendered_page(db, monkeypatch):
    """When the plain kasse HTML has no funds figure (JS-drawn) but
    Playwright is available, the rendered page is parsed instead — the
    reference bot needed the exact same fallback."""
    svc = _svc(db)
    svc.client._funds_html = "<div>drawn by JS</div>"
    svc.cfg.missionchief = SimpleNamespace(base_url="https://www.missionchief.com")

    async def fake_render(base_url, cookies, path, **kwargs):
        return "<div>Alliance Funds</div><div>7,500,000 Credits</div>"

    monkeypatch.setattr(
        "fra_bot.services.buildings.BrowserBuilder.available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr("fra_bot.mc.browser_builder.render_page", fake_render)
    svc._playwright_cookies = lambda: []
    funds, error = await svc._live_funds()
    assert funds == 7_500_000 and error is None


async def test_dry_run_board_request_not_blocked_by_funds_read(db):
    """A dry-run request spends nothing, so it must NOT wait on the funds
    gate: even with an unreadable kasse page the member gets the resolved
    location immediately instead of a silent 'waiting' retry loop."""
    from fra_bot.db.repos import AutomationRepo
    from fra_bot.geo.geocoder import GeocodeResult

    svc = _svc(db, dry_run=True)
    svc.cfg.automation.reply_to_board = True
    svc.client._funds_html = "<div>unreadable</div>"
    svc.requests = AutomationRepo(db)
    replies: list[str] = []

    class _Board:
        async def post_reply(self, thread_id, content):
            replies.append(content)
            return True

    svc.board = _Board()
    rid = await svc.requests.create(
        kind="building", thread_id=15304, post_id=3,
        requester_name="Alice", requester_mc_id=42, payload="{}",
    )
    request = await svc.requests.get(rid)
    location = GeocodeResult(63.42, 10.39, "St Olavs hospital, Trondheim", "url")
    await svc._attempt_build(request, "Alice", "hospital", location, {})

    row = await svc.requests.get(rid)
    assert row["status"] == "skipped"                  # answered, not waiting
    assert "resolved to hospital" in row["status_detail"]
    assert replies and "build it manually" in replies[0]


async def test_daily_build_dedups_against_existing(db):
    # An existing hospital ~55 m from the only hospital candidate blocks it;
    # the prison candidate is unaffected.
    api = '[{"id": 1, "building_type": 2, "latitude": 40.0006, "longitude": -74.001}]'
    svc = _svc(db, dry_run=True, api_json=api)
    lines = await svc.daily_build()
    hospital_line = next(line for line in lines if "hospital" in line)
    prison_line = next(line for line in lines if "prison" in line)
    assert hospital_line.startswith("❔")       # deduped -> no fresh location
    assert prison_line.startswith("📝")          # prison still buildable
    assert svc._overpass.calls >= 1 + 6          # prison once + hospital retried 6x


async def test_daily_build_dedups_against_alliance_buildings(db):
    # The daily build creates ALLIANCE buildings — a previous day's build only
    # shows up on /api/alliance_buildings, and must still block a repeat.
    alliance = '[{"id": 9, "building_type": 2, "latitude": 40.0006, "longitude": -74.001}]'
    svc = _svc(db, dry_run=True, api_json="[]", alliance_json=alliance)
    lines = await svc.daily_build()
    hospital_line = next(line for line in lines if "hospital" in line)
    assert hospital_line.startswith("❔")       # blocked by the alliance building


async def test_daily_build_live_builds_both(db, monkeypatch):
    from fra_bot.services import buildings as buildings_mod

    monkeypatch.setattr(buildings_mod.BrowserBuilder, "available", staticmethod(lambda: True))
    svc = _svc(db, dry_run=False)
    lines = await svc.daily_build()
    assert len(lines) == 2
    assert all(line.startswith("✅") for line in lines)
    assert {c[0] for c in svc._builder.calls} == {"hospital", "prison"}


# -- post-submit API confirmation (alliance builds reveal no id) -------------

def _hospital_row(bid, lat, lng):
    return {"id": bid, "building_type": 2, "latitude": lat, "longitude": lng}


async def test_confirm_build_finds_the_new_building(db, monkeypatch):
    import json as _json

    monkeypatch.setattr("fra_bot.services.buildings.CONFIRM_RETRY_SECONDS", 0)
    svc = _svc(db, alliance_json=_json.dumps([_hospital_row(9, 40.0001, -74.0001)]))
    before = await svc._same_type_near("hospital", 40.0, -74.0)
    assert set(before) == {9}
    # The build lands: a NEW row appears next to the pre-existing one.
    svc.client._alliance_json = _json.dumps(
        [_hospital_row(9, 40.0001, -74.0001), _hospital_row(12, 40.0002, -74.0002)]
    )
    result = await svc._confirm_build("hospital", 40.0, -74.0, before)
    assert result.ok and result.building_id == 12


async def test_confirm_build_reports_refusal_when_nothing_appears(db, monkeypatch):
    import json as _json

    monkeypatch.setattr("fra_bot.services.buildings.CONFIRM_RETRY_SECONDS", 0)
    pre = _json.dumps([_hospital_row(9, 40.0001, -74.0001)])
    svc = _svc(db, alliance_json=pre)
    before = await svc._same_type_near("hospital", 40.0, -74.0)
    result = await svc._confirm_build("hospital", 40.0, -74.0, before)
    assert not result.ok and result.building_id is None
    assert "refused" in result.detail and "permissions" in result.detail


async def test_board_build_submit_without_id_is_confirmed_via_api(db, monkeypatch):
    """The real alliance-build flow: the browser submit yields no id; the
    request must only be reported BUILT once the API shows the new
    building, with the link in the board reply."""
    import json as _json

    from fra_bot.db.repos import AutomationRepo
    from fra_bot.geo.geocoder import GeocodeResult
    from fra_bot.mc.browser_builder import BuildResult

    monkeypatch.setattr("fra_bot.services.buildings.CONFIRM_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        "fra_bot.services.buildings.BrowserBuilder.available",
        staticmethod(lambda: True),
    )
    svc = _svc(db, dry_run=False)
    svc.cfg.automation.reply_to_board = True
    svc.requests = AutomationRepo(db)
    replies: list[str] = []

    class _Board:
        async def post_reply(self, thread_id, content):
            replies.append(content)
            return True

    svc.board = _Board()

    class _SubmitOnlyBuilder:
        async def build(self, *, building_type, latitude, longitude, name,
                        address, dry_run=False):
            # Side-effect of the real build: the game now lists the building.
            svc.client._alliance_json = _json.dumps(
                [_hospital_row(77, latitude, longitude)]
            )
            return BuildResult(True, None, "submitted (HTTP 200)")

    svc._builder = _SubmitOnlyBuilder()
    rid = await svc.requests.create(
        kind="building", thread_id=15304, post_id=4,
        requester_name="Alice", requester_mc_id=42, payload="{}",
    )
    request = await svc.requests.get(rid)
    location = GeocodeResult(40.0, -74.0, "St Olavs hospital, Trondheim", "url")
    await svc._attempt_build(request, "Alice", "hospital", location, {})

    row = await svc.requests.get(rid)
    assert row["status"] == "done"
    assert "built hospital #77" in row["status_detail"]
    assert replies and "buildings/77" in replies[0]


async def test_board_build_refusal_is_reported_not_claimed_built(db, monkeypatch):
    """When the game silently refuses (nothing appears on the APIs), the
    member must NOT be told it was built, and the failure must carry the
    admin-action flag."""
    from fra_bot.db.repos import AutomationRepo
    from fra_bot.geo.geocoder import GeocodeResult
    from fra_bot.mc.browser_builder import BuildResult

    monkeypatch.setattr("fra_bot.services.buildings.CONFIRM_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        "fra_bot.services.buildings.BrowserBuilder.available",
        staticmethod(lambda: True),
    )
    svc = _svc(db, dry_run=False)
    svc.cfg.automation.reply_to_board = True
    svc.requests = AutomationRepo(db)
    replies: list[str] = []

    class _Board:
        async def post_reply(self, thread_id, content):
            replies.append(content)
            return True

    svc.board = _Board()

    class _SubmitOnlyBuilder:
        async def build(self, **kwargs):
            return BuildResult(True, None, "submitted (HTTP 200)")

    svc._builder = _SubmitOnlyBuilder()
    rid = await svc.requests.create(
        kind="building", thread_id=15304, post_id=5,
        requester_name="Alice", requester_mc_id=42, payload="{}",
    )
    request = await svc.requests.get(rid)
    location = GeocodeResult(40.0, -74.0, "St Olavs hospital", "url")
    await svc._attempt_build(request, "Alice", "hospital", location, {})

    row = await svc.requests.get(rid)
    assert row["status"] == "failed"
    assert "ADMIN ACTION NEEDED" in row["status_detail"]
    assert replies and "built" not in replies[0].split("couldn't build")[0]
    assert "admin has been notified" in replies[0].lower()
