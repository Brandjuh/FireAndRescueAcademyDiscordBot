"""Per-member building-request quota and the Overpass type fallback.

The quota: at most REQUEST_LIMIT_PER_MEMBER non-refused building requests
per person per trailing 24h window, matched across intake channels (MC id,
Discord id, name). The fallback: a pin whose reverse geocode is a bare
street address (no place name/OSM type) is classified by the active OSM
hospital/prison within ~200 m instead of being refused.
"""

import datetime as dt
import json
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo
from fra_bot.geo.geocoder import GeocodeResult
from fra_bot.geo.overpass import OverpassError
from fra_bot.services.buildings import (
    REQUEST_LIMIT_PER_MEMBER,
    BuildingsService,
    requests_in_window,
    resolve_building_type,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "rules.sqlite3")
    await database.connect()
    yield database
    await database.close()


class FakeOverpass:
    def __init__(self, data=None, error=None):
        self.data = data if data is not None else {"elements": []}
        self.error = error
        self.queries = []

    async def fetch(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.data


class FakeGeocoder:
    def __init__(self, result=None):
        self.result = result

    async def resolve_maps_link(self, link):
        return self.result


def _loc(address=None, place_text=None, place_type=None,
         lat=42.96, lng=-85.67):
    return GeocodeResult(
        latitude=lat, longitude=lng, address=address, source="test",
        place_text=place_text, place_type=place_type,
    )


HOSPITAL_NEARBY = {"elements": [
    {"type": "way", "id": 1, "center": {"lat": 42.9601, "lon": -85.6701},
     "tags": {"healthcare": "hospital",
              "name": "Pine Rest Mental Health Hospital"}},
]}
BOTH_NEARBY = {"elements": [
    HOSPITAL_NEARBY["elements"][0],
    {"type": "node", "id": 2, "lat": 42.9602, "lon": -85.6702,
     "tags": {"amenity": "prison", "name": "County Jail"}},
]}


def _cfg():
    return SimpleNamespace(
        missionchief=SimpleNamespace(base_url="https://example.test",
                                     alliance_id=1),
        reports=SimpleNamespace(timezone="UTC"),
        automation=SimpleNamespace(
            dry_run=True,
            reply_to_board=False,
            building=SimpleNamespace(
                enabled=True, thread_id=777, min_alliance_funds=2_000_000,
                min_contribution_rate=0.0, set_tax_percent=10,
            ),
        ),
    )


def _service(db, geocoder_result=None, overpass=None):
    svc = BuildingsService(
        _cfg(), SimpleNamespace(), db, FakeGeocoder(geocoder_result)
    )
    svc._overpass = overpass or FakeOverpass()
    svc.replies = []

    async def _capture(request, content):
        svc.replies.append(content)

    svc.reply_for = _capture
    return svc


# ---------------------------------------------------------------------------
# The resolver: detect + Overpass proximity fallback
# ---------------------------------------------------------------------------

async def test_resolver_skips_overpass_when_signals_decide():
    overpass = FakeOverpass()
    loc = _loc(address="St Mary Hospital, 200 Jefferson Ave",
               place_text="St Mary Hospital", place_type="hospital")
    assert await resolve_building_type(loc, overpass) == "hospital"
    assert overpass.queries == []


async def test_resolver_falls_back_to_osm_for_bare_address():
    # The mental-health/children's-hospital case: reverse geocoding gives
    # only a street address, both classification signals are empty.
    overpass = FakeOverpass(HOSPITAL_NEARBY)
    loc = _loc(address="200 Jefferson Ave, Grand Rapids")
    assert await resolve_building_type(loc, overpass) == "hospital"
    assert len(overpass.queries) == 1
    assert 'nwr["healthcare"="hospital"]' in overpass.queries[0]


async def test_resolver_veto_terms_block_the_fallback():
    # A pin explicitly named a clinic (or an inactive museum) must stay
    # refused even when a real hospital sits within the fallback radius.
    overpass = FakeOverpass(HOSPITAL_NEARBY)
    assert await resolve_building_type(
        _loc(address="Corner Clinic, 1 Main St"), overpass
    ) is None
    assert await resolve_building_type(
        _loc(place_text="Old Hospital Museum", address="1 Main St"), overpass
    ) is None
    assert overpass.queries == []


async def test_resolver_ambiguous_or_empty_osm_refuses():
    assert await resolve_building_type(
        _loc(address="1 Main St"), FakeOverpass(BOTH_NEARBY)
    ) is None
    assert await resolve_building_type(
        _loc(address="1 Main St"), FakeOverpass()
    ) is None


async def test_resolver_survives_overpass_outage():
    overpass = FakeOverpass(error=OverpassError("HTTP 504"))
    assert await resolve_building_type(_loc(address="1 Main St"), overpass) is None


# ---------------------------------------------------------------------------
# The quota counter
# ---------------------------------------------------------------------------

async def test_count_matches_identity_and_skips_refused_rows(db):
    repo = AutomationRepo(db)
    # A board row (MC id) and a Discord row (payload id, name in another
    # case) both count for the same person.
    await repo.create(kind="building", thread_id=777, post_id=1,
                      requester_name="Alice", requester_mc_id=42)
    await repo.create(kind="building", thread_id=0, post_id=2,
                      requester_name="alice", requester_mc_id=None,
                      payload=json.dumps({"discord_user_id": 100}))
    # Refused/rejected rows never consume quota.
    await repo.create(kind="building", thread_id=777, post_id=3,
                      requester_name="Alice", requester_mc_id=42,
                      status="skipped",
                      status_detail="refused: location is not a hospital or prison")
    await repo.create(kind="building", thread_id=0, post_id=4,
                      requester_name="Alice", requester_mc_id=42,
                      payload=json.dumps({"intake_rejected": True}),
                      status="skipped",
                      status_detail="rejected at intake: not a Google Maps link")
    # Someone else's row and another kind don't count.
    await repo.create(kind="building", thread_id=777, post_id=5,
                      requester_name="Bob", requester_mc_id=7)
    await repo.create(kind="training", thread_id=777, post_id=6,
                      requester_name="Alice", requester_mc_id=42)

    assert await requests_in_window(
        repo, requester_mc_id=42, discord_user_id=100, requester_name="ALICE",
    ) == 2
    assert await requests_in_window(repo, requester_name="ALICE") == 2
    assert await requests_in_window(repo) == 0  # no identifiers, no match


async def test_count_respects_window_and_before_id(db):
    repo = AutomationRepo(db)
    ids = [
        await repo.create(kind="building", thread_id=777, post_id=10 + n,
                          requester_name="Alice", requester_mc_id=42)
        for n in range(3)
    ]
    aged = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    ).isoformat(timespec="seconds")
    await db.execute(
        "UPDATE automation_requests SET created_at = ? WHERE id = ?",
        (aged, ids[0]),
    )
    assert await requests_in_window(repo, requester_mc_id=42) == 2
    assert await requests_in_window(
        repo, requester_mc_id=42, before_id=ids[2],
    ) == 1


# ---------------------------------------------------------------------------
# The executor: board rows are quota-gated, Discord/web rows are not
# ---------------------------------------------------------------------------

async def _board_row(repo, *, post_id, name="Alice", mc_id=42,
                     status="done", thread_id=777):
    return await repo.create(
        kind="building", thread_id=thread_id, post_id=post_id,
        requester_name=name, requester_mc_id=mc_id,
        payload=json.dumps({"link": "https://maps.app.goo.gl/x"}),
        status=status,
    )


async def test_executor_refuses_fifth_board_request(db):
    repo = AutomationRepo(db)
    for n in range(REQUEST_LIMIT_PER_MEMBER):
        await _board_row(repo, post_id=n + 1)
    rid = await _board_row(repo, post_id=99, status="pending")
    svc = _service(db)  # the geocoder must never be reached

    await svc.execute_request(await repo.get(rid), announce=True)

    row = await repo.get(rid)
    assert row["status"] == "skipped"
    assert "request limit reached (4" in row["status_detail"]
    assert f"limit is {REQUEST_LIMIT_PER_MEMBER} per member" in svc.replies[0]


async def test_executor_allows_board_request_under_limit(db):
    repo = AutomationRepo(db)
    for n in range(REQUEST_LIMIT_PER_MEMBER - 1):
        await _board_row(repo, post_id=n + 1)
    rid = await _board_row(repo, post_id=99, status="pending")
    svc = _service(db, geocoder_result=_loc(
        address="St Mary Hospital, 200 Jefferson Ave",
        place_text="St Mary Hospital", place_type="hospital",
    ))

    await svc.execute_request(await repo.get(rid), announce=True)

    row = await repo.get(rid)
    assert row["status"] == "skipped"  # dry-run resolves and records
    assert "resolved to hospital" in row["status_detail"]


async def test_executor_exempts_discord_and_web_rows(db):
    # thread_id 0 marks Discord/web-console rows: Discord was gated at
    # intake, the console operator is exempt — the executor must not
    # re-apply the quota to either.
    repo = AutomationRepo(db)
    for n in range(REQUEST_LIMIT_PER_MEMBER):
        await _board_row(repo, post_id=n + 1, name="Web console",
                         mc_id=None, thread_id=0)
    rid = await _board_row(repo, post_id=0, name="Web console",
                           mc_id=None, status="pending", thread_id=0)
    svc = _service(db, geocoder_result=_loc(
        address="St Mary Hospital, 200 Jefferson Ave",
        place_text="St Mary Hospital", place_type="hospital",
    ))

    await svc.execute_request(await repo.get(rid), announce=True)

    row = await repo.get(rid)
    assert row["status"] == "skipped"
    assert "resolved to hospital" in row["status_detail"]


async def test_executor_resolves_bare_address_pin_via_overpass(db):
    repo = AutomationRepo(db)
    rid = await _board_row(repo, post_id=1, status="pending")
    svc = _service(
        db,
        geocoder_result=_loc(address="200 Jefferson Ave, Grand Rapids"),
        overpass=FakeOverpass(HOSPITAL_NEARBY),
    )

    await svc.execute_request(await repo.get(rid), announce=True)

    row = await repo.get(rid)
    assert row["status"] == "skipped"
    assert "resolved to hospital" in row["status_detail"]
    assert json.loads(row["payload"])["building_type"] == "hospital"


async def test_executor_still_refuses_when_osm_is_empty(db):
    repo = AutomationRepo(db)
    rid = await _board_row(repo, post_id=1, status="pending")
    svc = _service(
        db,
        geocoder_result=_loc(address="200 Jefferson Ave, Grand Rapids"),
        overpass=FakeOverpass(),
    )

    await svc.execute_request(await repo.get(rid), announce=True)

    row = await repo.get(rid)
    assert row["status"] == "skipped"
    assert "refused: location is not a hospital or prison" in row["status_detail"]
