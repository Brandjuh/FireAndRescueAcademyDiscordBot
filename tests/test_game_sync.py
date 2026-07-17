"""Game-sync: userscript payload validation, intake flow, hotspots."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.game_sync import GameSyncCog
from fra_bot.db.database import Database
from fra_bot.db.repos import GameSyncRepo, LinksRepo, MemberActionsRepo
from fra_bot.services.game_sync import (
    Hotspot,
    SyncPayloadError,
    cluster_hotspots,
    parse_sync_payload,
    place_name,
    render_hotspots,
    summarize_buildings,
)

pytestmark = pytest.mark.asyncio


def _payload(mc=101, coords=None):
    return json.dumps({
        "fra_profile_sync": 1,
        "mc_user_id": mc,
        "mc_name": "Alice",
        "buildings": {
            "total": 3,
            "by_type": {"0": 2, "2": 1},
            "coords": coords if coords is not None
            else [[40.71, -74.0], [40.72, -74.01], [51.92, 4.47]],
        },
        "vehicles": {"total": 12, "by_type": {"0": 12}},
    })


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "gs.sqlite3")
    await database.connect()
    yield database
    await database.close()


# -- payload validation --------------------------------------------------------

async def test_parse_valid_payload():
    payload = parse_sync_payload(_payload())
    assert payload.mc_user_id == 101 and payload.mc_name == "Alice"
    assert payload.building_count == 3 and payload.vehicle_count == 12
    assert len(payload.coords) == 3
    assert payload.buildings_by_type == {"0": 2, "2": 1}


async def test_parse_rejects_garbage():
    with pytest.raises(SyncPayloadError):
        parse_sync_payload("not json at all")
    with pytest.raises(SyncPayloadError):
        parse_sync_payload(json.dumps({"hello": 1}))
    with pytest.raises(SyncPayloadError):
        parse_sync_payload(json.dumps({
            "fra_profile_sync": 1, "mc_user_id": "geen-nummer",
            "buildings": {}, "vehicles": {},
        }))


async def test_parse_drops_bad_coords_keeps_good():
    payload = parse_sync_payload(_payload(
        coords=[[40.7, -74.0], ["x", "y"], [999, 0], [51.9, 4.4]]
    ))
    assert payload.coords == [(40.7, -74.0), (51.9, 4.4)]


async def test_summarize_names_known_types():
    text = summarize_buildings({"0": 30, "2": 4, "77": 2})
    assert "30× fire station" in text and "4× hospital" in text
    assert "type 77" in text


# -- intake flow -----------------------------------------------------------------

class FakeAttachment:
    def __init__(self, data: bytes, filename="fra-profile-sync.json"):
        self.filename = filename
        self.size = len(data)
        self._data = data

    async def read(self):
        return self._data


class FakeMessage:
    def __init__(self, channel_id, payload_bytes=None, *, webhook=True):
        self.channel = SimpleNamespace(id=channel_id)
        self.webhook_id = 1 if webhook else None
        self.content = ""
        self.attachments = (
            [FakeAttachment(payload_bytes)] if payload_bytes else []
        )
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def _cog(db, *, channel_id=500):
    cog = GameSyncCog.__new__(GameSyncCog)
    actions = []

    async def _log(**kwargs):
        actions.append(kwargs)

    cog.bot = SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(discord=SimpleNamespace(
            channels=SimpleNamespace(game_sync=channel_id),
        )),
        log_member_action=_log,
    )
    cog.repo = GameSyncRepo(db)
    return cog, actions


async def test_intake_stores_and_links_and_reacts(db):
    await LinksRepo(db).upsert(9001, 101, status="approved")
    cog, actions = _cog(db)
    message = FakeMessage(500, _payload().encode())
    await cog.on_message(message)
    assert message.reactions == ["✅"]
    row = await GameSyncRepo(db).get_by_mc(101)
    assert row["discord_user_id"] == 9001
    assert row["building_count"] == 3 and row["vehicle_count"] == 12
    assert actions and actions[0]["action"] == "game_synced"
    # Re-sync overwrites the same row (one per MC account).
    await cog.on_message(FakeMessage(500, _payload().encode()))
    assert len(await GameSyncRepo(db).all_synced()) == 1


async def test_intake_rejects_invalid_payload(db):
    cog, actions = _cog(db)
    message = FakeMessage(500, b"{\"nope\": true}")
    await cog.on_message(message)
    assert message.reactions == ["⚠️"]
    assert actions == []


async def test_intake_ignores_other_channels_and_humans(db):
    cog, actions = _cog(db)
    await cog.on_message(FakeMessage(999, _payload().encode()))     # wrong channel
    await cog.on_message(FakeMessage(500, _payload().encode(), webhook=False))
    assert await GameSyncRepo(db).all_synced() == []
    assert actions == []


# -- hotspots --------------------------------------------------------------------

async def test_cluster_hotspots_counts_buildings_and_members():
    member_coords = {
        1: [(40.71, -74.01), (40.72, -74.02), (40.73, -74.03)],  # NYC
        2: [(40.74, -74.04), (51.92, 4.47)],                      # NYC + R'dam
    }
    spots = cluster_hotspots(member_coords, grid=0.1)
    assert spots[0].buildings == 4 and spots[0].members == 2     # NYC cell wins
    assert spots[1].buildings == 1 and spots[1].members == 1
    text = render_hotspots(spots, member_total=2, building_total=5)
    assert "4 buildings" in text and "maps.google.com" in text


async def test_render_hotspots_empty_points_at_the_userscript():
    assert "userscript" in render_hotspots([], member_total=0, building_total=0)


async def test_render_hotspots_prefers_place_names_over_coordinates():
    named = Hotspot(40.75, -74.05, 4, 2, place="Jersey City, New Jersey")
    bare = Hotspot(51.95, 4.45, 1, 1)
    text = render_hotspots([named, bare], member_total=2, building_total=5)
    assert "**Jersey City, New Jersey**" in text
    assert "**[51.95, 4.45]**" in text     # nameless cell falls back to coords


async def test_named_spots_survive_a_broken_geocoder():
    class FakeGeocoder:
        async def reverse_details(self, lat, lng):
            if lat > 45:
                raise RuntimeError("nominatim down")
            return {"city": "Jersey City", "state": "New Jersey"}

    cog = GameSyncCog.__new__(GameSyncCog)
    cog.bot = SimpleNamespace(geocoder=FakeGeocoder())
    named = await cog._named([Hotspot(40.7, -74.0, 4, 2), Hotspot(51.9, 4.4, 1, 1)])
    assert named[0].place == "Jersey City, New Jersey"
    assert named[1].place is None    # geocoder error -> nameless, not a crash


async def test_vehicle_names_cache_survives_a_broken_catalog(db, monkeypatch):
    import fra_bot.cogs.game_sync as gs_cog
    from fra_bot.db.repos import StateRepo

    cog = GameSyncCog.__new__(GameSyncCog)
    cog.bot = SimpleNamespace(db=db)

    async def fake_catalog(session):
        return [{"id": 30, "name": "Type 1 fire engine"}]

    monkeypatch.setattr(
        "fra_bot.mc.vehicles_catalog.fetch_catalog", fake_catalog
    )
    assert await cog._vehicle_names() == {30: "Type 1 fire engine"}

    # Age the cache past the refresh window, then break the fetch: the
    # stale cache must still answer instead of raising or returning {}.
    state = StateRepo(db)
    data = json.loads(await state.get(gs_cog.VEHICLE_NAMES_KEY))
    data["fetched_at"] = "2020-01-01T00:00:00+00:00"
    await state.set(gs_cog.VEHICLE_NAMES_KEY, json.dumps(data))

    async def broken(session):
        raise RuntimeError("github down")

    monkeypatch.setattr("fra_bot.mc.vehicles_catalog.fetch_catalog", broken)
    assert await cog._vehicle_names() == {30: "Type 1 fire engine"}


async def test_place_name_picks_locality_and_region():
    assert place_name(
        {"city": "Jersey City", "state": "New Jersey", "country": "United States"}
    ) == "Jersey City, New Jersey"
    # No city-level key: county carries, country backs up a missing state.
    assert place_name({"county": "Bergen County", "country": "United States"}) \
        == "Bergen County, United States"
    assert place_name({"state": "Texas"}) == "Texas"
    assert place_name({}) is None
    assert place_name(None) is None
    # Locality == region (city-states): no "Hamburg, Hamburg".
    assert place_name({"city": "Hamburg", "state": "Hamburg"}) == "Hamburg"
