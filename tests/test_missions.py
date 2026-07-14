"""The unified mission/event system: the board spec parser, the queue +
rotation repos, and the scheduler's member-first / rotation / next-up logic."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import MissionsRepo, RotationRepo
from fra_bot.geo.geocoder import GeocodeResult
from fra_bot.mc.parsers.missions_custom import value_field_name
from fra_bot.mc.parsers.mission_spec import (
    MissionSpec,
    MissionSpecError,
    is_mission_post,
    parse_board_request,
    parse_mission_spec,
)
from fra_bot.services.missions import MissionScheduler


# --------------------------------------------------------------------------
# Dedicated-board intake: a bare location is the request (no trigger word)
# --------------------------------------------------------------------------

def test_board_request_bare_location_per_kind():
    m = parse_board_request("New York City", default_kind="large")
    assert m.kind == "large" and m.source == "preset" and m.location_text == "New York City"
    e = parse_board_request("New York City", default_kind="event")
    # event board defaults: random type, large / circle / 30s
    assert e.kind == "event" and e.event_random
    assert (e.area, e.shape, e.call_volume) == ("large", "circle", "30")


def test_board_request_refinements_and_label():
    e = parse_board_request("Amsterdam\nevent: Storm\narea: small\ncall: 45", default_kind="event")
    assert e.event_type_id == 0 and e.area == "small" and e.call_volume == "45"
    m = parse_board_request(
        "Berlin\ncustom: need_lf=25 need_elw1=6\nname: Big fire\nrecurring", default_kind="large"
    )
    assert m.source == "custom" and m.recurring and m.custom.values["need_lf"] == 25
    assert parse_board_request("Location: Grand Rapids", default_kind="large").location_text == "Grand Rapids"


def test_board_request_ignores_bot_and_empty():
    assert parse_board_request("[FRA] got it — …", default_kind="large") is None
    assert parse_board_request("   ", default_kind="event") is None


def test_board_request_clarifies_bad_field():
    with pytest.raises(MissionSpecError):
        parse_board_request("NYC\nevent: Volcano", default_kind="event")


# --------------------------------------------------------------------------
# Board spec parsing (unified model)
# --------------------------------------------------------------------------

def test_parse_preset_large_post():
    spec = parse_mission_spec("large scale mission: 350 5th Ave, New York")
    assert spec is not None
    assert spec.kind == "large" and spec.source == "preset"
    assert spec.location_text == "350 5th Ave, New York"
    assert not spec.recurring


def test_parse_custom_post():
    spec = parse_mission_spec(
        "own mission: Grand Rapids\nname: Big fire\n"
        "custom: need_lf=25 need_elw1=6 water_needed=15000\nrecurring"
    )
    assert spec.source == "custom" and spec.recurring
    assert spec.custom.caption == "Big fire"
    assert spec.custom.values == {"need_lf": 25, "need_elw1": 6, "water_needed": 15000}


def test_parse_saved_post():
    spec = parse_mission_spec("large scale mission: Amsterdam\nsaved: Wildfire")
    assert spec.source == "saved" and spec.saved_name == "Wildfire"


def test_parse_event_kind_post():
    spec = parse_mission_spec("own mission: NYC\nkind: event")
    assert spec.kind == "event" and spec.source == "preset"


def test_custom_on_event_kind_rejected():
    with pytest.raises(MissionSpecError):
        parse_mission_spec("own mission: NYC\nkind: event\ncustom: need_lf=5")


def test_non_mission_post_returns_none():
    assert parse_mission_spec("location: Amsterdam") is None
    assert parse_mission_spec("thanks everyone!") is None


def test_is_mission_post_ownership():
    assert is_mission_post("own mission: NYC\ncustom: need_lf=5") is True
    assert is_mission_post("large scale mission: https://maps.google.com/?q=1,2") is True
    assert is_mission_post("location: Amsterdam") is False


def test_spec_validation_and_describe():
    spec = MissionSpec(location_text="  NYC  ").validate()
    assert spec.location_text == "NYC" and spec.kind == "large"
    assert "one-time" in spec.describe()
    with pytest.raises(MissionSpecError):
        MissionSpec(location_text="").validate()
    with pytest.raises(MissionSpecError):
        MissionSpec(location_text="x", kind="bogus").validate()
    with pytest.raises(MissionSpecError):
        MissionSpec(location_text="x", source="saved").validate()  # no name


# --------------------------------------------------------------------------
# Repo: queue + rotation
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "missions.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_board_dedup_and_cancel(db):
    repo = MissionsRepo(db)
    fields = {"kind": "large", "mission_source": "preset", "location_text": "x"}
    first = await repo.create_from_board(15293, 500, fields, requester_name="Bob", requester_mc_id=7)
    dup = await repo.create_from_board(15293, 500, fields, requester_name="Bob", requester_mc_id=7)
    assert first is not None and dup is None
    assert await repo.cancel(first) is True
    assert await repo.cancel(first) is False


async def test_sweep_stale_processing_only_touches_old(db):
    import datetime as dt

    repo = MissionsRepo(db)
    mid = await repo.create(source="discord", location_text="x")
    await repo.claim(mid)  # 'processing', updated_at = now
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
    assert await repo.sweep_stale_processing(past) == 0        # just-claimed: untouched
    assert (await repo.get(mid))["status"] == "processing"
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    assert await repo.sweep_stale_processing(future) == 1      # stuck: released
    row = await repo.get(mid)
    assert row["status"] == "failed" and "stale" in row["status_detail"]


async def test_sweep_requeue_in_dry_run(db):
    repo = MissionsRepo(db)
    mid = await repo.create(source="discord", location_text="x")
    await repo.claim(mid)  # -> processing
    # Dry-run re-queue: back to 'pending' to re-process cleanly, no scary note.
    assert await repo.sweep_processing(requeue=True) == 1
    row = await repo.get(mid)
    assert row["status"] == "pending" and row["status_detail"] is None


async def test_delete_mission_and_delete_terminal(db):
    repo = MissionsRepo(db)
    open_id = await repo.create(source="discord", location_text="open")
    done_id = await repo.create(source="discord", location_text="done")
    await repo.set_status(done_id, "done", "started")
    # Hard-delete a specific row (any status).
    assert await repo.delete(open_id) is True
    assert await repo.delete(open_id) is False          # gone
    assert await repo.get(open_id) is None
    # delete_terminal clears finished rows, leaves open ones.
    still_open = await repo.create(source="discord", location_text="still open")
    removed = await repo.delete_terminal()
    assert removed == 1                                  # only the 'done' row
    assert await repo.get(done_id) is None
    assert await repo.get(still_open) is not None


async def test_rotation_cycle_is_fair(db):
    rot = RotationRepo(db)
    a = await rot.add(location_text="A", created_by="admin")
    b = await rot.add(location_text="B", created_by="admin")
    # Never-started, lowest id first.
    assert (await rot.next_entry())["id"] == a
    await rot.mark_started(a, latitude=1.0, longitude=2.0, address="A addr")
    assert (await rot.next_entry())["id"] == b        # a now has a timestamp
    await rot.mark_started(b, latitude=3.0, longitude=4.0, address="B addr")
    assert (await rot.next_entry())["id"] == a        # back to a (older start)


async def test_rotation_pause_and_remove(db):
    rot = RotationRepo(db)
    a = await rot.add(location_text="A", created_by="admin")
    await rot.set_active(a, False)
    assert await rot.next_entry() is None             # paused -> skipped
    assert await rot.active_count() == 0
    await rot.set_active(a, True)
    assert (await rot.next_entry())["id"] == a
    assert await rot.remove(a) is True
    assert await rot.remove(a) is False


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

def _large_form(last_free="Mon, 01 Jul 2019 12:00", *, coins="0", saved=""):
    return f"""
    <html><body>
    Last free mission: {last_free}
    {saved}
    <form action="/missionAllianceCreate" id="new_mission_position" method="post">
      <input type="hidden" name="authenticity_token" value="tok"/>
      <input type="radio" name="mission_position[mission_type_id]" value="41" checked/>
      <input type="text" name="mission_position[mission_custom][caption]" value=""/>
      <input type="number" name="{value_field_name('need_lf')}" value="0"/>
      <input type="number" name="{value_field_name('need_elw1')}" value="0"/>
      <input type="hidden" name="mission_position[latitude]" value=""/>
      <input type="hidden" name="mission_position[longitude]" value=""/>
      <input type="hidden" name="mission_position[coins]" value="{coins}"/>
      <input type="submit" value="Start mission"/>
    </form>
    </body></html>
    """


_SAVED_ANCHOR = (
    '<a class="mission_custom_saved_restore" '
    "params='{\"caption\":\"Wildfire\",\"need_lf\":\"100\",\"need_brush_truck\":\"100\"}'>"
    "Wildfire (Author1)</a>"
)

_ELIGIBLE = _large_form()
_ELIGIBLE_SAVED = _large_form(saved=_SAVED_ANCHOR)
_STARTED = _large_form("Wed, 01 Jan 2025 12:00")           # advanced cooldown
_WAITING = _large_form("Fri, 01 Jan 2100 12:00")           # cooldown in the future
_COIN = _large_form(coins="500")
def _event_form(last_free="Mon, 01 Jul 2019 12:00", *, coins="1"):
    # Faithful to the real /missionAllianceEventNew: data-event-id radios
    # (0-7 standard, 8 = Soccer Game with data-event-tag="football"), no radio
    # checked, coins defaults to 1 ("Start Event (20 Coins)").
    radios = "".join(
        f'<label class="radio"><input type="radio" name="event_radio_group" '
        f'data-event-id="{i}" data-event-tag="{"football" if i == 8 else ""}"> E{i}</label>'
        for i in range(9)
    )
    return f"""
    <html><body>
    <span id="alliance_event_last_free_mission">Last free mission: {last_free}</span>
    <form action="/missionAllianceEventCreate" id="new_mission_position" method="post">
      <input type="hidden" name="authenticity_token" value="tok"/>
      {radios}
      <input type="hidden" name="mission_position[mission_type_id]" value=""/>
      <input type="hidden" name="mission_position[size]" value="1"/>
      <input type="hidden" name="mission_position[shape]" value=""/>
      <input type="hidden" name="mission_position[amount]" value="1"/>
      <input type="hidden" name="mission_position[coins]" value="{coins}"/>
      <input type="submit" value="Start Event ( 20 Coins )"/>
    </form></body></html>
    """


_EVENT = _event_form()
_EVENT_STARTED = _event_form("Wed, 01 Jan 2025 12:00")
_EVENT_WAITING = _event_form("Fri, 01 Jan 2100 12:00")


class FakeClient:
    def __init__(self, large_html=_ELIGIBLE, event_html=_EVENT, *, post_status=200):
        self.large_html = large_html
        self.event_html = event_html
        self.post_status = post_status
        self.posted = False
        self.post_calls = 0
        self.posted_body = None
        self.fetched: list[str] = []

    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None):
        self.fetched.append(path)
        is_event = "Event" in path
        if self.posted:  # verification fetch: show an advanced cooldown
            return _EVENT_STARTED if is_event else _STARTED
        return self.event_html if is_event else self.large_html

    async def post_form(self, path, data, **kwargs):
        self.posted = True
        self.post_calls += 1
        self.posted_body = dict(data)
        return (self.post_status, {}, "")


class FakeGeo:
    def __init__(self):
        self.result = GeocodeResult(40.5, -73.9, "Resolved NYC", "nominatim_search")

    async def search(self, query):
        return self.result

    async def resolve_maps_link(self, url):
        return self.result


def _cfg(*, dry_run=True, min_rate=5.0, events_enabled=False, board_enabled=False):
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=dry_run,
            reply_to_board=True,
            mission=SimpleNamespace(
                enabled=True, board_enabled=board_enabled, thread_id=15307,
                interval=5, panel_channel_id=0, min_contribution_rate=min_rate,
            ),
            events=SimpleNamespace(
                enabled=events_enabled, thread_id=15303, interval=5,
                min_contribution_rate=min_rate,
            ),
        ),
    )


def _scheduler(cfg, client, db):
    return MissionScheduler(cfg, client, db, FakeGeo())


async def _enqueue(sched, **kw):
    kw.setdefault("source", "discord")
    kw.setdefault("kind", "large")
    kw.setdefault("mission_source", "preset")
    kw.setdefault("location_text", "NYC")
    return await sched.missions.create(**kw)


# -- basic start paths ------------------------------------------------------

async def test_dry_run_marks_skipped_and_resolves(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"
    assert row["latitude"] == 40.5
    assert "would start" in row["status_detail"]


async def test_cooldown_queues_as_waiting(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_WAITING), db)
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    assert row["next_attempt_at"] is not None


async def test_coin_form_refused(db):
    sched = _scheduler(_cfg(dry_run=False), FakeClient(_COIN), db)
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "failed"
    assert "coins" in row["status_detail"]


async def test_live_preset_verified_done(db):
    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert client.post_calls == 1
    assert row["status"] == "done"


# -- custom + saved ---------------------------------------------------------

async def test_live_custom_posts_real_fields(db):
    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    await _enqueue(
        sched, mission_source="custom", caption="Big fire",
        custom_values='{"need_lf": 25, "need_elw1": 6}',
    )
    await sched._advance()
    body = client.posted_body
    assert body["mission_position[mission_type_id]"] == "-1"
    assert body["mission_position[mission_custom][caption]"] == "Big fire"
    assert body[value_field_name("need_lf")] == "25"
    assert body[value_field_name("need_elw1")] == "6"
    assert body["mission_position[coins]"] == "0"


async def test_live_saved_resolves_from_dropdown(db):
    client = FakeClient(_ELIGIBLE_SAVED)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    await _enqueue(sched, mission_source="saved", saved_name="Wildfire")
    await sched._advance()
    body = client.posted_body
    assert body["mission_position[mission_type_id]"] == "-1"
    assert body["mission_position[mission_custom][caption]"] == "Wildfire"
    assert body[value_field_name("need_lf")] == "100"
    assert body[value_field_name("need_brush_truck")] == "100"


async def test_saved_missing_from_dropdown_fails(db):
    client = FakeClient(_ELIGIBLE)  # no saved anchors
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await _enqueue(sched, mission_source="saved", saved_name="Nope")
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "failed"
    assert "not found" in row["status_detail"]
    assert client.post_calls == 0  # never submitted


# -- alliance events --------------------------------------------------------

async def test_event_live_posts_type_area_shape_volume_coins0(db):
    client = FakeClient(_ELIGIBLE, _EVENT)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    await _enqueue(
        sched, kind="event", event_type_id=3, area="large", shape="circle",
        call_volume="60",
    )
    await sched._advance()
    assert any("missionAllianceEventNew" in p for p in client.fetched)
    body = client.posted_body
    assert body["mission_position[mission_type_id]"] == "3"     # Fall weather
    assert body["mission_position[size]"] == "2"                # large
    assert body["mission_position[shape]"] == "circle"
    assert body["mission_position[amount]"] == "2"              # 60s
    # The event form defaults coins to 1; we must submit 0 (free weekly only).
    assert body["mission_position[coins]"] == "0"


async def test_event_not_refused_despite_form_coins_default(db):
    # is_free_submit(form) is False for the event form (coins=1), but the
    # event path must NOT refuse on that — it forces coins=0 and is
    # cooldown-gated instead.
    client = FakeClient(_ELIGIBLE, _EVENT)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await _enqueue(sched, kind="event", event_type_id=0)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "done"
    assert client.post_calls == 1


async def test_event_random_picks_standard_type(db):
    client = FakeClient(_ELIGIBLE, _EVENT)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    await _enqueue(sched, kind="event", event_random=1)
    await sched._advance()
    body = client.posted_body
    chosen = int(body["mission_position[mission_type_id]"])
    assert 0 <= chosen <= 7          # a standard type, never 8 (Soccer Game)


async def test_event_cooldown_waits(db):
    client = FakeClient(_ELIGIBLE, _EVENT_WAITING)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    mid = await _enqueue(sched, kind="event", event_type_id=1)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    assert client.post_calls == 0


async def test_event_dry_run_describes_type(db):
    client = FakeClient(_ELIGIBLE, _EVENT)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    mid = await _enqueue(sched, kind="event", event_type_id=2, area="small")
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"
    assert "Storm Surge" in row["status_detail"]


# -- contribution gate ------------------------------------------------------

async def test_board_contribution_gate_skips(db):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (7, 'Bob', 1.0, 1, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    sched = _scheduler(_cfg(dry_run=True, min_rate=5.0), FakeClient(), db)
    mid = await sched.missions.create_from_board(
        15293, 700, {"kind": "large", "mission_source": "preset", "location_text": "NYC"},
        requester_name="Bob", requester_mc_id=7,
    )
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"
    assert "contribution" in row["status_detail"]


# -- priority: member first, then rotation ----------------------------------

async def test_member_request_served_before_rotation(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    mid = await _enqueue(sched)                              # member request
    rid = await sched.rotation.add(location_text="Rotation City", created_by="admin")

    await sched._advance()                                  # serves the member
    assert (await sched.missions.get(mid))["status"] == "skipped"
    assert (await sched.rotation.get(rid))["last_started_at"] is None  # untouched

    await sched._advance()                                  # queue empty -> rotation
    assert (await sched.rotation.get(rid))["last_started_at"] is not None


async def test_geocode_transient_failure_retries_not_fails(db):
    from fra_bot.geo.geocoder import GeocodeError

    class FlakyGeo(FakeGeo):
        async def search(self, query):
            raise GeocodeError("provider hiccup", status=503, transient=True)

    sched = MissionScheduler(_cfg(dry_run=True), FakeClient(), db, FlakyGeo())
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"          # kept for retry
    assert "will retry" in row["status_detail"]


async def test_geocode_permanent_failure_fails_with_message(db):
    from fra_bot.geo.geocoder import GeocodeError

    class DeadKeyGeo(FakeGeo):
        async def search(self, query):
            raise GeocodeError(
                "geocoder geocode.maps.co returned HTTP 401 for /search "
                "— check GEOCODER_API_KEY",
                status=401, transient=False,
            )

    sched = MissionScheduler(_cfg(dry_run=True), FakeClient(), db, DeadKeyGeo())
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "failed"
    assert "401" in row["status_detail"]


async def test_rotation_geocode_failure_deactivates(db):
    class DeadGeo(FakeGeo):
        async def search(self, query):
            from fra_bot.geo.geocoder import GeocodeError
            raise GeocodeError("no such place")

    sched = MissionScheduler(_cfg(dry_run=True), FakeClient(), db, DeadGeo())
    rid = await sched.rotation.add(location_text="Nowhere", created_by="admin")
    handled = await sched._advance()
    assert handled == 0
    row = await sched.rotation.get(rid)
    assert row["active"] == 0
    assert "geocode failed" in (row["address"] or "")


async def test_rotation_transient_geocode_keeps_entry_active(db):
    # A transient geocode error (Nominatim 429/5xx) must NOT permanently pause
    # a good rotation entry — otherwise a brief outage silently collapses the
    # rotation to whatever's already cached and it repeats forever.
    class FlakyGeo(FakeGeo):
        async def search(self, query):
            from fra_bot.geo.geocoder import GeocodeError
            raise GeocodeError("nominatim 429", status=429, transient=True)

    sched = MissionScheduler(_cfg(dry_run=True), FakeClient(), db, FlakyGeo())
    rid = await sched.rotation.add(location_text="Flaky", kind="large",
                                   mission_source="preset", created_by="admin")
    handled = await sched._advance()
    assert handled == 0                                   # nothing started
    row = await sched.rotation.get(rid)
    assert row["active"] == 1                             # kept for retry, not paused
    assert row["last_started_at"] is None                # its turn is preserved


async def test_rotation_lost_post_after_start_is_confirmed_not_refired(db):
    # The POST reaches MissionChief and starts the mission, but the response is
    # lost (network error). Confirming via the advanced cooldown must mark the
    # entry started so it is NOT re-fired (same large mission two days running).
    from fra_bot.mc.errors import MissionChiefError

    class LostPostClient(FakeClient):
        async def post_form(self, path, data, **kwargs):
            self.posted = True          # server DID create the mission in-game
            self.post_calls += 1
            raise MissionChiefError("connection reset after commit")

    sched = MissionScheduler(_cfg(dry_run=False), LostPostClient(), db, FakeGeo())
    rid = await sched.rotation.add(location_text="CityX", kind="large",
                                   mission_source="preset", latitude=1.0, longitude=2.0,
                                   address="CityX", created_by="admin")
    handled = await sched._process_rotation()
    assert handled == 1                                   # counted as started
    row = await sched.rotation.get(rid)
    assert row["last_started_at"] is not None             # advanced -> won't re-fire
    assert row["start_count"] == 1


# -- recurring promotion + next-up ------------------------------------------

async def test_recurring_request_promotes_to_rotation(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    mid = await _enqueue(sched, recurring=1, location_text="Grand Rapids")
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"
    assert row["rotation_id"] is not None                   # promoted + linked
    entries = await sched.rotation.list_all()
    assert len(entries) == 1
    assert entries[0]["location_text"] == "Grand Rapids"
    # Promotion happens AT INTAKE now (before geocoding), so coordinates
    # are cached later — at the first real start (rotation.mark_started).
    assert entries[0]["latitude"] is None


async def test_next_up_prefers_request_then_rotation(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    rid = await sched.rotation.add(location_text="Rotation City", created_by="admin")
    # With only a rotation entry, next_up is the rotation.
    nxt = await sched.next_up()
    assert nxt["origin"] == "rotation" and nxt["id"] == rid

    mid = await _enqueue(sched, location_text="Member City")
    nxt = await sched.next_up()
    assert nxt["origin"] == "request" and nxt["id"] == mid
    assert nxt["location"] in ("Member City", "Resolved NYC")


async def test_next_up_none_when_empty(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    assert await sched.next_up() is None


# -- owner-only paid (coins) path -------------------------------------------

async def test_coin_mission_preview_never_posts_even_when_live(db):
    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)  # live, but no confirm
    spec = MissionSpec(location_text="NYC", kind="large", source="preset").validate()
    outcome = await sched.run_coin_mission(spec, confirm=False)
    assert outcome.state == "dry_run"
    assert "PAID" in outcome.detail
    assert client.post_calls == 0  # preview must never submit


async def test_coin_mission_confirm_posts_with_coins_even_in_dry_run(db):
    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=True), client, db)  # global dry-run ON…
    spec = MissionSpec(location_text="NYC", kind="large", source="preset").validate()
    outcome = await sched.run_coin_mission(spec, confirm=True)  # …but confirmed
    assert outcome.state == "started"
    assert client.post_calls == 1
    assert client.posted_body["mission_position[coins]"] == "1"  # spent coins


async def test_coin_mission_ignores_free_cooldown(db):
    # A future free cooldown blocks a FREE start, but coins ignore it.
    client = FakeClient(_WAITING)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    spec = MissionSpec(location_text="NYC").validate()
    outcome = await sched.run_coin_mission(spec, confirm=True)
    assert outcome.state == "started"
    assert client.posted_body["mission_position[coins]"] == "1"


async def test_coin_mission_custom_posts_fields_and_coins(db):
    from fra_bot.mc.parsers.missions_custom import CustomMission

    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    spec = MissionSpec(
        location_text="NYC", kind="large", source="custom",
        custom=CustomMission("Paid fire", {"need_lf": 25}),
    ).validate()
    outcome = await sched.run_coin_mission(spec, confirm=True)
    assert outcome.state == "started"
    body = client.posted_body
    assert body["mission_position[mission_type_id]"] == "-1"
    assert body[value_field_name("need_lf")] == "25"
    assert body["mission_position[coins]"] == "1"


# -- dedicated request boards (bare-location intake) ------------------------

class FakeBoard:
    """Minimal BoardClient stand-in for the scheduler's board scan."""

    def __init__(self, posts, *, current_user_id=999):
        self._posts = posts
        self._page = SimpleNamespace(current_user_id=current_user_id)
        self.replies: list[tuple[int, str]] = []

    async def fetch_new_posts(self, thread_id, last_seen):
        fresh = [p for p in self._posts if p.post_id > (last_seen or 0)]
        return self._page, fresh

    async def post_reply(self, thread_id, content):
        self.replies.append((int(thread_id), content))
        return True

    # Guide maintenance stubs (find-or-edit): default to "no existing guide,
    # create succeeds" so the board-scan tests don't touch a real board.
    async def find_bot_post(self, thread_id, marker, *, max_pages=None):
        return None

    async def create_post_get_id(self, thread_id, content):
        return 1

    async def edit_post(self, post_id, content):
        return True

    async def delete_post(self, thread_id, post_id):
        return True


class GuideBoard(FakeBoard):
    """Tracks guide find/create/edit calls to test the find-or-edit rule."""

    def __init__(self, *, existing=None, current_user_id=999):
        super().__init__([], current_user_id=current_user_id)
        self._existing = existing
        self.created: list[tuple[int, str]] = []
        self.edited: list[tuple[int, str]] = []

    async def find_bot_post(self, thread_id, marker, *, max_pages=None):
        return self._existing

    async def create_post_get_id(self, thread_id, content):
        self.created.append((int(thread_id), content))
        return 55

    async def edit_post(self, post_id, content):
        self.edited.append((int(post_id), content))
        return True


def _post(pid, content, *, author="Bob", mc_id=7):
    return SimpleNamespace(
        post_id=pid, author_mc_id=mc_id, author_name=author, content=content
    )


async def _prime_board(sched, thread_id):
    # Skip baseline + guide so the scan enqueues immediately. Recording an
    # existing guide id makes _ensure_guide edit-in-place (a no-op via the
    # FakeBoard stub) instead of creating.
    await sched.state.set(f"mission_board_last_post:{thread_id}", "100")
    await sched.state.set(f"mission_board_guide_id:{thread_id}", "1")


async def test_board_scan_bare_location_enqueues_event(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    await _prime_board(sched, 15303)
    sched.board = FakeBoard([_post(101, "New York City")])
    created = await sched._scan_board(15303, "event")
    assert created == 1
    row = (await sched.missions.recent())[0]
    assert row["kind"] == "event" and row["mission_source"] == "preset"
    assert row["event_random"] == 1 and row["area"] == "large" and row["call_volume"] == "30"
    assert row["location_text"] == "New York City"
    assert row["board_thread_id"] == 15303


async def test_board_scan_bare_location_enqueues_large(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    await _prime_board(sched, 15307)
    sched.board = FakeBoard([_post(101, "New York City")])
    created = await sched._scan_board(15307, "large")
    assert created == 1
    row = (await sched.missions.recent())[0]
    assert row["kind"] == "large" and row["mission_source"] == "preset"


async def test_board_scan_clarifies_bad_field(db):
    sched = _scheduler(_cfg(dry_run=False), FakeClient(), db)  # live so replies post
    await _prime_board(sched, 15303)
    board = FakeBoard([_post(101, "NYC\nevent: Volcano")])
    sched.board = board
    created = await sched._scan_board(15303, "event")
    assert created == 0
    assert any("could not be processed" in c for _, c in board.replies)


async def test_board_replies_post_in_dry_run_too(db):
    """Feedback replies are informational posts on our own request topics —
    dry-run gates game actions, not member feedback."""
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    await _prime_board(sched, 15307)
    board = FakeBoard([_post(101, "New York City")])
    sched.board = board
    assert await sched._scan_board(15307, "large") == 1
    assert any("Event request received" in c for _, c in board.replies)


async def test_geocode_permanent_failure_notifies_board(db):
    from fra_bot.geo.geocoder import GeocodeError

    class DeadGeo(FakeGeo):
        async def search(self, query):
            raise GeocodeError("Nominatim found nothing for 'Atlantis'")

    sched = MissionScheduler(_cfg(dry_run=True), FakeClient(), db, DeadGeo())
    board = FakeBoard([])
    sched.board = board
    mid = await _enqueue(
        sched, source="board", board_thread_id=15307, board_post_id=101,
        requester_name="Alice", location_text="Atlantis",
    )
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "failed"
    assert any(
        "could not be resolved to GPS coordinates" in c and "Alice" in c
        for _, c in board.replies
    )


async def test_board_scan_skips_own_and_baseline(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    await sched.state.set("mission_board_guide_id:15307", "1")
    # First scan is a baseline (no cursor yet): records the cursor, enqueues nothing.
    sched.board = FakeBoard([_post(50, "Chicago")])
    assert await sched._scan_board(15307, "large") == 0
    assert await sched.missions.open_count() == 0
    # The bot's own [FRA] post is never treated as a request.
    sched.board = FakeBoard([_post(60, "[FRA] got it — large · preset at Chicago")])
    assert await sched._scan_board(15307, "large") == 0


async def test_poll_advances_queue_even_when_board_is_broken(db):
    """A broken request board must not starve the mission queue."""
    from fra_bot.mc.errors import FetchError

    cfg = _cfg(dry_run=True, events_enabled=True)
    cfg.automation.reply_to_board = False              # skip guide upkeep
    sched = _scheduler(cfg, FakeClient(), db)

    class _BrokenBoard:
        async def fetch_new_posts(self, thread_id, last_seen):
            raise FetchError(f"/alliance_threads/{thread_id}", 403)

    sched.board = _BrokenBoard()
    mid = await _enqueue(sched)                        # a Discord request waits
    await sched.poll()                                 # scan fails, queue runs
    row = await sched.missions.get(mid)
    assert row["status"] == "skipped"                  # dry-run: handled


async def test_board_scan_empty_baseline_then_first_post_enqueues(db):
    """An EMPTY board's first scan sets the baseline too — the first real
    post afterwards must be enqueued, not swallowed as a second baseline."""
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    await sched.state.set("mission_board_guide_id:15307", "1")
    sched.board = FakeBoard([])                        # nothing on the board yet
    assert await sched._scan_board(15307, "large") == 0
    sched.board = FakeBoard([_post(101, "New York City")])
    assert await sched._scan_board(15307, "large") == 1  # processed, not baseline


async def test_request_boards_dedup_and_gating(db):
    sched = _scheduler(_cfg(events_enabled=True, board_enabled=True), FakeClient(), db)
    boards = sched._request_boards()
    assert (15303, "event") in boards and (15307, "large") in boards
    # Nothing enabled -> no boards scanned.
    off = _scheduler(_cfg(events_enabled=False, board_enabled=False), FakeClient(), db)
    assert off._request_boards() == []


# -- guide: find-or-edit, never duplicate -----------------------------------

async def test_ensure_guide_creates_then_skips_unchanged(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    board = GuideBoard(existing=None)
    sched.board = board
    await sched._ensure_guide(15307, "large")
    # Two maintained posts: the how-to guide AND the schedule post.
    assert len(board.created) == 2
    assert board.created[0][1].startswith("[FRA] 📋 How to request a LARGE")
    assert "Last updated:" in board.created[0][1]       # carries a freshness stamp
    assert board.created[1][1].startswith("[FRA] 📅 Scheduled locations")
    assert "In rotation (recurring)" in board.created[1][1]
    assert await sched.state.get("mission_board_guide_id:15307") == "55"
    assert await sched.state.get("mission_board_sched_id:15307") == "55"
    # Same content next poll: no duplicates, no needless edits (within the
    # refresh window, unchanged instructions and unchanged schedule).
    await sched._ensure_guide(15307, "large")
    assert len(board.created) == 2
    assert board.edited == []


async def test_ensure_guide_post_throttles_timestamp_refresh(db):
    from fra_bot.db.repos import StateRepo
    from fra_bot.mc.board import ensure_guide_post

    state = StateRepo(db)
    board = GuideBoard(existing=None)
    keys = dict(id_key="g:id", hash_key="g:hash", refreshed_key="g:ref",
                marker="[FRA] X", signature="sig1")
    await ensure_guide_post(board, state, 1, desired="body @100",
                            now_epoch=100.0, min_refresh_seconds=3600, **keys)
    assert len(board.created) == 1                     # created
    # Same signature 10s later → throttled, no edit.
    await ensure_guide_post(board, state, 1, desired="body @110",
                            now_epoch=110.0, min_refresh_seconds=3600, **keys)
    assert board.edited == []
    # Same signature past the refresh window → edits to freshen the timestamp.
    await ensure_guide_post(board, state, 1, desired="body @9999",
                            now_epoch=9999.0, min_refresh_seconds=3600, **keys)
    assert board.edited and board.edited[-1] == (55, "body @9999")


async def test_ensure_guide_edits_existing_instead_of_duplicating(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    board = GuideBoard(existing=88)                     # a guide already on the board
    sched.board = board
    await sched._ensure_guide(15303, "event")
    assert board.created == []                          # found it -> edit, never create
    assert board.edited and board.edited[0][0] == 88
    assert await sched.state.get("mission_board_guide_id:15303") == "88"


async def test_ensure_guide_reedits_when_text_changes(db):
    sched = _scheduler(_cfg(dry_run=True, min_rate=5.0), FakeClient(), db)
    board = GuideBoard(existing=None)
    sched.board = board
    await sched._ensure_guide(15307, "large")
    assert len(board.created) == 2                      # guide + schedule post
    # A different contribution threshold changes the guide text -> re-edit the
    # stored post (not a new one).
    sched._auto.min_contribution_rate = 10.0
    await sched._ensure_guide(15307, "large")
    assert len(board.created) == 2                      # still no duplicates
    assert board.edited and board.edited[-1][0] == 55


async def test_force_guide_missions_creates_and_reports(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    board = GuideBoard(existing=None)
    sched.board = board
    line = await sched.force_guide(15307, "large")
    assert line.startswith("✅") and "#55" in line
    assert "schedule #55" in line                       # schedule post reported
    assert len(board.created) == 2                      # guide + schedule
    # A second force bypasses the throttle and edits the stored posts.
    line = await sched.force_guide(15307, "large")
    assert line.startswith("✅")
    assert board.edited and board.edited[-1][0] == 55


async def test_ensure_guide_suppressed_when_replies_off(db):
    cfg = _cfg(dry_run=True)
    cfg.automation.reply_to_board = False
    sched = _scheduler(cfg, FakeClient(), db)
    board = GuideBoard(existing=None)
    sched.board = board
    await sched._ensure_guide(15307, "large")
    assert board.created == [] and board.edited == []


async def test_schedule_post_lists_rotation_and_queue(db):
    """The maintained schedule post shows the recurring rotation of the
    board's kind plus queued member requests — like the reference bot's
    locations post."""
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    rid = await sched.rotation.add(
        location_text="Amsterdam, Netherlands", kind="large", created_by="admin",
    )
    await sched.rotation.add(
        location_text="Tokyo, Japan", kind="event", created_by="admin",
    )
    await _enqueue(sched, source="board", kind="large",
                   location_text="New York City", requester_name="Bob")

    body = await sched._schedule_body("large")
    assert body.startswith("[FRA] 📅 Scheduled locations")
    assert "Amsterdam, Netherlands" in body
    assert "Tokyo, Japan" not in body                  # other board's kind
    assert "- New York City" in body
    assert "requested by" not in body                  # names add noise

    # The events board shows its own kind, and an empty queue says so.
    events = await sched._schedule_body("event")
    assert "Tokyo, Japan" in events
    assert "Amsterdam" not in events
    assert "empty — post a location to add one" in events

    # Deactivated rotation entries stay visible, marked paused.
    await sched.rotation.set_active(rid, False)
    body = await sched._schedule_body("large")
    assert "— paused" in body


async def test_recurring_promoted_at_intake_while_queued(db):
    """A recurring request must appear in the rotation IMMEDIATELY (the
    member asked for a recurring spot), not only after its first start —
    with a busy queue it would otherwise stay invisible for hours. The
    schedule post shows it under rotation, not duplicated in the queue."""
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_WAITING), db)
    mid = await _enqueue(sched, recurring=1, location_text="Sacramento, CA")
    await sched._advance()                              # start waits (cooldown)
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"                   # still queued...
    assert row["rotation_id"] is not None               # ...but already rotating
    entries = await sched.rotation.list_all()
    assert len(entries) == 1
    assert entries[0]["location_text"] == "Sacramento, CA"

    body = await sched._schedule_body("large")
    assert body.count("Sacramento, CA") == 1            # rotation section only
    # A second poll never promotes twice.
    await sched._advance()
    assert len(await sched.rotation.list_all()) == 1


# -- one poll must not be starved by waiting items ---------------------------

async def test_waiting_event_does_not_starve_free_large(db):
    """The large and event cooldowns are SEPARATE. A queued event whose
    7-day window is still closed must not consume the poll's one start:
    a free large mission behind it starts in the same poll. (This was the
    bug: every poll re-checked one waiting event, returned 'handled', and
    the startable large mission never got a turn.)"""
    client = FakeClient(_ELIGIBLE, _EVENT_WAITING)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    event_id = await _enqueue(sched, kind="event", event_type_id=1)
    large_id = await _enqueue(sched)                    # kind=large, eligible
    await sched._advance()                              # ONE poll
    assert (await sched.missions.get(event_id))["status"] == "waiting"
    assert (await sched.missions.get(large_id))["status"] == "skipped"  # dry-run start


async def test_closed_cooldown_checks_one_form_per_kind(db):
    """The cooldown is per kind and alliance-wide: once one queued item
    hears 'window closed', the rest of that kind is skipped this poll —
    not re-fetched one by one."""
    client = FakeClient(_WAITING, _EVENT_WAITING)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    for i in range(8):
        await _enqueue(sched, location_text=f"Spot {i}")
    await sched._advance()
    form_fetches = [p for p in client.fetched if "tlat" in p]
    assert len(form_fetches) == 1                       # one answer serves all


async def test_transient_rechecks_are_bounded(db):
    """Transient failures (form unreadable) don't share a cooldown, so each
    item retries individually — but bounded, so one poll can't walk the
    whole queue."""
    from fra_bot.services.missions import _MAX_RECHECKS_PER_POLL
    client = FakeClient("<html><body>maintenance</body></html>")
    sched = _scheduler(_cfg(dry_run=True), client, db)
    for i in range(_MAX_RECHECKS_PER_POLL + 3):
        await _enqueue(sched, location_text=f"Spot {i}")
    await sched._advance()
    form_fetches = [p for p in client.fetched if "tlat" in p]
    assert len(form_fetches) == _MAX_RECHECKS_PER_POLL


async def test_queued_events_dont_starve_large_in_same_poll(db):
    """The exact live situation: five pending recurring events ahead of a
    large request (lower ids). One poll checks the event window ONCE, skips
    the other events, and still starts the free large mission."""
    client = FakeClient(_ELIGIBLE, _EVENT_WAITING)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    event_ids = [
        await _enqueue(sched, kind="event", event_type_id=1, recurring=1,
                       location_text=f"Event {i}")
        for i in range(5)
    ]
    large_id = await _enqueue(sched, location_text="New York City")
    await sched._advance()                              # ONE poll
    assert (await sched.missions.get(event_ids[0]))["status"] == "waiting"
    for eid in event_ids[1:]:                           # skipped, not walked
        assert (await sched.missions.get(eid))["status"] == "pending"
    assert (await sched.missions.get(large_id))["status"] == "skipped"  # dry-run start
    event_fetches = [p for p in client.fetched if "Event" in p]
    assert len(event_fetches) == 1


async def test_two_startable_kinds_still_one_start_per_poll(db):
    """Both windows free, an event and a large queued: one poll starts
    exactly ONE of them (the older request); the other follows next poll.
    Pins the 'at most one start per poll' contract against the
    blocked-kinds machinery."""
    client = FakeClient(_ELIGIBLE, _EVENT)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    event_id = await _enqueue(sched, kind="event", event_type_id=1)
    large_id = await _enqueue(sched)
    await sched._advance()
    statuses = {
        (await sched.missions.get(event_id))["status"],
        (await sched.missions.get(large_id))["status"],
    }
    assert statuses == {"done", "pending"}              # one started, one waits
    assert client.post_calls == 1


async def test_recheck_budget_trip_keeps_rotation_out(db):
    """When the transient-recheck budget is spent with member requests
    still unexamined, the rotation may not grab the free window — member
    requests keep priority. (The budget trip means we don't KNOW the queue
    is empty.)"""
    from fra_bot.services.missions import _MAX_RECHECKS_PER_POLL
    client = FakeClient("<html><body>maintenance</body></html>")
    sched = _scheduler(_cfg(dry_run=False), client, db)
    for i in range(_MAX_RECHECKS_PER_POLL + 1):
        await _enqueue(sched, location_text=f"Spot {i}")
    rid = await sched.rotation.add(
        location_text="Rot", kind="large", mission_source="preset",
        latitude=40.7, longitude=-74.0, address="Rot", created_by="admin",
    )
    await sched._advance()
    assert (await sched.rotation.get(rid))["start_count"] == 0


async def test_transient_failures_back_off_and_cooldown_resets_attempts(db):
    """Transient failures are re-parked with a growing delay (not due the
    very next poll), and a later healthy cooldown answer resets the
    attempt counter — an hour of maintenance pages must not permanently
    retire a request that was merely waiting out its window."""
    import datetime as dt
    client = FakeClient("<html><body>maintenance</body></html>")
    sched = _scheduler(_cfg(dry_run=True), client, db)
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None           # backed off, not due now
    parked = dt.datetime.fromisoformat(row["next_attempt_at"])
    assert parked > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=3)

    # The page recovers with the cooldown closed: healthy recheck → reset.
    client.large_html = _WAITING
    await sched.missions.set_status(
        mid, "waiting", "retry now", next_attempt_at=None, announce=False,
    )
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    assert row["attempts"] == 0                          # consecutive failures only


async def test_rotation_event_head_does_not_starve_large_entry(db):
    """Rotation mirror of the queue fix: an event entry at the head of the
    cycle whose 7-day window is closed must not hide a large entry whose
    24h window is free — the kinds have separate cooldowns."""
    client = FakeClient(_ELIGIBLE, _EVENT_WAITING)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    ev = await sched.rotation.add(
        location_text="Tokyo", kind="event", mission_source="preset",
        event_type_id=1, latitude=35.6, longitude=139.7, address="Tokyo",
        created_by="admin",
    )
    lg = await sched.rotation.add(
        location_text="NYC", kind="large", mission_source="preset",
        latitude=40.7, longitude=-74.0, address="NYC",
        created_by="admin",
    )
    handled = await sched._advance()                    # queue empty → rotation
    assert handled == 1
    assert (await sched.rotation.get(lg))["start_count"] == 1   # large started
    assert (await sched.rotation.get(ev))["start_count"] == 0   # event still waiting


async def test_real_start_queues_event_ping(db):
    """A confirmed live start lands in the event-ping outbox with its
    resolved location, so the pinger can mention the right region role."""
    from fra_bot.db.repos import EventPingsRepo
    sched = _scheduler(_cfg(dry_run=False), FakeClient(_ELIGIBLE), db)
    mid = await _enqueue(sched)
    await sched._advance()
    assert (await sched.missions.get(mid))["status"] == "done"
    pings = await EventPingsRepo(db).unposted()
    assert len(pings) == 1
    assert pings[0]["kind"] == "large"
    assert pings[0]["address"] == "Resolved NYC"
    assert abs(pings[0]["latitude"] - 40.5) < 1e-6


async def test_dry_run_start_does_not_ping(db):
    from fra_bot.db.repos import EventPingsRepo
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_ELIGIBLE), db)
    await _enqueue(sched)
    await sched._advance()
    assert await EventPingsRepo(db).unposted() == []


async def test_preexisting_far_wait_is_reverified(db):
    """Rows parked far out by older code (which trusted the computed
    eligible_at outright) are pulled back within the re-verify horizon."""
    import datetime as dt
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_WAITING), db)
    mid = await _enqueue(sched)
    await sched.missions.claim(mid)
    # Computed, not hardcoded: a literal date would silently pass once it
    # lies in the past (the row is then simply due and takes the normal
    # recheck path, never exercising the sweep).
    far = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat(
        timespec="seconds"
    )
    await sched.missions.set_status(
        mid, "waiting", f"next free mission at {far}; queued",
        next_attempt_at=far, announce=False,
    )
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    parked = dt.datetime.fromisoformat(row["next_attempt_at"])
    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=31)
    assert parked <= horizon


async def test_waiting_next_attempt_capped_at_30_minutes(db):
    """The form's 'Last free mission' timestamp is trusted only up to 30
    minutes: a far-future eligible_at (tz skew, stale page) may not park
    the request beyond the cap — it re-verifies against the live form."""
    import datetime as dt
    sched = _scheduler(_cfg(dry_run=True), FakeClient(_WAITING), db)
    mid = await _enqueue(sched)                         # form frees in 2100
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"
    parked = dt.datetime.fromisoformat(row["next_attempt_at"])
    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=31)
    assert parked <= horizon


async def test_rotation_skips_entry_with_open_queue_request(db):
    """A recurring request is promoted at intake, but until its QUEUED
    first start happens the rotation must not also start that location —
    one request would otherwise run twice."""
    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await _enqueue(sched, recurring=1, location_text="Sacramento, CA")
    await sched._promote_pending_recurring()
    row = await sched.missions.get(mid)
    assert row["rotation_id"] is not None
    # Queue item still open -> rotation has nothing to run.
    assert await sched.rotation.next_entry() is None
    # Once the queued start completes, the entry rotates normally again.
    await sched._advance()
    assert (await sched.missions.get(mid))["status"] == "done"
    entry = await sched.rotation.next_entry()
    assert entry is not None and entry["id"] == row["rotation_id"]


# -- game-refused starts: wait + shared backoff, never a spurious failure ----

class RefusingClient(FakeClient):
    """POST accepted (HTTP 200) but the free cooldown never advances — the
    game refused the start (another alliance mission/event still running)."""

    async def fetch_page(self, path, *, referer=None):
        self.fetched.append(path)
        return self.event_html if "Event" in path else self.large_html


async def test_game_refusal_waits_instead_of_failing(db):
    client = RefusingClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await _enqueue(sched)
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "waiting"                      # NOT failed
    assert "still running" in row["status_detail"]
    assert row["next_attempt_at"] is not None
    assert await sched.start_backoff_until() is not None   # backoff armed


async def test_backoff_blocks_further_post_attempts(db):
    client = RefusingClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    await _enqueue(sched)
    await sched._advance()                                 # refused → backoff
    assert client.post_calls == 1
    m2 = await _enqueue(sched)
    await sched._advance()                                 # backoff: no POST
    assert client.post_calls == 1
    row = await sched.missions.get(m2)
    assert row["status"] == "waiting"
    assert "busy" in row["status_detail"]


async def test_confirmed_start_arms_backoff_for_next_kind(db):
    # The bot knows what it started: right after a confirmed large start,
    # an event request must wait instead of being submitted (and refused).
    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    await _enqueue(sched)                                  # large
    eid = await _enqueue(sched, kind="event")
    await sched._advance()                                 # large starts
    assert client.post_calls == 1
    await sched._advance()                                 # event: backoff
    assert client.post_calls == 1                          # nothing submitted
    row = await sched.missions.get(eid)
    assert row["status"] == "waiting"
    assert "busy" in row["status_detail"]


async def test_game_refusal_gives_up_after_deadline(db):
    client = RefusingClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    mid = await _enqueue(sched)
    await db.execute(
        "UPDATE scheduled_missions SET created_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", mid),
    )
    await sched._advance()
    row = await sched.missions.get(mid)
    assert row["status"] == "failed"
    assert "giving up" in row["status_detail"]


async def test_rotation_refusal_keeps_turn(db):
    client = RefusingClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=False), client, db)
    rid = await sched.rotation.add(location_text="NYC", created_by="admin")
    handled = await sched._advance()
    assert handled == 0
    row = await sched.rotation.get(rid)
    assert row["last_started_at"] is None                  # turn not consumed
    assert row["active"] == 1                              # not deactivated


async def test_coin_start_bypasses_backoff_and_pings(db):
    from fra_bot.db.repos import EventPingsRepo

    client = FakeClient(_ELIGIBLE)
    sched = _scheduler(_cfg(dry_run=True), client, db)
    await sched.set_start_backoff()                        # alliance "busy"
    spec = MissionSpec(location_text="NYC", kind="large", source="preset").validate()
    outcome = await sched.run_coin_mission(spec, confirm=True)
    assert outcome.state == "started"                      # coins ignore it
    assert len(await EventPingsRepo(db).unposted()) == 1


def test_saved_not_found_error_lists_visible_captions(tmp_path):
    from fra_bot.services.missions import MissionScheduler, _SavedMissionNotFound
    import pytest

    svc = MissionScheduler.__new__(MissionScheduler)
    html = (
        "<a class='mission_custom_saved_restore' "
        "params='{\"caption\": \"Big Fire\"}'>Big Fire</a>"
        "<a class='mission_custom_saved_restore' "
        "params='{\"caption\": \"Dock Blaze\"}'>Dock Blaze</a>"
    )
    with pytest.raises(_SavedMissionNotFound) as excinfo:
        svc._build_body(
            None, html, kind="large", source="saved", preset_type_id=None,
            caption=None, custom_values={}, saved_name="[WF] Wildfire",
            latitude=1.0, longitude=2.0, address="x",
        )
    message = str(excinfo.value)
    assert "Big Fire" in message and "Dock Blaze" in message

    with pytest.raises(_SavedMissionNotFound) as excinfo:
        svc._build_body(
            None, "<html>no anchors</html>", kind="large", source="saved",
            preset_type_id=None, caption=None, custom_values={},
            saved_name="[WF] Wildfire", latitude=1.0, longitude=2.0, address="x",
        )
    assert "NO saved missions" in str(excinfo.value)


async def test_saved_missions_html_returns_plain_when_anchors_present(tmp_path):
    from fra_bot.services.missions import MissionScheduler

    svc = MissionScheduler.__new__(MissionScheduler)
    html = (
        "<a class='mission_custom_saved_restore' "
        "params='{\"caption\": \"Big Fire\"}'>Big Fire</a>"
    )
    assert await svc._saved_missions_html(html) == html
    # No anchors + no Playwright in the test env -> plain HTML comes back.
    from types import SimpleNamespace
    svc.cfg = SimpleNamespace(missionchief=SimpleNamespace(base_url="https://x"))
    svc.client = SimpleNamespace(session=SimpleNamespace(cookie_jar=[]))
    assert await svc._saved_missions_html("<html></html>") == "<html></html>"
