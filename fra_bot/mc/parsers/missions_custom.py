"""The real "Own mission" model for a large scale alliance mission.

MissionChief's ``/missionAllianceNew`` page (reached *via the game*, with
``?tlat=&tlng=`` coordinates) offers five mission types — four presets and
**Own mission** (``mission_type_id = -1``). Choosing Own mission reveals a
``#custom_mission_creator`` block whose fields are the full required-unit
parameter set:

    mission_position[mission_custom][caption]                       -> name
    mission_position[mission_custom][mission_custom_values][need_lf] -> Firetrucks
    …one field per required unit / possible casualty…
    mission_position[mission_custom][mission_custom_values][patient_extension_id]

The page also carries a **Saved Missions** dropdown: each entry is an
``<a class="mission_custom_saved_restore" params="{…}">`` whose ``params``
JSON holds every value with *bare* keys (``need_lf`` …). Restoring one is a
pure client-side fill of the same inputs, so a saved mission is really just a
custom mission pre-populated with those values — there is no saved-mission id
submitted to the server.

This module is the single source of truth for that model: the field catalog
(with the per-field caps the alliance asked for — 100 everywhere except
``water_needed`` / ``foam_needed`` at 1,000,000), a parser for the Saved
Missions dropdown, and a payload builder that emits the real field names.

Everything here is side-effect free; the scheduler decides when (and whether,
in dry-run) to actually submit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

# Own mission is mission type -1 on /missionAllianceNew.
OWN_MISSION_TYPE_ID = -1

# Field name shapes on the real form (Rails nested params).
CAPTION_FIELD = "mission_position[mission_custom][caption]"
_VALUE_PREFIX = "mission_position[mission_custom][mission_custom_values]"
MISSION_TYPE_FIELD = "mission_position[mission_type_id]"
CAPTION_MAX_LEN = 30

# Per-field caps requested by the alliance: 100 for every required-unit
# field, but the two "water/foam volume" fields go up to 1,000,000.
DEFAULT_CAP = 100
VOLUME_CAP = 1_000_000
VOLUME_KEYS = frozenset({"water_needed", "foam_needed"})
# Hospital department is a dropdown (0-8) rather than a count; clamp to its
# real option range so a stray "100" can't submit an invalid department.
PATIENT_EXTENSION_KEY = "patient_extension_id"
PATIENT_EXTENSION_MAX = 8

# The complete, ordered set of custom-mission value keys, exactly as the
# form renders them (order kept so payloads and help output are stable).
CUSTOM_VALUE_KEYS: tuple[str, ...] = (
    "need_lf", "need_dlk", "need_elw1", "need_elw2", "need_gwgefahrgut",
    "need_rw", "need_gwl2wasser", "need_gwa", "need_arff", "need_boot",
    "need_large_rescue_boat", "need_large_fire_boat", "water_needed",
    "need_fire_investigation", "need_streifenwagen", "need_swat_personal",
    "need_k9", "need_fbi", "need_investigation", "need_mcc", "need_bomb",
    "need_fbi_drone", "need_sheriff", "possible_prisoner", "possible_patient",
    "transport_need_quote", "patient_extension_id", "need_foam", "foam_needed",
    "need_coastal_rescue", "need_coastal_command", "need_coastal_boat",
    "need_large_coastal_boat", "need_coastal_helicopter", "need_coastal_plane",
    "need_brush_truck", "need_elw3", "need_fire_aviation",
    "need_brush_air_command", "need_smoke_jumper_personnel", "need_elw_police",
    "need_detention_unit", "need_riot_police", "need_riot_police_trailer",
    "need_atv_carrier", "need_flood_equipment", "need_light_supply",
    "need_energy_supply", "water_damage_pump_value", "need_search_and_rescue",
    "need_technical_rescue", "possible_crashed_car_min", "possible_crashed_car",
    "possible_crashed_car_large_min", "possible_crashed_car_large", "need_fwk",
    "need_police_drone", "need_mountain_atv", "need_mountain_atv_tractive",
    "need_mountain_height_rescue", "need_mountain_rescue_dogs",
    "need_mountain_snow", "need_mountain_lift", "need_mountain_lift_2",
    "need_hazmat_container", "need_fire_water_carrier_container",
    "need_fire_breathing_protection_container",
    "need_fire_search_and_rescue_container",
    "need_fire_command_and_command_advanced_container",
    "need_fire_water_and_foam_carrier_container",
    "need_fire_flood_equipment_container",
)
_VALID_KEYS = frozenset(CUSTOM_VALUE_KEYS)

# Friendly labels for the common fields (from the in-game form). Unlisted
# keys are humanised on the fly; the real form labels can also be read with
# ``parse_field_labels`` when we have the page.
FIELD_LABELS: dict[str, str] = {
    "need_lf": "Firetrucks",
    "need_dlk": "Platform Trucks",
    "need_elw1": "Battalion Chief Vehicles",
    "need_elw2": "Mobile Command Vehicles",
    "need_gwgefahrgut": "HazMat Vehicles",
    "need_rw": "Heavy Rescue Vehicles",
    "need_gwl2wasser": "Tankers",
    "water_needed": "Water needed",
    "need_foam": "Foam Vehicles",
    "foam_needed": "Foam needed",
    "need_streifenwagen": "Police Cars",
    "need_swat_personal": "SWAT Personnel",
    "need_k9": "K-9 Units",
    "need_arff": "ARFF Vehicles",
    "need_boot": "Boats",
    "need_sheriff": "Sheriff Deputies",
    "possible_patient": "Possible patients",
    "possible_prisoner": "Possible prisoners",
    "patient_extension_id": "Hospital department",
}

# Aliases so a member can type a human word instead of the raw key. The raw
# key is always accepted too.
FIELD_ALIASES: dict[str, str] = {
    "firetrucks": "need_lf", "firetruck": "need_lf", "lf": "need_lf",
    "engines": "need_lf",
    "platform": "need_dlk", "platformtrucks": "need_dlk", "ladder": "need_dlk",
    "dlk": "need_dlk",
    "battalion": "need_elw1", "battalionchief": "need_elw1", "elw1": "need_elw1",
    "command": "need_elw2", "mobilecommand": "need_elw2", "elw2": "need_elw2",
    "hazmat": "need_gwgefahrgut",
    "rescue": "need_rw", "heavyrescue": "need_rw", "rw": "need_rw",
    "tankers": "need_gwl2wasser", "tanker": "need_gwl2wasser",
    "water": "water_needed", "waterneeded": "water_needed",
    "foam": "foam_needed", "foamneeded": "foam_needed",
    "police": "need_streifenwagen", "policecars": "need_streifenwagen",
    "swat": "need_swat_personal", "k9": "need_k9", "dog": "need_k9",
    "boats": "need_boot", "boat": "need_boot",
    "patients": "possible_patient", "patient": "possible_patient",
    "prisoners": "possible_prisoner", "prisoner": "possible_prisoner",
}


class CustomMissionError(ValueError):
    """A custom-mission value or key is unusable."""


def label_for(key: str) -> str:
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    stem = key
    for prefix in ("need_", "possible_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return stem.replace("_", " ").strip().capitalize()


def cap_for(key: str) -> int:
    if key in VOLUME_KEYS:
        return VOLUME_CAP
    if key == PATIENT_EXTENSION_KEY:
        return PATIENT_EXTENSION_MAX
    return DEFAULT_CAP


def resolve_key(name: str) -> str | None:
    """Map a raw key or friendly alias to a canonical value key, else None."""
    raw = (name or "").strip().lower()
    if raw in _VALID_KEYS:
        return raw
    return FIELD_ALIASES.get(raw.replace(" ", "").replace("-", ""))


def clamp_value(key: str, raw: object) -> int:
    """Coerce a value to a non-negative int within the field's cap."""
    try:
        value = int(str(raw).strip() or "0")
    except (TypeError, ValueError):
        raise CustomMissionError(f"'{raw}' is not a whole number for {key}")
    if value < 0:
        value = 0
    return min(value, cap_for(key))


@dataclass
class CustomMission:
    """A member-supplied (or saved) Own mission: a name plus required units.

    ``values`` holds only the non-default (non-zero) fields; everything else
    submits as 0. All values are clamped to their per-field cap.
    """

    caption: str
    values: dict[str, int] = field(default_factory=dict)

    def clamped(self) -> "CustomMission":
        caption = (self.caption or "").strip()[:CAPTION_MAX_LEN]
        if not caption:
            raise CustomMissionError("a mission name is required")
        cleaned: dict[str, int] = {}
        for key, raw in self.values.items():
            canonical = resolve_key(key)
            if canonical is None:
                raise CustomMissionError(f"unknown mission field: {key!r}")
            val = clamp_value(canonical, raw)
            if val:
                cleaned[canonical] = val
        return CustomMission(caption=caption, values=cleaned)

    def summary(self, *, limit: int = 6) -> str:
        """A short 'need_lf=25, need_elw1=6, …' description for Discord."""
        if not self.values:
            return "no required units set"
        items = [
            f"{label_for(k)} {v:,}"
            for k, v in sorted(self.values.items(), key=lambda kv: -kv[1])
        ]
        head = items[:limit]
        more = len(items) - len(head)
        text = ", ".join(head)
        return text + (f" +{more} more" if more > 0 else "")


@dataclass
class SavedMission:
    caption: str
    author: str | None
    values: dict[str, int]

    def to_custom(self) -> CustomMission:
        return CustomMission(caption=self.caption, values=dict(self.values)).clamped()


def _values_from_params(params: dict) -> dict[str, int]:
    values: dict[str, int] = {}
    for key, raw in params.items():
        if key == "caption" or key not in _VALID_KEYS:
            continue
        try:
            val = int(str(raw).strip() or "0")
        except (TypeError, ValueError):
            continue
        if val:
            values[key] = min(max(val, 0), cap_for(key))
    return values


def parse_saved_missions(html: str) -> list[SavedMission]:
    """Read the Saved Missions dropdown into a list of :class:`SavedMission`.

    Each ``<a class="mission_custom_saved_restore" params="{…}">`` carries a
    JSON blob of every field. The visible text is ``Name (Author)``.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[SavedMission] = []
    for anchor in soup.select("a.mission_custom_saved_restore"):
        raw = anchor.get("params")
        if not raw:
            continue
        try:
            params = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(params, dict):
            continue
        caption = str(params.get("caption") or "").strip()
        if not caption:
            # Fall back to the anchor's leading text line.
            caption = anchor.get_text(" ", strip=True).split("(")[0].strip()
        if not caption:
            continue
        author = None
        text = anchor.get_text(" ", strip=True)
        if "(" in text and text.rstrip().endswith(")"):
            author = text[text.rfind("(") + 1: text.rfind(")")].strip() or None
        out.append(
            SavedMission(caption=caption, author=author, values=_values_from_params(params))
        )
    return out


def _fold_caption(text: str) -> str:
    """Case- and whitespace-insensitive caption form ("[WF]  Wildfire" ==
    "[wf] wildfire") — members retype names by hand."""
    return " ".join((text or "").split()).lower()


def find_saved_mission(html: str, name: str) -> SavedMission | None:
    """Find a saved mission by (case/whitespace-insensitive) caption."""
    wanted = _fold_caption(name)
    if not wanted:
        return None
    matches = parse_saved_missions(html)
    for saved in matches:
        if _fold_caption(saved.caption) == wanted:
            return saved
    # Loose contains-match as a convenience when the exact caption is long.
    for saved in matches:
        if wanted in _fold_caption(saved.caption):
            return saved
    return None


def parse_field_labels(html: str) -> dict[str, str]:
    """Read the real ``<label>`` text for each custom value field, when
    available. Handy for building help output straight from the form."""
    soup = BeautifulSoup(html, "lxml")
    labels: dict[str, str] = {}
    for key in CUSTOM_VALUE_KEYS:
        el = soup.find(attrs={"name": f"{_VALUE_PREFIX}[{key}]"})
        if el is None:
            continue
        label_el = None
        group = el.find_parent(class_="form-group")
        if group is not None:
            label_el = group.find("label")
        if label_el is not None:
            text = label_el.get_text(" ", strip=True).lstrip("* ").strip()
            if text:
                labels[key] = text
    return labels


def value_field_name(key: str) -> str:
    return f"{_VALUE_PREFIX}[{key}]"


def build_custom_mission_payload(
    form,
    custom: CustomMission,
    *,
    latitude: float,
    longitude: float,
    address: str,
) -> list[tuple[str, str]]:
    """Build the POST body for an Own mission from a parsed form + spec.

    Starts from the form's own hidden/default fields (so every custom value
    field, the CSRF token and the position defaults flow through), then:

    * selects Own mission (``mission_type_id = -1``),
    * sets the caption and the supplied required-unit values (clamped),
    * injects the resolved coordinates + address,
    * pins ``coins`` to 0 — the scheduler only ever starts FREE missions.
    """
    custom = custom.clamped()
    merged = dict(getattr(form, "fields", {}) or {})

    merged[MISSION_TYPE_FIELD] = str(OWN_MISSION_TYPE_ID)
    merged[CAPTION_FIELD] = custom.caption

    # Reset every value field to a known state, then apply the overrides, so a
    # stale default from the form can't leak into the submission.
    for key in CUSTOM_VALUE_KEYS:
        merged[value_field_name(key)] = str(custom.values.get(key, 0))

    merged["mission_position[latitude]"] = f"{latitude:.6f}"
    merged["mission_position[longitude]"] = f"{longitude:.6f}"
    merged["mission_position[address]"] = address
    merged.setdefault("mission_position[poi_type]", "0")
    merged.setdefault("mission_position[shape]", "circle")
    merged.setdefault("mission_position[size]", "1")
    merged.setdefault("mission_position[amount]", "1")
    merged["mission_position[coins]"] = "0"

    return [
        (key, value)
        for key, value in merged.items()
        if value != "" or key.endswith("]")
    ]


def parse_custom_values(text: str) -> dict[str, int]:
    """Parse a compact ``need_lf=25 firetrucks:6 water 15000`` spec.

    Accepts ``key=value``, ``key:value`` or ``key value`` pairs separated by
    whitespace, commas or newlines. Keys may be raw field keys or aliases.
    Unknown keys raise so a typo surfaces instead of silently doing nothing.
    """
    import re

    values: dict[str, int] = {}
    if not text:
        return values
    # Tokens of "<word><sep><number>". The key is a whole word (matched
    # greedily so keys ending in digits — need_elw1, need_mountain_lift_2 —
    # aren't split), then an optional =/: or whitespace, then the number.
    for match in re.finditer(
        r"([A-Za-z][A-Za-z0-9_]*)\s*[=:]?\s*(-?\d[\d,]*)", text
    ):
        raw_key = match.group(1).strip()
        canonical = resolve_key(raw_key)
        if canonical is None:
            raise CustomMissionError(f"unknown mission field: {raw_key!r}")
        number = match.group(2).replace(",", "")
        values[canonical] = clamp_value(canonical, number)
    if not values:
        raise CustomMissionError(
            "no recognisable fields — use e.g. `need_lf=25 need_elw1=6 water_needed=15000`"
        )
    return values
