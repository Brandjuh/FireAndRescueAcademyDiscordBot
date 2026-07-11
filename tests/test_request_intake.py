"""Intake-time checks for Discord requests: the contribution gate, the
building panel's immediate location/type verdict, the always-written log
rows, and the mission chooser's preset/previously-created options."""

import asyncio
import json
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.cogs.missions import MissionChooserView, MissionsCog
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
        sync=SimpleNamespace(members_interval=60),
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


class FakeTrainingsService:
    """Captures the immediate-execution kick from the Discord intake."""

    def __init__(self):
        self.executed = asyncio.Event()

    async def execute_queue_now(self):
        self.executed.set()
        return 1


class FakeBot(SimpleNamespace):
    async def wait_until_ready(self):
        await asyncio.Event().wait()  # park the task loops forever

    def job_lock(self, name):
        locks = self.__dict__.setdefault("_locks", {})
        return locks.setdefault(name, asyncio.Lock())


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
    bot = FakeBot(
        db=db, cfg=_cfg(), geocoder=geocoder or FakeGeocoder(),
        trainings=FakeTrainingsService(),
    )
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


async def test_gate_rejects_low_contribution_with_numbers_and_retry_eta(db):
    import datetime as dt

    await _seed_member(db, 42, "Alice", 2.0, discord_id=100)
    verdict = await contribution_gate(db, 100, 5.0)
    assert not verdict.ok and verdict.reason == "low_contribution"
    assert verdict.mc_user_id == 42
    assert "2%" in verdict.rejection_text and "5%" in verdict.rejection_text
    assert "2%" in verdict.log_detail
    # A member who just fixed their alliance tax is told when the roster
    # refreshes and to retry after that (no live per-member check exists).
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    assert verdict.retry_at is not None and verdict.retry_at > now
    assert f"<t:{verdict.retry_at}:R>" in verdict.rejection_text
    assert "try again" in verdict.rejection_text

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
        assert "✅" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_training_intake_kicks_the_queue_immediately(db):
    # Opening a training is FIRST priority: the queue runs the moment the
    # request is created, not at the next scheduled pass.
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    cog = _requests_cog(db)
    try:
        interaction = FakeInteraction()
        await cog.submit_training(interaction, "fire", "HazMat", remind=False)
        await asyncio.wait_for(cog.bot.trainings.executed.wait(), timeout=2)
        assert "right now" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_training_intake_does_not_kick_when_automation_off(db):
    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)
    cog = _requests_cog(db)
    cog.bot.cfg.automation.training.enabled = False
    try:
        interaction = FakeInteraction()
        await cog.submit_training(interaction, "fire", "HazMat", remind=False)
        await asyncio.sleep(0)  # let any (wrong) kick task run
        assert not cog.bot.trainings.executed.is_set()
        assert "automation is currently OFF" in interaction.sent[0]
    finally:
        cog.cog_unload()


async def test_training_chooser_shows_cached_availability(db):
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.trainings import AVAILABILITY_STATE_KEY

    cog = _requests_cog(db)
    try:
        assert await cog._availability_line() is None  # never collected yet
        await StateRepo(db).set(AVAILABILITY_STATE_KEY, json.dumps({
            "counts": {"fire": 3, "police": 1, "ems": 0, "coastal": 2},
            "at": 1_750_000_000,
        }))
        line = await cog._availability_line()
        assert "Fire" in line and "**3**" in line
        assert "<t:1750000000:R>" in line
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

async def test_previous_saved_names_dedupe_and_exclusions(db):
    repo = MissionsRepo(db)
    await repo.create(source="discord", kind="large", mission_source="saved",
                      saved_name="Big Fire Drill", shape="rectangle")
    await repo.create(source="discord", kind="large", mission_source="saved",
                      saved_name="Big Fire Drill", shape="rectangle")  # dupe
    await repo.create(source="discord", kind="large", mission_source="custom",
                      caption="Dock Blaze", custom_values='{"need_lf": 25}',
                      shape="rectangle")  # customs are board-only, never listed
    await repo.create(source="discord", kind="large", mission_source="saved",
                      saved_name="Bad Name", shape="rectangle",
                      status="failed", status_detail="not in dropdown")
    await repo.create(source="discord", kind="event", mission_source="preset",
                      shape="rectangle")  # events never listed
    names = await repo.previous_saved_names()
    assert names == ["Big Fire Drill"]  # deduped; custom/failed/event excluded


async def test_terminal_at_intake_mission_row_is_not_claimable(db):
    repo = MissionsRepo(db)
    await repo.create(source="discord", kind="large", mission_source="preset",
                      shape="rectangle", status="cancelled",
                      status_detail="rejected at intake: test")
    assert await repo.claimable() == []
    row = (await repo.recent())[0]
    assert row["status"] == "cancelled"


async def test_chooser_offers_presets_and_saved_missions_but_no_custom(db):
    service = FakeMissionService()
    cog = _missions_cog(db, service)
    try:
        view = MissionChooserView(cog, "large", ["Big Fire Drill", "Dock Blaze"])
        source_values = [o.value for o in view.source_select.options]
        assert "preset:Major fire" in source_values
        assert "saved" in source_values
        assert "custom" not in source_values  # customs are board-only
        assert view.saved_select is not None
        labels = [o.label for o in view.saved_select.options]
        assert labels == ["Big Fire Drill", "Dock Blaze"]
        # No cached list -> no saved select, the modal input still works.
        assert MissionChooserView(cog, "large", []).saved_select is None
    finally:
        cog.cog_unload()


async def test_event_chooser_has_no_mission_data_selects(db):
    # The kind is chosen up front (panel buttons / two-way menu); the event
    # chooser only asks for a schedule — its options live in the modal.
    cog = _missions_cog(db, FakeMissionService())
    try:
        view = MissionChooserView(cog, "event")
        assert view.kind == "event"
        assert view.source_select is None and view.saved_select is None
    finally:
        cog.cog_unload()


async def test_kind_pick_menu_offers_event_and_large(db):
    from fra_bot.cogs.missions import MissionKindPickView

    cog = _missions_cog(db, FakeMissionService())
    try:
        labels = [item.label for item in MissionKindPickView(cog).children]
        assert "Alliance event" in labels
        assert "Large scale alliance mission" in labels
    finally:
        cog.cog_unload()


# ---------------------------------------------------------------------------
# Outcome notification: Discord DM -> in-game PM, never a channel mention
# ---------------------------------------------------------------------------

async def test_closed_dms_fall_back_to_ingame_pm(db):
    import discord

    from fra_bot.cogs.automation import AutomationCog

    await _seed_member(db, 42, "Alice", 10.0, discord_id=100)

    class ClosedDmUser:
        async def send(self, text):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "DMs closed"
            )

    sent_pms = []
    channel_sends = []

    async def send_new(name, subject, body):
        sent_pms.append((name, subject, body))
        return {"ok": True, "detail": "sent", "thread": None}

    bot = FakeBot(
        db=db, cfg=_cfg(),
        dm_mirror=SimpleNamespace(send_new=send_new),
        add_dynamic_items=lambda *items: None,
        get_user=lambda uid: ClosedDmUser(),
        get_channel=lambda cid: SimpleNamespace(send=channel_sends.append),
    )
    cog = AutomationCog(bot)
    try:
        row = {
            "id": 9, "kind": "building", "status": "done",
            "status_detail": "built hospital #5",
            "requester_mc_id": 42, "requester_name": "Discord Nick",
            "payload": json.dumps({
                "discord_user_id": 100, "channel_id": 555,
                "building_type": "hospital",
                "address": "St Mary Hospital, Grand Rapids",
                "latitude": 42.96, "longitude": -85.67, "building_id": 5,
            }),
        }
        await cog._notify_requester(row)
        assert len(sent_pms) == 1
        name, subject, body = sent_pms[0]
        assert name == "Alice"          # resolved via mc_id, not the nickname
        assert subject == "Building request"
        assert "APPROVED" in body and "**" not in body
        assert channel_sends == []      # never a mention in the channel
    finally:
        cog.cog_unload()

# ---------------------------------------------------------------------------
# The game's saved-missions list: cache + refresh
# ---------------------------------------------------------------------------

_DROPDOWN_HTML = (
    "<div>"
    "<a class='mission_custom_saved_restore' "
    "params='{\"caption\": \"Big Fire Drill\", \"need_lf\": \"25\"}'>"
    "Big Fire Drill (Alice)</a>"
    "<a class='mission_custom_saved_restore' "
    "params='{\"caption\": \"Dock Blaze\", \"need_rw\": \"5\"}'>"
    "Dock Blaze (Bob)</a>"
    "</div>"
)


def _mission_scheduler(db, pages):
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.missions import MissionScheduler

    class _Client:
        def __init__(self):
            self.fetches = 0

        async def fetch_page(self, path, *, referer=None):
            self.fetches += 1
            return pages.get(path, "<html></html>")

    svc = MissionScheduler.__new__(MissionScheduler)
    svc.state = StateRepo(db)
    svc.client = _Client()
    return svc


async def test_refresh_saved_missions_caches_the_dropdown(db):
    svc = _mission_scheduler(db, {"/missionAllianceNew": _DROPDOWN_HTML})
    names = await svc.refresh_saved_missions()
    assert names == ["Big Fire Drill", "Dock Blaze"]
    assert await svc.saved_mission_names() == ["Big Fire Drill", "Dock Blaze"]


async def test_empty_dropdown_never_clobbers_a_known_list(db):
    svc = _mission_scheduler(db, {"/missionAllianceNew": _DROPDOWN_HTML})
    await svc.refresh_saved_missions()
    # A glitchy/empty page later keeps the old names.
    svc2 = _mission_scheduler(db, {"/missionAllianceNew": "<html></html>"})
    names = await svc2.refresh_saved_missions()
    assert names == ["Big Fire Drill", "Dock Blaze"]


async def test_saved_mission_names_empty_without_cache(db):
    svc = _mission_scheduler(db, {})
    assert await svc.saved_mission_names() == []

# ---------------------------------------------------------------------------
# Training chooser: live course list + pagination past Discord's 25-option cap
# ---------------------------------------------------------------------------

async def test_courses_for_prefers_the_live_harvest(db):
    from fra_bot.db.repos import StateRepo
    from fra_bot.services.trainings import TRAINING_COURSES_STATE_KEY

    await StateRepo(db).set(TRAINING_COURSES_STATE_KEY, json.dumps({
        "courses": {"fire": {"Hotshot Crew Training": 0, "HazMat": 3}},
        "at": 1,
    }))
    cog = _requests_cog(db)
    try:
        fire = dict(await cog.courses_for("fire"))
        assert "Hotshot Crew Training" in fire
        assert "Airport Firefighter" not in fire  # live list is authoritative
        police = await cog.courses_for("police")
        assert police  # no harvest yet -> built-in catalog fallback
        # Reminder duration: live 0 falls back to the built-in catalog.
        assert await cog.course_days("fire", "HazMat") == 3
        assert await cog.course_days("fire", "Hotshot Crew Training") == 0
    finally:
        cog.cog_unload()


async def test_course_select_paginates_past_25_options(db):
    from fra_bot.cogs.requests_panel import TrainingChooserView

    cog = _requests_cog(db)
    try:
        view = TrainingChooserView(cog)
        view._courses = [(f"Course {i:02d}", 1) for i in range(30)]
        view._apply_course_page()
        assert len(view.t_select.options) == 25  # 24 courses + the page flip
        assert view.t_select.options[-1].value == "_page"
        assert "page 1/2" in view.t_select.placeholder
        view._page += 1
        view._apply_course_page()
        labels = [o.value for o in view.t_select.options]
        assert "Course 24" in labels and "Course 00" not in labels
        assert "page 2/2" in view.t_select.placeholder
    finally:
        cog.cog_unload()


async def test_course_select_stays_flat_under_25(db):
    from fra_bot.cogs.requests_panel import TrainingChooserView

    cog = _requests_cog(db)
    try:
        view = TrainingChooserView(cog)
        view._courses = [("HazMat", 3)]
        view._apply_course_page()
        assert [o.value for o in view.t_select.options] == ["HazMat"]
    finally:
        cog.cog_unload()
