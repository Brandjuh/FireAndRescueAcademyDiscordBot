"""MissionChief ``/api/buildings`` JSON + proximity dedup for auto-building.

Before the daily build places a hospital/prison, it checks our existing
buildings so it never stacks a second one on top of an existing facility.
The game exposes every building we own — with coordinates and type — at
``/api/buildings``; we treat a candidate within a small radius of an
existing SAME-TYPE building as a duplicate and skip it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

# Numeric building-type ids on MissionChief (match browser_builder).
BUILDING_TYPE_IDS = {
    "hospital": 2, "prison": 10,
    "fire academy": 4, "police academy": 7,
    "rescue (ems) academy": 19, "coastal rescue school": 24,
}


@dataclass(frozen=True)
class ExistingBuilding:
    building_type_id: int | None
    latitude: float | None
    longitude: float | None
    building_id: int | None


def _first(rec: dict, *keys: str) -> Any:
    for key in keys:
        if key in rec and rec[key] is not None:
            return rec[key]
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_api_buildings(
    raw: str | list | dict, *, require_coordinates: bool = True
) -> list[ExistingBuilding]:
    """Parse ``/api/buildings`` (a JSON list, or a dict wrapping one).

    ``require_coordinates`` drops records without a usable lat/lon — the
    proximity dedup needs them. Enumeration-only callers (the trainings
    service listing academies by type-id) pass ``False``: a missing
    coordinate must not hide an academy from them.
    """
    data = json.loads(raw) if isinstance(raw, str) else raw
    items: Any = data
    if isinstance(data, dict):
        items = None
        for key in ("buildings", "result", "data"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None:
            return []
    out: list[ExistingBuilding] = []
    for rec in items or []:
        if not isinstance(rec, dict):
            continue
        lat = _to_float(_first(rec, "latitude", "lat"))
        lon = _to_float(_first(rec, "longitude", "lon", "lng"))
        if require_coordinates and (lat is None or lon is None):
            continue
        out.append(
            ExistingBuilding(
                building_type_id=_to_int(
                    _first(rec, "building_type", "building_type_id", "buildingType")
                ),
                latitude=lat,
                longitude=lon,
                building_id=_to_int(_first(rec, "id", "building_id", "buildingId")),
            )
        )
    return out


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    radius_m = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_duplicate(
    latitude: float,
    longitude: float,
    building_type: str,
    existing: list[ExistingBuilding],
    *,
    radius_m: float,
) -> ExistingBuilding | None:
    """The nearest SAME-TYPE existing building within ``radius_m``, if any.

    A building whose type id is unknown is compared too (conservative — we'd
    rather skip a possible duplicate than stack two facilities)."""
    type_id = BUILDING_TYPE_IDS.get(building_type)
    best: ExistingBuilding | None = None
    best_distance = radius_m
    for b in existing:
        if b.latitude is None or b.longitude is None:
            continue
        if type_id is not None and b.building_type_id is not None and b.building_type_id != type_id:
            continue
        distance = haversine_meters(latitude, longitude, b.latitude, b.longitude)
        if distance <= best_distance:
            best_distance = distance
            best = b
    return best
