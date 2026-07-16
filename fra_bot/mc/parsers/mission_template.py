"""The copy-paste "Own mission" template members fill in on the board.

The request guide shows the game's full Own-mission field list — the
labels below, in the exact order of the ``/missionAllianceNew`` form. A
member copies the list, fills in their numbers and posts it; this module
turns that post back into location, mission name and required-unit
values. A line the member deleted (or left at 0) simply means 0, the
transport probability defaults to 50, and the hospital department can be
picked by NAME (default General Internal).
"""

from __future__ import annotations

import re

from .missions_custom import (
    CustomMissionError,
    clamp_value,
    label_for,
)

# (template label, form field key) in the exact order of the game's form.
TEMPLATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Required Firetrucks", "need_lf"),
    ("Required Platform Trucks", "need_dlk"),
    ("Required Battalion Chief Vehicles", "need_elw1"),
    ("Required Mobile Command Vehicles", "need_elw2"),
    ("Required HazMat Vehicles", "need_gwgefahrgut"),
    ("Required Heavy Rescue Vehicles", "need_rw"),
    ("Required Tankers", "need_gwl2wasser"),
    ("Required Mobile Air Vehicles", "need_gwa"),
    ("Required ARFF", "need_arff"),
    ("Required Light boats", "need_boot"),
    ("Required large rescue boats", "need_large_rescue_boat"),
    ("Required large fire boats", "need_large_fire_boat"),
    ("Water needed (in gallons)", "water_needed"),
    ("Required Fire Investigation Units", "need_fire_investigation"),
    ("Required Police Cars", "need_streifenwagen"),
    ("Required SWAT Personal", "need_swat_personal"),
    ("Required K-9 Units", "need_k9"),
    ("Required FBI Units", "need_fbi"),
    ("Required FBI Investigation Wagons", "need_investigation"),
    ("Required FBI Mobile Command Centers", "need_mcc"),
    ("Required FBI Bomb Technician Vehicles", "need_bomb"),
    ("Required FBI Surveillance Drones", "need_fbi_drone"),
    ("Required Police Supervisors / Sheriffs", "need_sheriff"),
    ("Possible Prisoners", "possible_prisoner"),
    ("Possible Patients", "possible_patient"),
    ("Patient transport probability (in percent)", "transport_need_quote"),
    ("Hospital department", "patient_extension_id"),
    ("Required Foam Tenders", "need_foam"),
    ("Foam needed (in gallons)", "foam_needed"),
    ("Required Lifeguard Trucks", "need_coastal_rescue"),
    ("Required Lifeguard Supervisors", "need_coastal_command"),
    ("Required Small Coastal Boats", "need_coastal_boat"),
    ("Required Large Coastal Boats", "need_large_coastal_boat"),
    ("Required Coastal Helicopters", "need_coastal_helicopter"),
    ("Required Coastal Guard Planes", "need_coastal_plane"),
    ("Need Brush truck", "need_brush_truck"),
    ("Required Wildland MCCs", "need_elw3"),
    ("Required airborne firefighting vehicles", "need_fire_aviation"),
    ("Required Wildland Lead Planes", "need_brush_air_command"),
    ("Required Smoke Jumpers", "need_smoke_jumper_personnel"),
    ("Required Police MCV", "need_elw_police"),
    ("Required Police Prisoner Vans", "need_detention_unit"),
    ("Required Riot Police Units", "need_riot_police"),
    ("Required Riot Police Trailers", "need_riot_police_trailer"),
    ("Required Police ATV Trailer", "need_atv_carrier"),
    ("Required Flood Equipment", "need_flood_equipment"),
    ("Required Light Tower Trailers", "need_light_supply"),
    ("Required Energy Supplies", "need_energy_supply"),
    ("Water to Pump", "water_damage_pump_value"),
    ("Required SAR Equipments", "need_search_and_rescue"),
    ("Required Technical Rescue Equipments", "need_technical_rescue"),
    ("Minimum amount of cars to tow", "possible_crashed_car_min"),
    ("Maximum amount of cars to tow", "possible_crashed_car"),
    ("Minimum amount of trucks to tow", "possible_crashed_car_large_min"),
    ("Maximum amount of trucks to tow", "possible_crashed_car_large"),
    ("Required Fire Cranes", "need_fwk"),
    ("Required Police Drones", "need_police_drone"),
    ("Need Mountain Atv", "need_mountain_atv"),
    ("Required ATV", "need_mountain_atv_tractive"),
    ("Required Mountain Height Rescue", "need_mountain_height_rescue"),
    ("Required Mountain Dog", "need_mountain_rescue_dogs"),
    ("Need Mountain Snow", "need_mountain_snow"),
    ("Required Sked", "need_mountain_lift"),
    ("Required Litter", "need_mountain_lift_2"),
    ("Required HazMat", "need_hazmat_container"),
    ("Required WTC", "need_fire_water_carrier_container"),
    ("Required FBPC", "need_fire_breathing_protection_container"),
    ("Required USARC", "need_fire_search_and_rescue_container"),
    ("Required ICPC", "need_fire_command_and_command_advanced_container"),
    ("Required CWFT", "need_fire_water_and_foam_carrier_container"),
    ("Required FWDC", "need_fire_flood_equipment_container"),
)

TRANSPORT_KEY = "transport_need_quote"
DEPARTMENT_KEY = "patient_extension_id"
TRANSPORT_DEFAULT = 50

# Hospital departments as the form's dropdown shows them (id 0-8), taken
# from the reference bot's mappings.
HOSPITAL_DEPARTMENTS: dict[int, str] = {
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().rstrip(":").casefold()


_LABEL_TO_KEY: dict[str, str] = {_norm(label): key for label, key in TEMPLATE_FIELDS}
_KEY_TO_LABEL: dict[str, str] = {key: label for label, key in TEMPLATE_FIELDS}
_DEPT_BY_NAME: dict[str, int] = {
    _norm(name): dept_id for dept_id, name in HOSPITAL_DEPARTMENTS.items()
}
# Longest labels first so an inline value ("Required Firetrucks 5") never
# matches a shorter label that happens to be a prefix (Required HazMat vs
# Required HazMat Vehicles).
_LABELS_BY_LENGTH: tuple[str, ...] = tuple(
    sorted(_LABEL_TO_KEY, key=len, reverse=True)
)

_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")
_INLINE_HEADER_RE = re.compile(r"^(location|name)\s*:\s*(.+)$", re.IGNORECASE)


def render_template() -> str:
    """The copyable request template shown in the board guide."""
    lines = ["Location:", "<location>", "Name:", "<name>"]
    for label, key in TEMPLATE_FIELDS:
        lines.append(label)
        if key == TRANSPORT_KEY:
            lines.append(str(TRANSPORT_DEFAULT))
        elif key == DEPARTMENT_KEY:
            lines.append(HOSPITAL_DEPARTMENTS[0])
        else:
            lines.append("0")
    return "\n".join(lines)


def looks_like_template(content: str) -> bool:
    """True when a post is (a filled-in copy of) the request template:
    it has a Location line plus at least one known field label line."""
    has_location = False
    has_field = False
    for line in str(content or "").splitlines():
        n = _norm(line)
        header = _INLINE_HEADER_RE.match(line.strip())
        if n == "location" or (header and header.group(1).lower() == "location"):
            has_location = True
        elif n in _LABEL_TO_KEY or _split_inline(line) is not None:
            has_field = True
        if has_location and has_field:
            return True
    return False


def _split_inline(line: str) -> tuple[str, str] | None:
    """Match 'Required Firetrucks 5' AND 'Required Firetrucks: 5' — a label
    with the value on the SAME line. Returns (key, value) or None.

    ``_norm`` only strips a TRAILING colon, so the attached-colon form keeps
    its ':' mid-string — requiring ``label + " "`` used to silently drop
    every 'Label: value' requirement."""
    n = _norm(line)
    for label in _LABELS_BY_LENGTH:
        if not n.startswith(label):
            continue
        rest = n[len(label):]
        if not rest or rest[0] not in " :":
            continue  # prefix of a longer label, not this field
        rest = rest.lstrip(" :").strip()
        if rest:
            return _LABEL_TO_KEY[label], rest
    return None


def _department_id(raw: str) -> int:
    n = _norm(raw)
    if n in _DEPT_BY_NAME:
        return _DEPT_BY_NAME[n]
    try:
        dept_id = int(n)
    except ValueError:
        options = ", ".join(HOSPITAL_DEPARTMENTS.values())
        raise CustomMissionError(
            f"unknown hospital department {raw!r} — use one of: {options}"
        )
    if dept_id not in HOSPITAL_DEPARTMENTS:
        raise CustomMissionError(
            f"hospital department must be 0-{max(HOSPITAL_DEPARTMENTS)}, not {dept_id}"
        )
    return dept_id


def _numeric(key: str, raw: str) -> int:
    cleaned = str(raw).strip().rstrip("%").replace(",", "").replace(".", "")
    if not re.fullmatch(r"-?\d+", cleaned or ""):
        label = _KEY_TO_LABEL.get(key, label_for(key))
        raise CustomMissionError(
            f"{raw!r} is not a whole number for '{label}'"
        )
    return clamp_value(key, cleaned)


def parse_template(content: str) -> tuple[str, str, dict[str, int]]:
    """Parse a filled-in template into (location, name, values).

    Values only carry the non-zero fields — a deleted or 0 line submits
    as 0, exactly like the untouched form. Raises
    :class:`CustomMissionError` on an unreadable value (the reply tells
    the member which line to fix)."""
    lines = [line.strip() for line in str(content or "").splitlines()]
    location = ""
    name = ""
    values: dict[str, int] = {}

    def is_label(text: str) -> bool:
        n = _norm(text)
        return n in _LABEL_TO_KEY or n in ("location", "name")

    def take_value(index: int) -> tuple[str, int]:
        """The value line following a label — empty when the member deleted
        it (the next line is already another label, or the post ends)."""
        j = index + 1
        while j < len(lines) and not lines[j]:
            j += 1
        if j >= len(lines) or is_label(lines[j]) or _split_inline(lines[j]):
            return "", index + 1
        return lines[j], j + 1

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue
        inline_header = _INLINE_HEADER_RE.match(line)
        if inline_header:
            value = inline_header.group(2).strip()
            if value and not _PLACEHOLDER_RE.match(value):
                if inline_header.group(1).lower() == "location":
                    location = value
                else:
                    name = value
            i += 1
            continue
        n = _norm(line)
        if n == "location":
            value, i = take_value(i)
            if value and not _PLACEHOLDER_RE.match(value):
                location = value
            continue
        if n == "name":
            value, i = take_value(i)
            if value and not _PLACEHOLDER_RE.match(value):
                name = value
            continue
        key = _LABEL_TO_KEY.get(n)
        raw_value = None
        if key is not None:
            raw_value, i = take_value(i)
        else:
            inline = _split_inline(line)
            if inline is not None:
                key, raw_value = inline
            i += 1
        if key is None or not raw_value or _PLACEHOLDER_RE.match(raw_value):
            continue
        if key == DEPARTMENT_KEY:
            dept_id = _department_id(raw_value)
            if dept_id:
                values[key] = dept_id
            continue
        number = _numeric(key, raw_value)
        if number:
            values[key] = number

    return location, name, values
