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

import pytest
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


def _cfg(min_contribution_rate=0.0):
    return SimpleNamespace(
        missionchief=SimpleNamespace(base_url="https://example.test",
                                     alliance_id=1),
        reports=SimpleNamespace(timezone="UTC"),
        automation=SimpleNamespace(
            dry_run=True,
            reply_to_board=False,
            building=SimpleNamespace(
                enabled=True, thread_id=777, min_alliance_funds=2_000_000,
                min_contribution_rate=min_contribution_rate,
                set_tax_percent=10,
            ),
        ),
    )


async def _seed_roster(db, mc_id, name, rate):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, '2026-01-01', "
        "'2026-07-01')",
        (mc_id, name, rate),
    )


def _fund_the_service(svc, funds=99_000_000):
    """Stub the live funds read — _auto_build_one checks the treasury
    before it ever looks for a location."""
    async def _funds():
        return funds, None

    svc._live_funds = _funds
    return svc


async def _fake_search(query):
    """A geocoder that always resolves a city, so a test can isolate the
    Overpass side of the daily-build search."""
    return _loc(address=str(query), lat=42.96, lng=-85.67)


def _service(db, geocoder_result=None, overpass=None,
             min_contribution_rate=0.0):
    svc = BuildingsService(
        _cfg(min_contribution_rate), SimpleNamespace(), db,
        FakeGeocoder(geocoder_result),
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


async def test_board_building_gate_fails_closed(db):
    # Board building posts never had a contribution gate at all; now they
    # get the same fail-closed one as trainings/events/missions: an empty
    # rate column on the roster is 0% (refused), an unknown requester
    # waits for the next members sync, a healthy rate passes.
    repo = AutomationRepo(db)
    await _seed_roster(db, 42, "Alice", None)      # never set a donation
    await _seed_roster(db, 43, "Saint", 10.0)
    svc = _service(
        db,
        geocoder_result=_loc(
            address="St Mary Hospital, 200 Jefferson Ave",
            place_text="St Mary Hospital", place_type="hospital",
        ),
        min_contribution_rate=5.0,
    )

    rid = await _board_row(repo, post_id=1, name="Alice", mc_id=42,
                           status="pending")
    await svc.execute_request(await repo.get(rid), announce=True)
    row = await repo.get(rid)
    assert row["status"] == "skipped"
    assert "contribution rate 0% is below the required 5%" in row["status_detail"]
    assert any("minimum required for building requests" in r
               for r in svc.replies)
    # …and where to change it, so the member isn't left asking an admin.
    assert any("How to update your alliance donation" in r
               and "Alliance Funds" in r for r in svc.replies)

    rid = await _board_row(repo, post_id=2, name="Stranger", mc_id=777,
                           status="pending")
    await svc.execute_request(await repo.get(rid), announce=True)
    row = await repo.get(rid)
    assert row["status"] == "waiting"
    assert "not on the stored roster" in row["status_detail"]

    rid = await _board_row(repo, post_id=3, name="Saint", mc_id=43,
                           status="pending")
    await svc.execute_request(await repo.get(rid), announce=True)
    row = await repo.get(rid)
    assert row["status"] == "skipped"              # dry-run resolves
    assert "resolved to hospital" in row["status_detail"]


async def test_web_rows_stay_exempt_from_the_building_gate(db):
    repo = AutomationRepo(db)
    svc = _service(
        db,
        geocoder_result=_loc(
            address="St Mary Hospital, 200 Jefferson Ave",
            place_text="St Mary Hospital", place_type="hospital",
        ),
        min_contribution_rate=5.0,
    )
    rid = await _board_row(repo, post_id=0, name="Web console", mc_id=None,
                           status="pending", thread_id=0)
    await svc.execute_request(await repo.get(rid), announce=True)
    row = await repo.get(rid)
    assert row["status"] == "skipped"
    assert "resolved to hospital" in row["status_detail"]


async def test_building_guide_documents_the_contribution_minimum(db):
    svc = _service(db, min_contribution_rate=5.0)
    assert "contribution of at least 5%" in svc.guide_body()
    # With the gate off the guide doesn't promise one.
    assert "contribution" not in _service(db).guide_body()


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


# ---------------------------------------------------------------------------
# Admin override on the board + the finisher's funds handling
# ---------------------------------------------------------------------------

def test_force_keyword_needs_both_a_force_word_and_one_type():
    from fra_bot.services.buildings import forced_type_from_post as forced

    assert forced("Prison: https://maps.app.goo.gl/x force") == "prison"
    assert forced("FORCE Hospital: https://maps.app.goo.gl/x") == "hospital"
    assert forced("forceer jail https://x") == "prison"
    # No force word, an ordinary request — verified as usual.
    assert forced("Prison: https://maps.app.goo.gl/x") is None
    # Both types named: nothing unambiguous to force.
    assert forced("force hospital and prison") is None
    # The word must stand alone, never inside another word.
    assert forced("Reinforcement near the prison https://x") is None


async def _override_service(db, *, admin: bool):
    svc = _service(db, geocoder_result=_loc(address="1 Nowhere Rd"))

    async def _is_admin(mc_user_id):
        return admin

    svc.is_admin_mc_id = _is_admin
    return svc


async def test_board_override_builds_the_named_type_for_an_admin(db):
    repo = AutomationRepo(db)
    svc = await _override_service(db, admin=True)
    rid = await _board_row(repo, post_id=1, status="pending")
    await repo.set_status(rid, "pending", payload=json.dumps(
        {"link": "https://maps.app.goo.gl/x", "forced_type": "prison"}
    ))
    row = await repo.get(rid)
    assert await svc._forced_type_allowed(row) == "prison"


async def test_board_override_is_ignored_for_a_non_admin(db):
    repo = AutomationRepo(db)
    svc = await _override_service(db, admin=False)
    rid = await _board_row(repo, post_id=1, status="pending")
    await repo.set_status(rid, "pending", payload=json.dumps(
        {"link": "https://maps.app.goo.gl/x", "forced_type": "prison"}
    ))
    assert await svc._forced_type_allowed(await repo.get(rid)) is None


async def test_override_absent_without_the_keyword(db):
    repo = AutomationRepo(db)
    svc = await _override_service(db, admin=True)
    rid = await _board_row(repo, post_id=1, status="pending")
    assert await svc._forced_type_allowed(await repo.get(rid)) is None


async def test_finisher_keeps_a_building_while_funds_are_blocked(db):
    """The live bug: 12h of alliance funds under the floor retired a
    half-built hospital as "complete", and nothing ever finished it."""
    from fra_bot.services.buildings import (
        COMPLETION_IDLE_LIMIT,
        PENDING_COMPLETION_KEY,
    )
    from fra_bot.services.building_upgrade import UpgradeReport

    svc = _service(db)
    svc.cfg.automation.dry_run = False
    svc._auto = svc.cfg.automation.building

    class _Upgrader:
        def __init__(self, blocked):
            self.blocked = blocked

        async def upgrade_one(self, building_id, *, kind, name):
            report = UpgradeReport(mode="LIVE")
            report.funds_blocked = self.blocked
            return report

    await svc.state.set(PENDING_COMPLETION_KEY, json.dumps({
        "555": {"kind": "hospital", "name": "St Mary", "tax_done": True,
                "idle": COMPLETION_IDLE_LIMIT - 1},
    }))

    svc._upgrader = _Upgrader(blocked=True)
    for _ in range(3):                      # would have retired it 3x over
        await svc.finish_pending()
    still = json.loads(await svc.state.get(PENDING_COMPLETION_KEY))
    assert "555" in still, "a funds-blocked pass must not retire the building"
    assert still["555"]["idle"] == COMPLETION_IDLE_LIMIT - 1   # untouched

    # Funds recover and there is genuinely nothing left to buy — now it retires.
    svc._upgrader = _Upgrader(blocked=False)
    await svc.finish_pending()
    assert json.loads(await svc.state.get(PENDING_COMPLETION_KEY)) == {}


# ---------------------------------------------------------------------------
# The daily auto-build says WHY it found nothing
# ---------------------------------------------------------------------------

def test_explain_ranks_the_failure_reasons():
    from fra_bot.services.buildings import BuildingsService

    assert BuildingsService._explain({}) == "no attempts were made"
    assert BuildingsService._explain({"Overpass unavailable (504)": 4,
                                      "geocode failed (quota)": 2}) == (
        "4x Overpass unavailable (504), 2x geocode failed (quota)"
    )


async def test_auto_build_stops_walking_cities_when_overpass_is_down(db):
    """An outage is not a per-city problem: retrying all six cities just
    repeats it while holding the daily-build job lock — and with silently
    dropped packets each attempt costs a full timeout."""
    from fra_bot.services.buildings import MAX_OVERPASS_FAILURES

    overpass = FakeOverpass(error=OverpassError("Cannot connect to host"))
    svc = _fund_the_service(_service(db, overpass=overpass))
    svc._geocoder.search = _fake_search
    await svc._auto_build_one("hospital", [])
    assert len(overpass.queries) == MAX_OVERPASS_FAILURES


async def test_auto_build_reports_an_overpass_outage_as_such(db):
    """The live report said "all nearby ones already built, or Overpass
    unavailable" for both types — one line covering three unrelated
    causes. Each must now name itself."""
    svc = _fund_the_service(
        _service(db, overpass=FakeOverpass(error=OverpassError("HTTP 504")))
    )
    svc._geocoder.search = _fake_search
    line = await svc._auto_build_one("hospital", [])
    assert "no location found" in line
    assert "Overpass unavailable" in line
    assert "HTTP 504" in line


async def test_auto_build_reports_a_geocoder_outage_as_such(db):
    from fra_bot.geo.geocoder import GeocodeError

    svc = _fund_the_service(_service(db, overpass=FakeOverpass()))

    async def _boom(query):
        raise GeocodeError("quota exceeded")

    svc._geocoder.search = _boom
    line = await svc._auto_build_one("prison", [])
    assert "geocode failed" in line and "quota exceeded" in line
    assert "Overpass" not in line          # never blamed the wrong layer


async def test_auto_build_distinguishes_empty_osm_from_already_built(db):
    svc = _fund_the_service(_service(db, overpass=FakeOverpass()))  # zero elements
    svc._geocoder.search = _fake_search
    assert "none in OSM near the city" in await svc._auto_build_one("prison", [])


# ---------------------------------------------------------------------------
# Overpass endpoint failover
# ---------------------------------------------------------------------------

async def test_overpass_falls_over_to_the_next_mirror():
    """One unreachable host must not disable the whole feature — that is
    exactly what took out the daily build ("Cannot connect to host
    overpass-api.de:443")."""
    import aiohttp

    from fra_bot.geo.overpass import OverpassClient

    client = OverpassClient(urls=["https://down.example/api",
                                  "https://up.example/api"])
    tried: list[str] = []

    async def _fetch_one(session, url, query):
        tried.append(url)
        if "down" in url:
            raise aiohttp.ClientError("Cannot connect to host down.example:443")
        return {"elements": [{"type": "node", "id": 1, "lat": 1.0, "lon": 2.0,
                              "tags": {"amenity": "prison", "name": "X"}}]}

    client._fetch_one = _fetch_one
    data = await client.fetch("[out:json];")
    assert [u.split("/")[2] for u in tried] == ["down.example", "up.example"]
    assert data["elements"][0]["tags"]["name"] == "X"


async def test_overpass_names_every_endpoint_that_failed():
    import aiohttp

    from fra_bot.geo.overpass import OverpassClient, OverpassError

    client = OverpassClient(urls=["https://a.example/api", "https://b.example/api"])

    async def _fetch_one(session, url, query):
        raise aiohttp.ClientError("boom")

    client._fetch_one = _fetch_one
    with pytest.raises(OverpassError) as excinfo:
        await client.fetch("[out:json];")
    assert "a.example" in str(excinfo.value) and "b.example" in str(excinfo.value)


async def test_overpass_timeout_becomes_an_overpass_error():
    # asyncio.TimeoutError is not an aiohttp.ClientError, so it used to
    # escape OverpassError and surface as an unhandled exception.
    import asyncio as _asyncio

    from fra_bot.geo.overpass import OverpassClient, OverpassError

    client = OverpassClient(urls=["https://slow.example/api"])

    async def _fetch_one(session, url, query):
        raise _asyncio.TimeoutError()

    client._fetch_one = _fetch_one
    with pytest.raises(OverpassError):
        await client.fetch("[out:json];")


def test_query_server_timeout_fits_under_the_client_timeout():
    from fra_bot.geo.overpass import build_candidate_query

    assert "[out:json][timeout:60]" in build_candidate_query(1, 1, 2, 2)


async def test_overpass_client_bounds_the_tcp_handshake():
    """A host whose packets are silently dropped must fail on the connect
    timeout, not burn the whole request budget before the next mirror."""
    import aiohttp

    from fra_bot.geo.overpass import CONNECT_TIMEOUT, OverpassClient

    seen = {}
    real_session = aiohttp.ClientSession

    class _Spy(real_session):
        def __init__(self, *args, timeout=None, **kwargs):
            seen["timeout"] = timeout
            super().__init__(*args, timeout=timeout, **kwargs)

    aiohttp.ClientSession = _Spy
    try:
        client = OverpassClient(urls=["https://x.example/api"])

        async def _fetch_one(session, url, query):
            raise aiohttp.ClientError("nope")

        client._fetch_one = _fetch_one
        with pytest.raises(Exception):
            await client.fetch("[out:json];")
    finally:
        aiohttp.ClientSession = real_session
    assert seen["timeout"].sock_connect == CONNECT_TIMEOUT
    assert seen["timeout"].total >= CONNECT_TIMEOUT
