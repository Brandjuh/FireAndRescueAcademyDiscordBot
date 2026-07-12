"""The LSSM vehicle-catalog parser + normaliser + tag inference.

Fixtures mirror the real ``vehicles.ts`` / ``buildings.ts`` shapes (single
AND double quoted strings, ``19_200`` numeric separators, trailing commas,
nested training, and — for buildings — arrow functions and spreads in value
positions that the name extractor must skip)."""

import asyncio

import pytest

from fra_bot.mc import vehicles_catalog as vc


VEHICLES_TS = """\
import type { InternalVehicle } from 'typings/Vehicle';

export default {
    0: {
        caption: 'Type 1 fire engine',
        credits: 5_000,
        coins: 25,
        staff: { min: 1, max: 6 },
        possibleBuildings: [0, 13],
        waterTank: 750,
        pumpType: 'fire',
    },
    9: {
        caption: "Warden's HazMat truck",
        credits: 20_000,
        coins: 40,
        staff: {
            min: 1,
            max: 2,
            training: { 'Fire Station': { gw_gefahrgut: { all: true } } },
        },
        possibleBuildings: [0],
        isTrailer: true,
    },
    27: {
        caption: 'Ambulance',
        credits: 15_000,
        coins: 20,
        staff: { min: 1, max: 2 },
        possibleBuildings: [3, 16],
    },
} satisfies Record<number, InternalVehicle>;
"""

# buildings.ts is NOT pure data: the arrow function + spread below must be
# ignored by the name extractor.
BUILDINGS_TS = """\
import type { InternalBuilding } from 'typings/Building';

export default {
    0: {
        caption: 'Fire station',
        color: '#bb0000',
        credits: 100_000,
        levelPrices: {
            credits: [10_000, 50_000, ...Array(37).fill(100_000)],
        },
        maxExtensionsFunction: buildingsByType =>
            Math.floor((Object.keys(buildingsByType[0] ?? {}).length ?? 0) / 2),
        extensions: [
            { caption: 'Ambulance Extension', credits: 100_000 },
        ],
    },
    3: {
        caption: 'Ambulance station',
        credits: 100_000,
    },
    16: {
        caption: 'Ambulance station (Small station)',
        credits: 50_000,
    },
    13: {
        caption: 'Fire station (Small station)',
        credits: 50_000,
    },
} satisfies Record<number, InternalBuilding>;
"""


# ---------------------------------------------------------------------------
# parse_ts_module (the literal parser)
# ---------------------------------------------------------------------------

def test_parse_ts_module_reads_every_vehicle():
    data = vc.parse_ts_module(VEHICLES_TS)
    assert set(data) == {"0", "9", "27"}


def test_parse_ts_module_handles_quotes_separators_and_nesting():
    data = vc.parse_ts_module(VEHICLES_TS)
    assert data["0"]["caption"] == "Type 1 fire engine"     # single-quoted
    assert data["9"]["caption"] == "Warden's HazMat truck"  # double-quoted apostrophe
    assert data["0"]["credits"] == 5000                     # 5_000 separator
    assert data["0"]["possibleBuildings"] == [0, 13]        # array of nums
    assert data["9"]["staff"]["training"] == {
        "Fire Station": {"gw_gefahrgut": {"all": True}}
    }
    assert data["9"]["isTrailer"] is True


def test_parse_ts_module_stops_at_the_top_level_object():
    # The `satisfies …;` tail after the closing brace must not derail it.
    data = vc.parse_ts_module(VEHICLES_TS)
    assert "27" in data and isinstance(data["27"], dict)


def test_parse_ts_module_rejects_garbage():
    with pytest.raises(ValueError):
        vc.parse_ts_module("const x = 1;")  # no object literal


# ---------------------------------------------------------------------------
# parse_building_names (the targeted extractor)
# ---------------------------------------------------------------------------

def test_parse_building_names_skips_functions_and_spreads():
    names = vc.parse_building_names(BUILDINGS_TS)
    assert names == {
        0: "Fire station",
        3: "Ambulance station",
        16: "Ambulance station (Small station)",
        13: "Fire station (Small station)",
    }


def test_parse_building_names_ignores_nested_extension_captions():
    # 'Ambulance Extension' is an extension caption, not a building name.
    names = vc.parse_building_names(BUILDINGS_TS)
    assert "Ambulance Extension" not in names.values()


# ---------------------------------------------------------------------------
# normalize_vehicle
# ---------------------------------------------------------------------------

def _catalog():
    raw = vc.parse_ts_module(VEHICLES_TS)
    names = vc.parse_building_names(BUILDINGS_TS)
    return {vid: vc.normalize_vehicle(int(vid), v, names) for vid, v in raw.items()}


def test_normalize_resolves_building_names():
    veh = _catalog()["0"]
    assert veh["buildings"] == ["Fire station", "Fire station (Small station)"]
    assert veh["water_tank"] == 750
    assert veh["pump_type"] == "fire"
    assert veh["staff_min"] == 1 and veh["staff_max"] == 6


def test_normalize_flattens_trainings():
    veh = _catalog()["9"]
    assert veh["trainings"] == ["Fire Station: HazMat"]
    assert veh["is_trailer"] is True


def test_normalize_unknown_building_degrades_gracefully():
    raw = {"caption": "Ghost", "possibleBuildings": [999]}
    veh = vc.normalize_vehicle(5, raw, {})
    assert veh["buildings"] == ["Building 999"]


# ---------------------------------------------------------------------------
# infer_tags / all_tag_names
# ---------------------------------------------------------------------------

def test_infer_tags_category_and_capability():
    cat = _catalog()
    assert vc.infer_tags(cat["0"]) == ["Fire", "Water/Pump"]
    assert set(vc.infer_tags(cat["9"])) == {"Fire", "Training required", "Trailer"}
    assert vc.infer_tags(cat["27"]) == ["EMS"]


def test_infer_tags_falls_back_to_other():
    veh = vc.normalize_vehicle(5, {"caption": "Tow truck", "possibleBuildings": []}, {})
    assert vc.infer_tags(veh) == [vc.FALLBACK_TAG]


def test_infer_tags_never_exceeds_the_post_cap():
    # Everything at once: category + water + training + trailer.
    veh = vc.normalize_vehicle(5, {
        "caption": "Everything", "possibleBuildings": [0],
        "waterTank": 500, "isTrailer": True,
        "staff": {"training": {"Fire Station": {"gw_gefahrgut": {"all": True}}}},
    }, {0: "Fire station"})
    tags = vc.infer_tags(veh)
    assert len(tags) <= vc.MAX_TAGS_PER_POST
    assert len(tags) == len(set(tags))  # no duplicates


def test_all_tag_names_covers_every_inferable_tag():
    known = set(vc.all_tag_names())
    for veh in _catalog().values():
        assert set(vc.infer_tags(veh)) <= known


# ---------------------------------------------------------------------------
# keys + hashes
# ---------------------------------------------------------------------------

def test_vehicle_key_is_stable_on_id():
    veh = _catalog()["0"]
    assert vc.vehicle_key(veh) == "veh-0"


def test_content_hash_folds_format_version_but_data_hash_does_not(monkeypatch):
    veh = _catalog()["0"]
    data_before, content_before = vc.data_hash(veh), vc.content_hash(veh)
    monkeypatch.setattr(vc, "FORMAT_VERSION", "vehicles-forum-v999")
    # A format bump re-renders (content_hash moves) but is NOT a data change.
    assert vc.data_hash(veh) == data_before
    assert vc.content_hash(veh) != content_before


def test_data_hash_tracks_real_changes():
    veh = _catalog()["0"]
    before = vc.data_hash(veh)
    veh = dict(veh, credits=9999)
    assert vc.data_hash(veh) != before


# ---------------------------------------------------------------------------
# fetch_catalog error handling (a ClientTimeout raises asyncio.TimeoutError,
# which is neither ClientError nor ValueError — both fetches must handle it)
# ---------------------------------------------------------------------------

class _TimeoutResp:
    async def __aenter__(self):
        raise asyncio.TimeoutError()

    async def __aexit__(self, *a):
        return False


class _OkResp:
    def __init__(self, text):
        self._text = text
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text


class _FakeSession:
    """Serves the inline fixtures, or times out on whichever URL matches."""

    def __init__(self, *, timeout_on):
        self._timeout_on = timeout_on

    def get(self, url, timeout=None):
        if self._timeout_on in url:
            return _TimeoutResp()
        return _OkResp(VEHICLES_TS if "vehicles" in url else BUILDINGS_TS)


async def test_buildings_timeout_degrades_to_building_labels():
    # buildings.ts times out -> names can't be read -> "Building N" fallback,
    # NOT a crash (and NOT a bogus 45-min-timeout alert upstream).
    cat = await vc.fetch_catalog(_FakeSession(timeout_on="buildings"))
    by_id = {v["id"]: v for v in cat}
    assert len(cat) == 3
    assert by_id[0]["buildings"] == ["Building 0", "Building 13"]


async def test_vehicles_timeout_becomes_value_error():
    # vehicles.ts times out -> a clean ValueError the sync turns into an
    # "unusable" summary, not an escaping TimeoutError.
    with pytest.raises(ValueError):
        await vc.fetch_catalog(_FakeSession(timeout_on="vehicles"))
