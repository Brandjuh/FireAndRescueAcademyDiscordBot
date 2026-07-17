"""Game-sync: userscript payload validation, intake flow, hotspots."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.game_sync import GameSyncCog
from fra_bot.db.database import Database
from fra_bot.db.repos import GameSyncRepo, LinksRepo, MemberActionsRepo
from fra_bot.services.game_sync import (
    SyncPayloadError,
    cluster_hotspots,
    parse_sync_payload,
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
