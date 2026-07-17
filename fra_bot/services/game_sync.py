"""Game-data sync: userscript payloads → profiles + hotspots.

Members run the FRA userscript (``tools/fra-profile-sync.user.js``) in
their own browser; it reads THEIR ``/api/buildings`` and
``/api/vehicles`` (data the bot's account can never see) and posts a
compact JSON file to a Discord webhook in a private intake channel.
This module validates those payloads and turns the collected building
coordinates into alliance hotspots (where coverage clusters).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: Payload marker + version the userscript sends.
PAYLOAD_MARKER = "fra_profile_sync"
MAX_COORDS = 3000
MAX_TYPES = 200

#: Building type ids we can name (same ids the bot uses elsewhere).
#: Unknown ids render as "type <id>" — the game adds types over time.
BUILDING_TYPE_NAMES = {
    0: "fire station", 1: "dispatch center", 2: "hospital",
    3: "rescue station", 4: "fire academy", 5: "police station",
    6: "police academy", 7: "police academy", 9: "staging area",
    10: "prison", 19: "rescue academy", 24: "coastal rescue school",
}


class SyncPayloadError(ValueError):
    """The webhook message is not a valid profile-sync payload."""


@dataclass(frozen=True)
class SyncPayload:
    mc_user_id: int
    mc_name: str | None
    building_count: int
    vehicle_count: int
    buildings_by_type: dict[str, int]
    vehicles_by_type: dict[str, int]
    coords: list[tuple[float, float]]

    @property
    def buildings_json(self) -> str:
        return json.dumps({
            "by_type": self.buildings_by_type,
            "coords": [[lat, lng] for lat, lng in self.coords],
        })

    @property
    def vehicles_json(self) -> str:
        return json.dumps({"by_type": self.vehicles_by_type})


def _counts(raw, *, what: str) -> tuple[int, dict[str, int]]:
    if not isinstance(raw, dict):
        raise SyncPayloadError(f"{what} section missing or not an object")
    by_type_raw = raw.get("by_type") or {}
    if not isinstance(by_type_raw, dict) or len(by_type_raw) > MAX_TYPES:
        raise SyncPayloadError(f"{what}.by_type invalid")
    by_type: dict[str, int] = {}
    for key, value in by_type_raw.items():
        try:
            by_type[str(int(key))] = max(0, int(value))
        except (TypeError, ValueError):
            raise SyncPayloadError(f"{what}.by_type has a non-numeric entry")
    try:
        total = int(raw.get("total", sum(by_type.values())))
    except (TypeError, ValueError):
        raise SyncPayloadError(f"{what}.total not a number")
    return max(0, total), by_type


def parse_sync_payload(text: str) -> SyncPayload:
    """Validate a raw webhook payload; raises SyncPayloadError."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SyncPayloadError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict) or PAYLOAD_MARKER not in data:
        raise SyncPayloadError("missing fra_profile_sync marker")
    try:
        mc_user_id = int(data.get("mc_user_id"))
    except (TypeError, ValueError):
        raise SyncPayloadError("mc_user_id missing or not a number")
    if mc_user_id <= 0:
        raise SyncPayloadError("mc_user_id must be positive")

    building_count, buildings_by_type = _counts(
        data.get("buildings"), what="buildings"
    )
    vehicle_count, vehicles_by_type = _counts(
        data.get("vehicles"), what="vehicles"
    )

    coords_raw = (data.get("buildings") or {}).get("coords") or []
    if not isinstance(coords_raw, list) or len(coords_raw) > MAX_COORDS:
        raise SyncPayloadError("buildings.coords invalid or too large")
    coords: list[tuple[float, float]] = []
    for pair in coords_raw:
        try:
            lat, lng = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue  # one bad pair must not kill the sync
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            coords.append((round(lat, 4), round(lng, 4)))

    name = data.get("mc_name")
    return SyncPayload(
        mc_user_id=mc_user_id,
        mc_name=str(name)[:80] if name else None,
        building_count=building_count,
        vehicle_count=vehicle_count,
        buildings_by_type=buildings_by_type,
        vehicles_by_type=vehicles_by_type,
        coords=coords,
    )


def summarize_buildings(by_type: dict[str, int], *, top: int = 4) -> str:
    """'30× fire station, 4× hospital, …' — named where we know the id."""
    items = sorted(by_type.items(), key=lambda kv: -kv[1])[:top]
    parts = []
    for type_id, count in items:
        name = BUILDING_TYPE_NAMES.get(int(type_id), f"type {type_id}")
        parts.append(f"{count}× {name}")
    return ", ".join(parts)


# -- hotspots -----------------------------------------------------------------

@dataclass(frozen=True)
class Hotspot:
    latitude: float    # cell center
    longitude: float
    buildings: int
    members: int
    place: str | None = None  # reverse-geocoded name, filled by the cog
    #: The distinct MC ids behind ``members`` — carried so a place-level
    #: merge can union them instead of double-counting.
    member_ids: frozenset = frozenset()


def _top_types(
    by_type_dicts: list[dict], names: dict[int, str], top: int
) -> list[tuple[str, int]]:
    """The members' per-type dicts summed and ranked, ids resolved through
    ``names`` with "type N" as fallback, garbage keys skipped."""
    totals: dict[int, int] = {}
    for by_type in by_type_dicts:
        for type_id, count in by_type.items():
            try:
                totals[int(type_id)] = totals.get(int(type_id), 0) + int(count)
            except (TypeError, ValueError):
                continue
    ranked = sorted(totals.items(), key=lambda item: -item[1])[:top]
    return [
        (names.get(type_id, f"type {type_id}"), count)
        for type_id, count in ranked
    ]


def top_building_types(
    by_type_dicts: list[dict], top: int = 6
) -> list[tuple[str, int]]:
    """Alliance-wide building counts per type name, biggest first."""
    return _top_types(by_type_dicts, BUILDING_TYPE_NAMES, top)


def top_vehicle_types(
    by_type_dicts: list[dict], names: dict[int, str], top: int = 6
) -> list[tuple[str, int]]:
    """Alliance-wide vehicle counts per type name, biggest first. ``names``
    is the LSSM id → name map (see the cog's cached fetch); an id the
    catalog doesn't know degrades to "type N"."""
    return _top_types(by_type_dicts, names, top)


#: Nominatim address keys that name the locality, most specific first.
_LOCALITY_KEYS = ("city", "town", "village", "hamlet", "municipality", "county")


def place_name(details: dict | None) -> str | None:
    """A short human place name ("Jersey City, New Jersey") from Nominatim
    address details; None when there is nothing usable there (open sea)."""
    if not isinstance(details, dict):
        return None
    locality = next(
        (details[key] for key in _LOCALITY_KEYS if details.get(key)), None
    )
    region = details.get("state") or details.get("country")
    if locality and region and locality != region:
        return f"{locality}, {region}"
    return locality or region


def cluster_hotspots(
    member_coords: dict[int, list[tuple[float, float]]],
    *, grid: float = 0.1, top: int = 12,
) -> list[Hotspot]:
    """Grid-cluster every member's building coordinates into hotspots.

    ``grid`` is the cell size in degrees (0.1° ≈ 11 km). Returns the top
    cells by building count, with the number of DISTINCT members present
    — a cell with 200 buildings from 8 members is an alliance hotspot; a
    cell with 200 buildings from 1 member is one person's home town."""
    cells: dict[tuple[int, int], dict] = {}
    for mc_user_id, coords in member_coords.items():
        for lat, lng in coords:
            key = (int(lat // grid), int(lng // grid))
            cell = cells.setdefault(key, {"n": 0, "members": set()})
            cell["n"] += 1
            cell["members"].add(mc_user_id)
    spots = [
        Hotspot(
            latitude=round((key[0] + 0.5) * grid, 4),
            longitude=round((key[1] + 0.5) * grid, 4),
            buildings=cell["n"],
            members=len(cell["members"]),
            member_ids=frozenset(cell["members"]),
        )
        for key, cell in cells.items()
    ]
    spots.sort(key=lambda s: (-s.buildings, -s.members))
    return spots[:top]


def merge_by_place(spots: list[Hotspot], *, top: int = 12) -> list[Hotspot]:
    """Named cells that resolve to the SAME place collapse into one entry:
    buildings summed, distinct members unioned, the centre weighted by
    building count. One metro area (every 11 km cell of New York is its
    own hotspot) can no longer crowd every other city out of the list.
    Nameless cells pass through untouched. Feed this MORE cells than
    ``top`` (the callers cluster twice as wide) so merging leaves a full
    list."""
    groups: dict[str, list[Hotspot]] = {}
    out: list[Hotspot] = []
    for spot in spots:
        if spot.place:
            groups.setdefault(spot.place, []).append(spot)
        else:
            out.append(spot)
    for place, group in groups.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        buildings = sum(s.buildings for s in group)
        ids = frozenset().union(*(s.member_ids for s in group))
        # Without carried ids (hand-built spots) the true distinct count
        # is unknowable; the biggest cell is the honest lower bound.
        members = len(ids) if ids else max(s.members for s in group)
        out.append(Hotspot(
            latitude=round(
                sum(s.latitude * s.buildings for s in group) / buildings, 4
            ),
            longitude=round(
                sum(s.longitude * s.buildings for s in group) / buildings, 4
            ),
            buildings=buildings,
            members=members,
            place=place,
            member_ids=ids,
        ))
    out.sort(key=lambda s: (-s.buildings, -s.members))
    return out[:top]


def render_hotspots(
    spots: list[Hotspot], *, member_total: int, building_total: int
) -> str:
    if not spots:
        return (
            "No game data synced yet — members install the userscript "
            "(tools/fra-profile-sync.user.js) and click 'Sync to FRA'."
        )
    lines = [
        f"🔥 **Alliance hotspots** — {building_total} buildings from "
        f"{member_total} synced member(s):"
    ]
    for index, spot in enumerate(spots, 1):
        where = spot.place or f"[{spot.latitude:.2f}, {spot.longitude:.2f}]"
        buildings = f"{spot.buildings} building" + ("s" if spot.buildings != 1 else "")
        members = f"{spot.members} member" + ("s" if spot.members != 1 else "")
        lines.append(
            f"{index}. **{where}** — {buildings} · {members} · "
            f"[map](<https://maps.google.com/?q={spot.latitude},{spot.longitude}>)"
        )
    return "\n".join(lines)[:1900]
