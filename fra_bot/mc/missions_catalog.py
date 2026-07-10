"""The MissionChief mission catalog (``/einsaetze.json``).

Fetching, normalising and classifying every mission the game can generate,
for the missions-database forum. All labels are English; the mapping tables
mirror the reference bot's ``missionsdatabase`` cog (LSSM-derived data).

The payload has shipped in three shapes over time — a list of missions, a
``{"missions": [...]}`` wrapper, and a dict keyed by mission id — so
:func:`normalize_missions` accepts all three. Mission identity is
:func:`mission_key`: ``base_id/overlay-slug`` for additive-overlay variants,
otherwise the plain id (which itself may be a ``"644-0"`` hyphen variant).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

EINSAETZE_PATH = "/einsaetze.json"

# Bump to force a re-render of every forum post after format changes: the
# version is hashed together with the raw mission data, so a bump makes
# every stored content_hash stale at once.
FORMAT_VERSION = "missions-forum-v1"

# average_credits at or above this gets the "High Credits" tag.
HIGH_CREDITS_THRESHOLD = 10_000

HYPHEN_VARIANT_RE = re.compile(r"^(\d+)-(\d+)$")

# ---------------------------------------------------------------------------
# Mapping tables (ported from the reference missionsdatabase cog)
# ---------------------------------------------------------------------------

BUILDINGS: dict[int, str] = {
    0: "Fire Station",
    1: "Dispatch Center",
    2: "Hospital",
    3: "Ambulance Station",
    4: "Fire Academy",
    5: "Police Station",
    6: "Medical Helicopter Station",
    7: "Police Academy",
    8: "Police Aviation",
    9: "Staging Area",
    10: "Prison",
    11: "Fire Boat Dock",
    12: "Rescue Boat Dock",
    13: "Fire Station (Small Station)",
    14: "Clinic",
    15: "Police Station (Small Station)",
    16: "Ambulance Station (Small Station)",
    17: "Firefighting Plane Station",
    18: "Federal Police Station",
    19: "Rescue (EMS) Academy",
    22: "Fire Marshal's Office",
    23: "Coastal Rescue Station",
    24: "Coastal Rescue School",
    25: "Coastal Air Station",
    26: "Lifeguard Post",
    27: "Tow Truck Station",
}

EQUIPMENT: dict[str, str] = {
    "breathing_protection": "Mobile Air Equipment",
    "flood_equipment": "Flood Equipment",
    "fire_rescue": "Heavy Rescue Equipment",
    "hose": "Water Tank Equipment",
    "light_supply": "Light Tower Equipment",
    "hazmat": "HazMat Equipment",
    "energy_supply": "Energy Generator Equipment",
    "foam_carrier": "Foam Tank Equipment",
    "fire_engine": "Fire Hose Equipment",
    "wildfire_engine": "Wildland Fire Engine Equipment",
    "search_and_rescue": "Search and Rescue Equipment",
    "technical_rescue": "Technical Rescue Equipment",
    "fire_water_carrier": "Small Portable Pond",
    "fire_water_carrier_2": "Medium Portable Pond",
    "fire_water_carrier_3": "Large Portable Pond",
    "fire_ladder": "Ladder Rack",
    "water_rescue_boat": "Swift Water Rescue Boat",
    "fire_command_advanced": "Radio Equipment",
    "fire_crane": "Fire Crane",
}

TRAININGS: dict[str, str] = {
    "gw_gefahrgut": "HazMat",
    "elw2": "Mobile Command",
    "arff": "ARFF-Training",
    "gw_wasserrettung": "Swift Water Rescue",
    "ocean_navigation": "Ocean Navigation",
    "airborne_firefighting": "Airborne Firefighting",
    "heavy_machinery": "Heavy Machinery Operating",
    "truck_drivers_license": "Truck Driver's License",
    "ambulance_fire_truck": "ALS Medical Training for Fire Apparatus",
    "ambulance_police_car": "Tactical Medic Training",
    "ems_mobile_command": "EMS Mobile Command",
    "fire_investigator": "Law Enforcement for Arson Investigation",
    "coastal_command": "Lifeguard Supervisor",
    "coastal_rescue": "Lifeguard Training",
    "brush_air_command": "Wildland Lead Pilot Training",
    "elw3": "Wildland Mobile Command Center Training",
    "hotshot": "Hotshot Crew Training",
    "smoke_jumper": "Smoke Jumper Training",
    "traffic_control": "Traffic Control Training",
    "search_and_rescue": "Search and Rescue Training",
    "technical_rescue": "Technical Rescue Training",
    "critical_care": "Critical Care",
    "polizeihubschrauber": "Police Aviation",
    "swat": "SWAT",
    "k9": "K-9",
    "police_motorcycle": "Police Motorcycle",
    "fbi_mcc": "FBI Mobile Center Commander",
    "fbi_bomb_tech": "FBI Bomb Technician",
    "fbi_drone_operator": "FBI Drone Operator",
    "sheriff": "Police Supervisor / Sheriff",
    "game_warden": "Environmental Game Warden",
    "riot_police": "Riot Police Training",
    "elw_police": "Police Operations Management",
    "tactical_medic": "Tactical Rescue Training",
    "sniper": "Sharpshooter Training",
    "coastal_rescue_pilot": "Coastal Air Rescue Operations",
    "law_enforcement_marine": "Law Enforcement Marine (TACLET)",
}

EXTENSION_NAMES: dict[str, str] = {
    "airport_extension": "Airport Extension",
    "water_rescue_extension": "Water Rescue Extension",
    "forestry_expansion": "Forestry Expansion",
    "fire_investigation_extension": "Fire Investigation Extension",
    "foam_extension": "Foam Extension",
    "lifeguard_extension": "Lifeguard Extension",
    "wildland_command_extension": "Wildland Command Extension",
    "flood_control_extension": "Flood Control Extension",
    "disaster_response_count": "Disaster Response Extension",
    "disaster_response_extension": "Disaster Response Extension",
    "traffic_control_extension": "Traffic Control Extension",
    "tow_truck_extension": "Tow Truck Extension",
    "tow_trucks": "Tow Truck Extension",
    "water_police_extension": "Water Police Extension",
    "game_warden_office": "Game Warden Office",
    "k9_carrier_extension": "K9 Carrier Extension",
    "riot_police_extension": "Riot Police Extension",
    "detention_unit_extension": "Detention Unit Extension",
    "federal_police_extension": "Federal Police Extension",
    "bomb_squad_extension": "Bomb Squad Extension",
    "bomb_disposal_count": "Bomb Squad Extension",
    "police_water_rescue": "Police Water Rescue Extension",
    "smoke_jumper_extension": "Smoke Jumper Extension",
    "atf_expansion": "ATF Expansion",
    "dea_expansion": "DEA Expansion",
    "ambulance_extension": "Ambulance Extension",
    "mass_casualty_trailer_extension": "Mass Casualty Trailer Extension",
}

VEHICLE_LABELS: dict[str, str] = {
    "firetrucks": "Fire Trucks",
    "platform_trucks": "Platform Trucks",
    "battalion_chief_vehicles": "Battalion Chief Vehicles",
    "heavy_rescue_vehicles": "Heavy Rescue Vehicles",
    "police_cars": "Patrol Cars",
    "k9": "K-9 Units",
    "k9_units": "K-9 Units",
    "ambulances": "Ambulances",
    "fly_cars": "Fly-Cars",
    "mobile_air_vehicles": "Mobile Air",
    "water_tankers": "Water Tankers",
    "utility_vehicles": "Utility Units",
    "hazmat_vehicles": "HazMat",
    "quints": "Quints",
    "rescue_engines": "Rescue Engines",
    "swat_vehicles": "SWAT Vehicles",
    "swat_armoured_vehicles": "SWAT Armoured Vehicles",
    "swat_suvs": "SWAT SUVs",
    "police_motorcycles": "Police Motorcycles",
    "sheriff_units": "Sheriff Units",
    "mass_casualty_units": "Mass Casualty Units",
    "ems_chiefs": "EMS Chiefs",
    "mobile_command_vehicles": "Mobile Command Vehicles",
    "hems": "HEMS",
    "police_helicopters": "Police Helicopters",
    "fbi_units": "FBI Units",
    "fbi_investigation_wagons": "FBI Investigation Wagons",
    "fbi_mobile_command_centers": "FBI Mobile Command Centers",
    "fbi_bomb_technician_vehicles": "FBI Bomb Technician Vehicles",
    "fbi_surveillance_drones": "FBI Surveillance Drones",
    "dea_units": "DEA Units",
    "dea_clan_labs": "DEA Clan Labs",
    "atf_units": "ATF Units",
    "atf_lab_vehicles": "ATF Lab Vehicles",
    "patrol_boats": "Patrol Boats",
    "wardens_trucks": "Warden's Trucks",
    "riot_police_vans": "Riot Police Vans",
    "riot_police_buses": "Riot Police Buses",
    "police_prisoner_vans": "Police Prisoner Vans",
    "tow_trucks": "Tow Trucks",
    "wreckers": "Wreckers",
    "flatbed_carriers": "Flatbed Carriers",
    # German/European abbreviations that leak through from Leitstellenspiel.
    "elw2": "Mobile Command Vehicle",
    "elw3": "Wildfire MCC",
    "gw_gefahrgut": "HazMat",
    "gw_wasserrettung": "Heavy Rescue + Light Boat",
    "fwk": "Crew Carrier",
    "gwm": "Mobile Air",
    "rw": "Heavy Rescue Vehicle",
    "dlk": "Platform Truck",
}

HOSPITAL_SPECIALIZATIONS: dict[int, str] = {
    0: "General Internal",
    1: "General Surgeon",
    2: "Gynecology",
    3: "Urology",
    4: "Traumatology",
    5: "Neurology",
    6: "Neurosurgery",
    7: "Cardiology",
    8: "Cardiac Surgery",
}

# ---------------------------------------------------------------------------
# Forum tags: 9 disciplines + 9 attributes (18 of Discord's 20 tag slots).
# Names must stay ≤ 20 characters (Discord limit); max 5 applied per post.
# ---------------------------------------------------------------------------

DISCIPLINE_TAG_EMOJI: dict[str, str] = {
    "Fire": "🔥",
    "EMS": "🚑",
    "Police": "🚓",
    "Federal": "🏛️",
    "Water": "🌊",
    "Wildfire": "🌲",
    "HazMat": "☣️",
    "Technical": "🛠️",
    "Airport": "✈️",
}

ATTRIBUTE_TAG_EMOJI: dict[str, str] = {
    "Patients": "🧑‍⚕️",
    "Prisoners": "🚔",
    "Towing": "🧲",
    "Training Needed": "🎓",
    "Unlock Needed": "🔓",
    "POI Only": "📍",
    "High Credits": "💰",
    "Variation": "🔁",
    "Event": "🎪",
}

FORUM_TAG_EMOJI: dict[str, str] = {**DISCIPLINE_TAG_EMOJI, **ATTRIBUTE_TAG_EMOJI}

CATEGORY_TO_DISCIPLINE: dict[str, str] = {
    "fire": "Fire",
    "urban": "Fire",
    "rural": "Fire",
    "ems": "EMS",
    "ambulance": "EMS",
    "mass_casualty": "EMS",
    "police": "Police",
    "swat": "Police",
    "riot_police_specialization": "Police",
    "game_warden_specialization": "Police",
    "federal_police": "Federal",
    "water_damage_and_flood": "Water",
    "water_damage_and_flood_specialization": "Water",
    "water_rescue": "Water",
    "water_rescue_specialization": "Water",
    "flood": "Water",
    "coastal_rescue": "Water",
    "coastal_rescue_ocean": "Water",
    "water_police_specialization": "Water",
    "marine": "Water",
    "wildfire": "Wildfire",
    "forestry": "Wildfire",
    "forestry_specialization": "Wildfire",
    "forest": "Wildfire",
    "hazmat": "HazMat",
    "fire_support_specialization": "HazMat",
    "technical_rescue": "Technical",
    "tow_trucks": "Technical",
    "tow_trucks_only": "Technical",
    "mountain": "Technical",
    "highway": "Technical",
    "aviation": "Airport",
    "airport": "Airport",
}


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------

async def fetch_catalog(client) -> list[dict[str, Any]]:
    """Fetch and normalise the full mission catalog (paced, logged-in)."""
    text = await client.fetch_page(EINSAETZE_PATH)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError(
            f"einsaetze.json did not parse as JSON: {exc}"
        ) from exc
    missions = normalize_missions(payload)
    add_related_mission_names(missions)
    return missions


def normalize_missions(payload: Any) -> list[dict[str, Any]]:
    """Accept the three payload shapes; every mission gets an ``id``."""
    if isinstance(payload, list):
        missions = [dict(m) for m in payload if isinstance(m, dict)]
        for index, mission in enumerate(missions):
            mission.setdefault("id", str(index))
        return missions
    if isinstance(payload, dict):
        if isinstance(payload.get("missions"), list):
            return normalize_missions(payload["missions"])
        missions = []
        for mission_id, data in payload.items():
            if not isinstance(data, dict):
                continue
            mission = dict(data)
            mission.setdefault("id", str(mission_id))
            missions.append(mission)
        return missions
    raise ValueError(f"Unexpected einsaetze.json payload: {type(payload)!r}")


def add_related_mission_names(missions: list[dict[str, Any]]) -> None:
    """Resolve ``expansion_missions_ids`` to names (stored on ``additional``
    so a renamed expansion target also refreshes this mission's post)."""
    names_by_id: dict[str, str] = {}
    for mission in missions:
        for id_field in ("id", "base_mission_id"):
            value = mission.get(id_field)
            if value not in (None, ""):
                names_by_id.setdefault(str(value), mission_name(mission))
    for mission in missions:
        additional = mission.get("additional")
        if not isinstance(additional, dict):
            continue
        ids = additional.get("expansion_missions_ids") or []
        if ids:
            additional["expansion_mission_names"] = [
                names_by_id.get(str(mid), f"Mission {mid}") for mid in ids
            ]


def mission_key(mission: dict[str, Any]) -> str:
    """Stable identity: ``base/overlay`` → ``id`` → ``base`` → name slug."""
    overlay = str(mission.get("additive_overlays") or "").strip().lower()
    base_id = mission.get("base_mission_id")
    mission_id = mission.get("id")
    if base_id not in (None, "") and overlay:
        return f"{base_id}/{overlay}"
    if mission_id not in (None, ""):
        return str(mission_id)
    if base_id not in (None, ""):
        return str(base_id)
    slug = re.sub(r"[^a-z0-9]+", "-", str(mission.get("name") or "unknown").lower())
    return slug.strip("-") or "unknown"


def mission_name(mission: dict[str, Any]) -> str:
    return str(mission.get("name") or mission.get("caption") or "Unknown Mission")


def detail_url(mission: dict[str, Any], base_url: str) -> str:
    """The mission's page on MissionChief (variants keep their base page)."""
    base_url = base_url.rstrip("/")
    mission_id = str(mission.get("id") or "")
    base_id = mission.get("base_mission_id")
    overlay = str(mission.get("additive_overlays") or "").strip()
    if overlay and base_id not in (None, ""):
        return f"{base_url}/einsaetze/{base_id}"
    hyphen = HYPHEN_VARIANT_RE.match(mission_id)
    if hyphen:
        return f"{base_url}/einsaetze/{hyphen.group(1)}"
    return f"{base_url}/einsaetze/{mission_id}"


def content_hash(mission: dict[str, Any]) -> str:
    """SHA-256 over the canonical mission JSON + format version, so both a
    data change and a formatter bump mark the post as needing an edit."""
    payload = {"format_version": FORMAT_VERSION, "mission": mission}
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field helpers (English labels)
# ---------------------------------------------------------------------------

def format_field_name(field_name: str) -> str:
    return field_name.replace("_", " ").title()


def _empty(value: Any) -> bool:
    return value in (None, "", 0, [], {})


def requirement_label(key: str) -> str:
    if key in EQUIPMENT or "equipment" in key.casefold():
        return EQUIPMENT.get(key, format_field_name(key))
    return VEHICLE_LABELS.get(key, format_field_name(key))


def prerequisite_label(key: str) -> str:
    return EXTENSION_NAMES.get(key, format_field_name(key))


def requirement_lines(mission: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(vehicle/equipment lines, training lines) from ``requirements``.

    Scalars are counts; ``"oneof X or Y"`` strings are alternatives; a
    nested dict is the trained-personnel sub-map.
    """
    requirements = mission.get("requirements") or {}
    vehicles: list[str] = []
    trainings: list[str] = []
    for key, value in requirements.items():
        if _empty(value):
            continue
        if isinstance(value, dict):
            for training_key, count in value.items():
                label = TRAININGS.get(
                    str(training_key), format_field_name(str(training_key))
                )
                trainings.append(f"• {count}× {label}")
            continue
        key_s = str(key)
        text = str(value)
        if "oneof" in text.casefold():
            vehicles.append(
                "• " + text.replace("oneof", "one of").replace(" or ", " OR ")
            )
        elif key_s == "water_needed":
            try:
                vehicles.append(f"• Water needed: {int(value):,} gallons")
            except (TypeError, ValueError):
                vehicles.append(f"• Water needed: {value}")
        else:
            try:
                vehicles.append(f"• {int(value):,}× {requirement_label(key_s)}")
            except (TypeError, ValueError):
                vehicles.append(f"• {value} {requirement_label(key_s)}")
    return vehicles, trainings


def unlock_lines(mission: dict[str, Any]) -> list[str]:
    """Prerequisites other than the generating building (unlock gates)."""
    prerequisites = mission.get("prerequisites") or {}
    lines: list[str] = []
    for key, value in prerequisites.items():
        if key == "main_building" or _empty(value) or isinstance(value, dict):
            continue
        lines.append(f"• {value}× {prerequisite_label(str(key))}")
    return lines


def generated_by(mission: dict[str, Any]) -> str:
    explicit = mission.get("generated_by")
    if explicit:
        return str(explicit)
    building = (mission.get("prerequisites") or {}).get("main_building")
    if building in (None, ""):
        return "Unknown"
    try:
        building = int(building)
    except (TypeError, ValueError):
        return str(building)
    if building == -1:
        return "Any station"
    return BUILDINGS.get(building, f"Building #{building}")


def patient_summary(mission: dict[str, Any]) -> str:
    additional = mission.get("additional") or {}
    chances = mission.get("chances") or {}
    patients = additional.get("possible_patient") or 0
    try:
        patients = int(patients)
    except (TypeError, ValueError):
        patients = 0
    if patients <= 0:
        return ""
    parts = [f"Up to {patients}"]
    transport = chances.get("patient_transport")
    if transport not in (None, ""):
        parts.append(f"transport chance {transport}%")
    departments = additional.get("patient_specialization_captions") or []
    if departments:
        parts.append("departments: " + ", ".join(str(d) for d in departments))
    return " · ".join(parts)


def prisoner_summary(mission: dict[str, Any]) -> str:
    additional = mission.get("additional") or {}
    low = additional.get("min_possible_prisoners")
    high = additional.get("max_possible_prisoners")
    if low not in (None, "") or high not in (None, ""):
        low = low if low not in (None, "") else 0
        high = high if high not in (None, "") else low
        if str(low) == str(high):
            return f"{high}" if str(high) != "0" else ""
        return f"{low}–{high}"
    count = additional.get("possible_prisoner_count")
    if count not in (None, "", 0):
        return f"Up to {count}"
    return ""


def towing_summary(mission: dict[str, Any]) -> str:
    additional = mission.get("additional") or {}
    low = additional.get("possible_crashed_car_min")
    high = additional.get("possible_crashed_car_max")
    if low in (None, "") and high in (None, ""):
        return ""
    low = low if low not in (None, "") else 0
    high = high if high not in (None, "") else low
    if str(high) == "0":
        return ""
    if str(low) in ("0", str(high)):
        prefix = f"Up to {high}" if str(low) == "0" else f"{high}"
        return f"{prefix} vehicle(s) to tow"
    return f"{low}–{high} vehicle(s) to tow"


def poi_list(mission: dict[str, Any]) -> list[str]:
    places = mission.get("place_array") or []
    if not places and mission.get("place"):
        places = [mission["place"]]
    return [str(p) for p in places if str(p).strip()]


def expansion_names(mission: dict[str, Any]) -> list[str]:
    additional = mission.get("additional") or {}
    names = additional.get("expansion_mission_names") or []
    if names:
        return [str(n) for n in names]
    ids = additional.get("expansion_missions_ids") or []
    return [f"Mission {mid}" for mid in ids]


# ---------------------------------------------------------------------------
# Tag derivation
# ---------------------------------------------------------------------------

def _has_training_requirement(mission: dict[str, Any]) -> bool:
    return any(
        isinstance(v, dict) and v
        for v in (mission.get("requirements") or {}).values()
    )


def _has_unlock_requirement(mission: dict[str, Any]) -> bool:
    return bool(unlock_lines(mission))


def _is_variation(mission: dict[str, Any]) -> bool:
    if str(mission.get("additive_overlays") or "").strip():
        return True
    return bool(HYPHEN_VARIANT_RE.match(str(mission.get("id") or "")))


def _is_event(mission: dict[str, Any]) -> bool:
    categories = [str(c).casefold() for c in mission.get("mission_categories") or []]
    if any("event" in c for c in categories):
        return True
    return "event" in str(mission.get("generated_by") or "").casefold()


def derive_tags(mission: dict[str, Any]) -> list[str]:
    """Tag names for a mission, at most 5 (Discord's per-post limit):
    disciplines first (max 2), then Patients/Prisoners, then the rest."""
    categories = [str(c) for c in mission.get("mission_categories") or []]
    mapped = {CATEGORY_TO_DISCIPLINE.get(c) for c in categories}
    disciplines = [tag for tag in DISCIPLINE_TAG_EMOJI if tag in mapped][:2]

    attributes: list[str] = []
    if patient_summary(mission):
        attributes.append("Patients")
    if prisoner_summary(mission):
        attributes.append("Prisoners")
    if towing_summary(mission):
        attributes.append("Towing")
    if _has_training_requirement(mission):
        attributes.append("Training Needed")
    if _has_unlock_requirement(mission):
        attributes.append("Unlock Needed")
    if poi_list(mission):
        attributes.append("POI Only")
    try:
        if int(mission.get("average_credits") or 0) >= HIGH_CREDITS_THRESHOLD:
            attributes.append("High Credits")
    except (TypeError, ValueError):
        pass
    if _is_variation(mission):
        attributes.append("Variation")
    if _is_event(mission):
        attributes.append("Event")

    return (disciplines + attributes)[:5]


def discipline_of(mission: dict[str, Any]) -> str | None:
    """The mission's primary discipline (drives the embed colour)."""
    mapped = {
        CATEGORY_TO_DISCIPLINE.get(str(c))
        for c in mission.get("mission_categories") or []
    }
    for tag in DISCIPLINE_TAG_EMOJI:
        if tag in mapped:
            return tag
    return None
