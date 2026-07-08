"""The corrected Own-mission model: field catalog + caps, the Saved Missions
dropdown parser, and the payload builder against the REAL form field names."""

import pytest

from fra_bot.mc.parsers.events import parse_event_form
from fra_bot.mc.parsers.missions_custom import (
    CAPTION_MAX_LEN,
    CUSTOM_VALUE_KEYS,
    CustomMission,
    CustomMissionError,
    build_custom_mission_payload,
    cap_for,
    clamp_value,
    find_saved_mission,
    parse_custom_values,
    parse_saved_missions,
    resolve_key,
    value_field_name,
)

# A trimmed but faithful /missionAllianceNew form: the Own-mission subtree
# uses mission_position[mission_custom][mission_custom_values][<key>], plus a
# Saved Missions dropdown carrying bare-key params JSON.
FORM_HTML = """
<html><body>
Last free mission: Mon, 01 Jul 2019 12:00:00 -0400
<ul class="dropdown-menu">
  <li><a class="mission_custom_saved_restore " params='{"caption":"Wildfire","need_lf":"100","water_needed":"1200","need_brush_truck":"100"}'>Wildfire (Author1)</a></li>
  <li><a class="mission_custom_saved_restore " params='{"caption":"Motor Vehicle Accident","need_lf":"2","need_rw":"2","possible_patient":"1","transport_need_quote":"75"}'>Motor Vehicle Accident (Bob)</a></li>
</ul>
<form action="/missionAllianceCreate" id="new_mission_position" method="post">
  <input type="hidden" name="authenticity_token" value="tok-1"/>
  <input type="radio" name="mission_position[mission_type_id]" value="41" checked/>
  <input type="radio" name="mission_position[mission_type_id]" value="-1"/>
  <input type="text" name="mission_position[mission_custom][caption]" value=""/>
  <input type="number" name="mission_position[mission_custom][mission_custom_values][need_lf]" value="0"/>
  <input type="number" name="mission_position[mission_custom][mission_custom_values][need_elw1]" value="0"/>
  <input type="number" name="mission_position[mission_custom][mission_custom_values][water_needed]" value="0"/>
  <select name="mission_position[mission_custom][mission_custom_values][patient_extension_id]">
    <option value="0" selected>General Internal</option>
    <option value="4">Traumatology</option>
  </select>
  <input type="hidden" name="mission_position[latitude]" value=""/>
  <input type="hidden" name="mission_position[longitude]" value=""/>
  <input type="hidden" name="mission_position[address]" value=""/>
  <input type="hidden" name="mission_position[poi_type]" value="0"/>
  <input type="hidden" name="mission_position[coins]" value="0"/>
  <button type="submit" id="startMission">Start</button>
</form>
</body></html>
"""


# -- catalog + caps ---------------------------------------------------------

def test_catalog_is_complete_and_ordered():
    assert len(CUSTOM_VALUE_KEYS) == len(set(CUSTOM_VALUE_KEYS))  # no dupes
    assert "need_lf" in CUSTOM_VALUE_KEYS
    assert "patient_extension_id" in CUSTOM_VALUE_KEYS
    assert "need_fire_flood_equipment_container" in CUSTOM_VALUE_KEYS


def test_caps_default_100_volume_1m():
    assert cap_for("need_lf") == 100
    assert cap_for("need_elw1") == 100
    assert cap_for("water_needed") == 1_000_000
    assert cap_for("foam_needed") == 1_000_000
    assert cap_for("patient_extension_id") == 8


def test_clamp_value_respects_caps_and_floor():
    assert clamp_value("need_lf", 999) == 100
    assert clamp_value("need_lf", -5) == 0
    assert clamp_value("water_needed", 500000) == 500000
    assert clamp_value("water_needed", 9_999_999) == 1_000_000
    assert clamp_value("patient_extension_id", 50) == 8
    with pytest.raises(CustomMissionError):
        clamp_value("need_lf", "abc")


def test_resolve_key_accepts_raw_and_alias():
    assert resolve_key("need_lf") == "need_lf"
    assert resolve_key("firetrucks") == "need_lf"
    assert resolve_key("Tankers") == "need_gwl2wasser"
    assert resolve_key("mobile command") == "need_elw2"
    assert resolve_key("nonsense") is None


# -- compact value parsing --------------------------------------------------

def test_parse_custom_values_various_separators():
    v = parse_custom_values("need_lf=25 need_elw1:6, water_needed 15000")
    assert v == {"need_lf": 25, "need_elw1": 6, "water_needed": 15000}


def test_parse_custom_values_keeps_digit_suffixed_keys_intact():
    # need_elw1 / need_mountain_lift_2 must not be split on their trailing digit.
    v = parse_custom_values("need_elw1=2 need_mountain_lift_2=3")
    assert v == {"need_elw1": 2, "need_mountain_lift_2": 3}


def test_parse_custom_values_aliases_and_clamp():
    v = parse_custom_values("firetrucks=999 water=15000")
    assert v == {"need_lf": 100, "water_needed": 15000}


def test_parse_custom_values_unknown_field_raises():
    with pytest.raises(CustomMissionError):
        parse_custom_values("need_lf=1 bogus_field=2")


def test_parse_custom_values_empty_raises():
    with pytest.raises(CustomMissionError):
        parse_custom_values("just some words")


# -- saved missions dropdown ------------------------------------------------

def test_parse_saved_missions():
    saved = parse_saved_missions(FORM_HTML)
    assert len(saved) == 2
    wild = saved[0]
    assert wild.caption == "Wildfire"
    assert wild.author == "Author1"
    assert wild.values["need_lf"] == 100
    assert wild.values["water_needed"] == 1200
    assert wild.values["need_brush_truck"] == 100
    mva = saved[1]
    assert mva.caption == "Motor Vehicle Accident"
    assert mva.values["possible_patient"] == 1


def test_find_saved_mission_case_insensitive_and_contains():
    assert find_saved_mission(FORM_HTML, "wildfire").caption == "Wildfire"
    assert find_saved_mission(FORM_HTML, "motor vehicle").caption == "Motor Vehicle Accident"
    assert find_saved_mission(FORM_HTML, "nope") is None


# -- payload builder --------------------------------------------------------

def test_build_custom_payload_uses_real_field_names():
    form = parse_event_form(FORM_HTML)
    custom = CustomMission(caption="My structure fire", values={"need_lf": 25, "need_elw1": 6})
    body = dict(build_custom_mission_payload(
        form, custom, latitude=42.9634, longitude=-85.6681, address="Grand Rapids"
    ))
    # Own mission selected.
    assert body["mission_position[mission_type_id]"] == "-1"
    # Caption + values on the real nested keys.
    assert body["mission_position[mission_custom][caption]"] == "My structure fire"
    assert body[value_field_name("need_lf")] == "25"
    assert body[value_field_name("need_elw1")] == "6"
    # Unset values submit as 0, not left blank.
    assert body[value_field_name("water_needed")] == "0"
    # Position + free-only guard.
    assert body["mission_position[latitude]"] == "42.963400"
    assert body["mission_position[address]"] == "Grand Rapids"
    assert body["mission_position[coins]"] == "0"


def test_build_custom_payload_clamps_and_requires_caption():
    form = parse_event_form(FORM_HTML)
    body = dict(build_custom_mission_payload(
        form, CustomMission("Big", {"need_lf": 9999, "water_needed": 50000}),
        latitude=1.0, longitude=2.0, address="x",
    ))
    assert body[value_field_name("need_lf")] == "100"          # capped
    assert body[value_field_name("water_needed")] == "50000"   # under 1M cap
    with pytest.raises(CustomMissionError):
        build_custom_mission_payload(
            form, CustomMission("", {"need_lf": 1}),
            latitude=1.0, longitude=2.0, address="x",
        )


def test_caption_truncated_to_form_limit():
    long = "x" * 100
    cm = CustomMission(caption=long, values={"need_lf": 1}).clamped()
    assert len(cm.caption) == CAPTION_MAX_LEN


def test_saved_mission_round_trips_into_payload():
    form = parse_event_form(FORM_HTML)
    saved = find_saved_mission(FORM_HTML, "Wildfire")
    body = dict(build_custom_mission_payload(
        form, saved.to_custom(), latitude=1.0, longitude=2.0, address="x"
    ))
    assert body["mission_position[mission_type_id]"] == "-1"
    assert body["mission_position[mission_custom][caption]"] == "Wildfire"
    assert body[value_field_name("need_lf")] == "100"
    assert body[value_field_name("need_brush_truck")] == "100"
