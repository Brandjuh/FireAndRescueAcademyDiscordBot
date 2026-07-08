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
    assert entries[0]["latitude"] == 40.5                   # cached resolved coords


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


def _post(pid, content, *, author="Bob", mc_id=7):
    return SimpleNamespace(
        post_id=pid, author_mc_id=mc_id, author_name=author, content=content
    )


async def _prime_board(sched, thread_id):
    # Skip baseline + guide so the scan enqueues immediately.
    await sched.state.set(f"mission_board_last_post:{thread_id}", "100")
    await sched.state.set(f"mission_board_guide_posted:{thread_id}", "1")


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
    assert any("couldn't use" in c for _, c in board.replies)


async def test_board_scan_skips_own_and_baseline(db):
    sched = _scheduler(_cfg(dry_run=True), FakeClient(), db)
    await sched.state.set("mission_board_guide_posted:15307", "1")
    # First scan is a baseline (no cursor yet): records the cursor, enqueues nothing.
    sched.board = FakeBoard([_post(50, "Chicago")])
    assert await sched._scan_board(15307, "large") == 0
    assert await sched.missions.open_count() == 0
    # The bot's own [FRA] post is never treated as a request.
    sched.board = FakeBoard([_post(60, "[FRA] got it — large · preset at Chicago")])
    assert await sched._scan_board(15307, "large") == 0


async def test_request_boards_dedup_and_gating(db):
    sched = _scheduler(_cfg(events_enabled=True, board_enabled=True), FakeClient(), db)
    boards = sched._request_boards()
    assert (15303, "event") in boards and (15307, "large") in boards
    # Nothing enabled -> no boards scanned.
    off = _scheduler(_cfg(events_enabled=False, board_enabled=False), FakeClient(), db)
    assert off._request_boards() == []
