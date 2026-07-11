"""Intake-time checks for Discord requests: the contribution gate, the
building panel's immediate location/type verdict, the always-written log
rows, and the mission chooser's preset/previously-created options."""

import asyncio
import json
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.cogs.missions import MissionChooserView, MissionsCog, _values_text
from fra_bot.cogs.requests_panel import RequestsCog
from fra_bot.db.database import Database
from fra_bot.db.repos import AutomationRepo, MissionsRepo
from fra_bot.geo.geocoder import GeocodeError, GeocodeResult
from fra_bot.services.intake import INTAKE_REJECTED_FLAG, contribution_gate


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "intake.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _all_requests(db):
    async with db.conn.execute(
        "SELECT * FROM automation_requests ORDER BY id"
    ) as cur:
        return list(await cur.fetchall())


async def _seed_member(db, mc_id, name, rate, *, discord_id=None, status="approved"):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, contribution_rate, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, '2026-01-01', '2026-07-01')",
        (mc_id, name, rate),
    )
    if discord_id is not None:
        await db.execute(
            "INSERT INTO member_links (discord_id, mc_user_id, status, "
            "reviewer_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, '2026-01-01', '2026-01-01')",
            (discord_id, mc_id, status),
        )


def _cfg():
    return SimpleNamespace(
        automation=SimpleNamespace(
            dry_run=True,
            training=SimpleNamespace(enabled=True, min_contribution_rate=5.0),
            building=SimpleNamespace(enabled=True, min_contribution_rate=5.0),
            mission=SimpleNamespace(enabled=True, min_contribution_rate=5.0),
            events=SimpleNamespace(min_contribution_rate=7.0),
        ),
    )


class FakeGeocoder:
    def __init__(self):
        self.result = None
        self.error = None

    async def resolve_maps_link(self, link):
        if self.error is not None:
            raise self.error
        return self.result


class FakeMissionService:
    def __init__(self):
        self.calls = []

    async def enqueue_discord(self, spec, **kwargs):
        self.calls.append((spec, kwargs))
        return 40 + len(self.calls)


class FakeBot(SimpleNamespace):
    async def wait_until_ready(self):
        await asyncio.Event().wait()  # park the task loops forever


class FakeResponse:
    def __init__(self, sink):
        self._sink = sink
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self._sink.append(content)

    async def defer(self, **kwargs):
        self._done = True


class FakeFollowup:
    def __init__(self, sink):
        self._sink = sink

    async def send(self, content=None, **kwargs):
        self._sink.append(content)


class FakeInteraction:
    _next_id = 900000

    def __init__(self, user_id=100, name="Tester"):
        FakeInteraction._next_id += 1
        self.id = FakeInteraction._next_id
        self.user = SimpleNamespace(id=user_id, display_name=name)
        self.channel_id = 555
        self.sent: list[str] = []
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup(self.sent)


def _requests_cog(db, geocoder=None):
    bot = FakeBot(db=db, cfg=_cfg(), geocoder=geocoder or FakeGeocoder())
    return RequestsCog(bot)


def _missions_cog(db, service):
    bot = FakeBot(db=db, cfg=_cfg(), missions_service=service)
    return MissionsCog(bot)


HOSPITAL = GeocodeResult(
    latitude=42.96, longitude=-85.67,
    address="St Mary Hospital, 200 Jefferson Ave, Grand Rapids",
    source="test", place_text="St Mary Hospital", place_type="hospital",
)
CAFE = GeocodeResult(
    latitude=42.96, longitude=-85.67,
    address="Corner Cafe, 1 Main St, Grand Rapids",
    source="test", place_text="Corner Cafe", place_type="cafe",
)
LINK = "https://maps.app.goo.gl/AbCdEf123"


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

async def test_gate_rejects_unlinked_user(db):
    verdict = await contribution_gate(db, 100, 5.0)
    assert not verdict.ok and verdict.reason == "not_linked"
    assert "verify" in verdict.rejection_text
    assert "no verified" in verdict.log_detail


async def test_gate_rejects_pending_link(db):
    await _seed_member(db, 42, "Alice", 50.0, discord_id=100, status="pending")
    verdict = await contribution_gate(db, 100, 5.0)
    assert not verdict.ok and verdict.reason == "not_linked"


async def test_gate_rejects_low_contribution_with_numbers(db):
    await _seed_member(db, 42, "Alice", 2.0, discord_id=100)
    verdict = await contribution_gate(db, 100, 5.0)
    assert not verdict.ok and verdict.reason == "low_contribution"
    assert verdict.mc_user_id == 42
    assert "2%" in verdict.rejection_text and "5%" in verdict.rejection_text
    assert "2%" in verdict.log_detail

async def test_gate_passes_good_contribution(db):
    await _seed_member(db, 42, "Alice", 5.0, discord_id=100)
    verdict = await contribution_gate(db, 100, 5.0)
    assert verdict.ok and verdict.mc_user_id == 42 and verdict.rate == 5.0


async def test_gate_passes_linked_member_missing_from_roster(db):
    # Link exists but the roster sweep hasn't picked the member up yet:
    # allowed with an unknown rate (execute-time gates re-check later).
    await db.execute(
        "INSERT INTO member_links (discord_id, mc_user_id, status, reviewer_id, "
        "created_at, updated_at) VALUES (100, 42, 'approved', 0, '2026-01-01', '2026-01-01')"
    )
    verdict = await contribution_gate(db, 100, 5.0)
    assert verdict.ok and verdict.mc_user_id == 42 and verdict.rate is None


# ---------------------------------------------------------------------------
# Training intake
# ---------------------------------------------------------------------------

async def test_training_intake_carries_mc_identity(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    cog = _requests_cog(db)
    try:
        interaction = FakeInteraction()
        await cog.submit_training(interaction, "fire", "HazMat", remind=False)
        rows = await AutomationRepo(db).claimable("training")
        assert len(rows) == 1
        assert rows[0]["requester_mc_id"] == 42
        assert rows[0]["status"] == "pending"
        assert "queued" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_training_intake_rejects_and_logs(db):
    await _seed_member(db, 42, "Alice", 1.0, discord_id=100)
    cog = _requests_cog(db)
    try:
        interaction = FakeInteraction()
        await cog.submit_training(interaction, "fire", "HazMat", remind=False)
        assert await AutomationRepo(db).claimable("training") == []  # nothing runnable
        row = (await _all_requests(db))[0]
        assert row["status"] == "skipped"
        assert "rejected at intake" in row["status_detail"]
        assert json.loads(row["payload"])[INTAKE_REJECTED_FLAG] is True
        assert "not submitted" in interaction.sent[0]
    finally:
        cog.cog_unload()


# ---------------------------------------------------------------------------
# Building intake: link, contribution, location, type
# ---------------------------------------------------------------------------

async def test_building_intake_accepts_hospital_with_resolved_pin(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    geocoder = FakeGeocoder()
    geocoder.result = HOSPITAL
    cog = _requests_cog(db, geocoder)
    try:
        interaction = FakeInteraction()
        await cog.submit_building(interaction, LINK)
        rows = await AutomationRepo(db).claimable("building")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["building_type"] == "hospital"
        assert payload["latitude"] == HOSPITAL.latitude  # executor skips re-geocode
        assert rows[0]["requester_mc_id"] == 42
        assert "accepted" in interaction.sent[0] and "hospital" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_building_intake_rejects_non_hospital_pin(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    geocoder = FakeGeocoder()
    geocoder.result = CAFE
    cog = _requests_cog(db, geocoder)
    try:
        interaction = FakeInteraction()
        await cog.submit_building(interaction, LINK)
        assert await AutomationRepo(db).claimable("building") == []
        row = (await _all_requests(db))[0]
        assert row["status"] == "skipped"
        assert "not a hospital or prison" in row["status_detail"]
        assert "Corner Cafe" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_building_intake_rejects_unresolvable_location(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    geocoder = FakeGeocoder()
    geocoder.error = GeocodeError("place not found")
    cog = _requests_cog(db, geocoder)
    try:
        interaction = FakeInteraction()
        await cog.submit_building(interaction, LINK)
        row = (await _all_requests(db))[0]
        assert row["status"] == "skipped"
        assert "geocoding failed" in row["status_detail"]
        assert "could not be resolved" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_building_intake_queues_on_transient_geocoder_failure(db):
    # A geocoder hiccup is not the member's fault: the request queues and
    # the poller performs the pin check at its next pass instead.
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    geocoder = FakeGeocoder()
    geocoder.error = GeocodeError("geocoder 503", transient=True)
    cog = _requests_cog(db, geocoder)
    try:
        interaction = FakeInteraction()
        await cog.submit_building(interaction, LINK)
        rows = await AutomationRepo(db).claimable("building")
        assert len(rows) == 1
        assert "latitude" not in json.loads(rows[0]["payload"])
        assert "queued" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_building_intake_rejects_low_contribution_before_geocoding(db):
    await _seed_member(db, 42, "Alice", 1.0, discord_id=100)
    geocoder = FakeGeocoder()  # would blow up if called (result None)
    geocoder.error = AssertionError("geocoder must not be called")
    cog = _requests_cog(db, geocoder)
    try:
        interaction = FakeInteraction()
        await cog.submit_building(interaction, LINK)
        row = (await _all_requests(db))[0]
        assert row["status"] == "skipped"
        assert "contribution" in row["status_detail"]
    finally:
        cog.cog_unload()


async def test_building_intake_rejects_non_maps_link_and_logs(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    cog = _requests_cog(db)
    try:
        interaction = FakeInteraction()
        await cog.submit_building(interaction, "not a link at all")
        row = (await _all_requests(db))[0]
        assert row["status"] == "skipped"
        assert "not a Google Maps link" in row["status_detail"]
        assert "Rejected" in interaction.sent[0]
    finally:
        cog.cog_unload()


# ---------------------------------------------------------------------------
# Mission / event intake
# ---------------------------------------------------------------------------

async def test_mission_intake_rejects_and_logs_cancelled_row(db):
    await _seed_member(db, 42, "Alice", 1.0, discord_id=100)
    service = FakeMissionService()
    cog = _missions_cog(db, service)
    try:
        interaction = FakeInteraction()
        await cog.submit_request(
            interaction, location="Grand Rapids", kind="large",
            schedule="once", source="preset",
        )
        assert len(service.calls) == 1
        _, kwargs = service.calls[0]
        assert kwargs["status"] == "cancelled"
        assert "rejected at intake" in kwargs["status_detail"]
        assert kwargs["channel_id"] is None  # admin log only, no public shaming
        assert "not submitted" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_event_intake_uses_the_events_minimum(db):
    # 6% passes the mission gate (5%) but fails the events gate (7%).
    await _seed_member(db, 42, "Alice", 6.0, discord_id=100)
    service = FakeMissionService()
    cog = _missions_cog(db, service)
    try:
        interaction = FakeInteraction()
        await cog.submit_request(
            interaction, location="Grand Rapids", kind="large",
            schedule="once", source="preset",
        )
        _, kwargs = service.calls[-1]
        assert "status" not in kwargs and kwargs["requester_mc_id"] == 42

        interaction = FakeInteraction()
        await cog.submit_request(
            interaction, location="Grand Rapids", kind="event",
            schedule="once", source="preset", event_type="Storm",
        )
        _, kwargs = service.calls[-1]
        assert kwargs.get("status") == "cancelled"
    finally:
        cog.cog_unload()


async def test_mission_intake_passes_preset_through(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    service = FakeMissionService()
    cog = _missions_cog(db, service)
    try:
        interaction = FakeInteraction()
        await cog.submit_request(
            interaction, location="Grand Rapids", kind="large",
            schedule="once", source="preset", preset="Major fire",
        )
        spec, kwargs = service.calls[0]
        assert spec.preset_type_id is not None
        assert kwargs["requester_mc_id"] == 42
        assert "queued" in interaction.sent[0]
    finally:
        cog.cog_unload()


# ---------------------------------------------------------------------------
# Previously created missions: repo + chooser
# ---------------------------------------------------------------------------

async def test_previous_mission_options_dedupe_and_exclusions(db):
    repo = MissionsRepo(db)
    await repo.create(source="discord", kind="large", mission_source="saved",
                      saved_name="Big Fire Drill", shape="rectangle")
    await repo.create(source="discord", kind="large", mission_source="saved",
                      saved_name="Big Fire Drill", shape="rectangle")  # dupe
    await repo.create(source="discord", kind="large", mission_source="custom",
                      caption="Dock Blaze", custom_values='{"need_lf": 25}',
                      shape="rectangle")
    await repo.create(source="discord", kind="large", mission_source="saved",
                      saved_name="Bad Name", shape="rectangle",
                      status="failed", status_detail="not in dropdown")
    await repo.create(source="discord", kind="event", mission_source="preset",
                      shape="rectangle")  # events never listed
    options = await repo.previous_mission_options()
    names = [(row["mission_source"], row["saved_name"] or row["caption"])
             for row in options]
    assert ("saved", "Big Fire Drill") in names
    assert ("custom", "Dock Blaze") in names
    assert all(name != "Bad Name" for _, name in names)
    assert len(names) == 2  # deduped, failed + event rows excluded


async def test_terminal_at_intake_mission_row_is_not_claimable(db):
    repo = MissionsRepo(db)
    await repo.create(source="discord", kind="large", mission_source="preset",
                      shape="rectangle", status="cancelled",
                      status_detail="rejected at intake: test")
    assert await repo.claimable() == []
    row = (await repo.recent())[0]
    assert row["status"] == "cancelled"


async def test_chooser_offers_presets_and_previous_missions(db):
    service = FakeMissionService()
    cog = _missions_cog(db, service)
    try:
        previous = [
            {"id": 1, "mission_source": "saved", "saved_name": "Big Fire Drill",
             "caption": None, "custom_values": None},
            {"id": 2, "mission_source": "custom", "saved_name": None,
             "caption": "Dock Blaze", "custom_values": '{"need_lf": 25}'},
        ]
        view = MissionChooserView(cog, previous)
        source_values = [o.value for o in view.source_select.options]
        assert "preset:Major fire" in source_values
        assert "custom" in source_values and "saved" in source_values
        assert view.prev_select is not None
        labels = [o.label for o in view.prev_select.options]
        assert "Big Fire Drill" in labels and "Dock Blaze" in labels
        # No history -> no second select, the modal input still works.
        assert MissionChooserView(cog, []).prev_select is None
    finally:
        cog.cog_unload()


def test_values_text_round_trip():
    assert _values_text('{"need_lf": 25, "water_needed": 15000}') == (
        "need_lf=25 water_needed=15000"
    )
    assert _values_text(None) == ""
    assert _values_text("not json") == ""
