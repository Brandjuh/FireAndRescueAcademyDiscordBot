"""Verify our building-type ids against the real /buildings/new dropdown."""

from fra_bot.mc.browser_builder import BUILDING_TYPE_IDS
from fra_bot.mc.parsers.buildings import parse_building_type_options

# The <select> exactly as served by MissionChief on /buildings/new.
BUILDING_TYPE_SELECT = """
<select id="building_building_type" name="building[building_type]">
<option value=""></option>
<option value="0">Fire station</option>
<option value="13">Fire station (Small station)</option>
<option value="4">Fire academy</option>
<option value="3">Ambulance station</option>
<option value="2">Hospital</option>
<option value="14">Clinic</option>
<option value="5">Police station</option>
<option value="10">Prison</option>
<option value="9">Staging area</option>
</select>
"""


def test_parse_building_type_options():
    options = parse_building_type_options(BUILDING_TYPE_SELECT)
    assert options["hospital"] == "2"
    assert options["prison"] == "10"
    assert options["fire station"] == "0"


def test_our_hardcoded_ids_match_the_form():
    options = parse_building_type_options(BUILDING_TYPE_SELECT)
    for label, expected_id in BUILDING_TYPE_IDS.items():
        assert options.get(label) == expected_id, (
            f"building type id for {label!r} drifted from the live form"
        )


def test_no_select_returns_empty():
    assert parse_building_type_options("<html><body>not logged in</body></html>") == {}
