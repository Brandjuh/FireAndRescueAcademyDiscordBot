"""The copy-paste Own-mission template: rendering, parsing a filled-in
copy, deleted lines as 0, department by name, and the board-parser hook."""

import pytest

from fra_bot.mc.parsers.mission_template import (
    HOSPITAL_DEPARTMENTS,
    TEMPLATE_FIELDS,
    looks_like_template,
    parse_template,
    render_template,
)
from fra_bot.mc.parsers.missions_custom import (
    CUSTOM_VALUE_KEYS,
    CustomMissionError,
)
from fra_bot.mc.parsers.mission_spec import (
    MissionSpecError,
    is_mission_post,
    parse_board_request,
)


def test_template_covers_every_form_field_in_order():
    # The template IS the game's form: same keys, same order, nothing
    # missing — this breaks loudly if either side changes.
    assert tuple(key for _, key in TEMPLATE_FIELDS) == CUSTOM_VALUE_KEYS


def test_render_template_shows_labels_and_defaults():
    text = render_template()
    lines = text.splitlines()
    assert lines[0] == "Location:"
    assert lines[2] == "Name:"
    for label, _ in TEMPLATE_FIELDS:
        assert label in lines
    # The two non-zero defaults.
    idx = lines.index("Patient transport probability (in percent)")
    assert lines[idx + 1] == "50"
    idx = lines.index("Hospital department")
    assert lines[idx + 1] == "General Internal"


def test_untouched_template_with_location_and_name_parses():
    filled = render_template().replace(
        "<location>", "Grand Rapids"
    ).replace("<name>", "Big fire")
    location, name, values = parse_template(filled)
    assert location == "Grand Rapids"
    assert name == "Big fire"
    # Everything at its default: only the transport probability is non-zero.
    assert values == {"transport_need_quote": 50}


def test_filled_values_and_department_by_name():
    text = "\n".join([
        "Location:",
        "Rome, Italy",
        "Name:",
        "Airport crash",
        "Required Firetrucks",
        "25",
        "Water needed (in gallons)",
        "15,000",
        "Patient transport probability (in percent)",
        "80%",
        "Hospital department",
        "Traumatology",
        "Possible Patients",
        "12",
    ])
    location, name, values = parse_template(text)
    assert location == "Rome, Italy"
    assert name == "Airport crash"
    assert values == {
        "need_lf": 25,
        "water_needed": 15000,
        "transport_need_quote": 80,
        "patient_extension_id": 4,       # Traumatology
        "possible_patient": 12,
    }


def test_deleted_lines_count_as_zero():
    # Member kept only three fields; every deleted field submits as 0.
    text = "\n".join([
        "Location: Berlin",
        "Name: Warehouse fire",
        "Required Firetrucks",
        "10",
        "Required Platform Trucks",     # value line deleted -> 0
        "Required Battalion Chief Vehicles",
        "2",
    ])
    _, _, values = parse_template(text)
    assert values == {"need_lf": 10, "need_elw1": 2}


def test_inline_values_and_headers_parse():
    text = "\n".join([
        "Location: Tokyo",
        "Name: Refinery",
        "Required Firetrucks 12",
        "Required HazMat 3",             # container field, prefix of HazMat Vehicles
        "Required HazMat Vehicles 4",
    ])
    _, _, values = parse_template(text)
    assert values == {
        "need_lf": 12,
        "need_hazmat_container": 3,
        "need_gwgefahrgut": 4,
    }


def test_inline_values_with_attached_colon_parse():
    # 'Label: value' (colon glued to the label) used to be silently dropped —
    # the requirement never reached the started mission.
    text = "\n".join([
        "Location: Tokyo",
        "Name: Refinery",
        "Required Firetrucks: 12",
        "Required HazMat:3",
        "Required HazMat Vehicles : 4",
    ])
    _, _, values = parse_template(text)
    assert values == {
        "need_lf": 12,
        "need_hazmat_container": 3,
        "need_gwgefahrgut": 4,
    }


def test_bad_number_names_the_line():
    text = "Location: Oslo\nName: X\nRequired Firetrucks\nveel"
    with pytest.raises(CustomMissionError) as exc:
        parse_template(text)
    assert "Required Firetrucks" in str(exc.value)


def test_bad_department_lists_the_options():
    text = "Location: Oslo\nName: X\nHospital department\nPediatrics"
    with pytest.raises(CustomMissionError) as exc:
        parse_template(text)
    assert "General Internal" in str(exc.value)
    assert len(HOSPITAL_DEPARTMENTS) == 9


def test_looks_like_template_is_strict():
    assert looks_like_template(render_template())
    assert looks_like_template("Location: X\nRequired Firetrucks\n5")
    # Normal posts are untouched.
    assert not looks_like_template("New York City")
    assert not looks_like_template("Amsterdam\ncustom: need_lf=25")
    assert not looks_like_template("location: Berlin")  # location alone


# -- board parser hook -------------------------------------------------------

def test_board_request_accepts_template_as_custom_mission():
    filled = render_template().replace(
        "<location>", "Grand Rapids"
    ).replace("<name>", "Big fire")
    spec = parse_board_request(filled, default_kind="large")
    assert spec.kind == "large" and spec.source == "custom"
    assert spec.location_text == "Grand Rapids"
    assert spec.custom.caption == "Big fire"
    assert spec.custom.values["transport_need_quote"] == 50


def test_template_on_event_board_still_becomes_large_custom():
    filled = render_template().replace(
        "<location>", "Rome"
    ).replace("<name>", "Colosseum fire")
    spec = parse_board_request(filled, default_kind="event")
    assert spec.kind == "large" and spec.source == "custom"


def test_template_recurring_line_joins_rotation():
    filled = render_template().replace("<location>", "Oslo").replace(
        "<name>", "Harbour fire"
    ) + "\nschedule: recurring"
    spec = parse_board_request(filled, default_kind="large")
    assert spec.recurring is True


def test_template_without_location_reports_it():
    filled = render_template().replace("<name>", "No place")
    with pytest.raises(MissionSpecError) as exc:
        parse_board_request(filled, default_kind="large")
    assert "Location" in str(exc.value)


def test_template_counts_as_mission_post_for_shared_threads():
    filled = render_template().replace("<location>", "Oslo")
    assert is_mission_post(filled)
    assert not is_mission_post("just a location line")
