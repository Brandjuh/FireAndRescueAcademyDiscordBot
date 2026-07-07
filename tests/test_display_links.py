from fra_bot.cogs.display import (
    affected_url,
    format_log_description,
    profile_url,
)


def test_profile_url():
    assert profile_url(555) == "https://www.missionchief.com/profile/555"
    assert profile_url(None) is None
    assert profile_url(0) is None


def test_affected_url_user_links_to_profile():
    # The "added to alliance" case: affected is the new member.
    assert affected_url("user", 555) == "https://www.missionchief.com/profile/555"


def test_affected_url_defaults_to_profile_when_type_missing():
    assert affected_url(None, 555) == "https://www.missionchief.com/profile/555"


def test_affected_url_other_entities():
    assert affected_url("building", 777) == "https://www.missionchief.com/buildings/777"
    assert affected_url("mission", 12) == "https://www.missionchief.com/missions/12"
    assert affected_url("vehicle", 9) == "https://www.missionchief.com/vehicles/9"


def test_affected_url_without_id_is_none():
    assert affected_url("user", None) is None
    assert affected_url("user", 0) is None


def test_course_description_shows_only_course_name():
    # The reported case: title already says "Course created".
    assert (
        format_log_description("created_course", "Created a course (Technical Rescue Training)")
        == "Technical Rescue Training"
    )
    assert format_log_description("course_completed", "Course completed (SWAT)") == "SWAT"


def test_course_description_strips_prefix_without_parens():
    assert format_log_description("created_course", "Created a course: HazMat") == "HazMat"


def test_course_description_keeps_original_when_no_name():
    assert format_log_description("created_course", "Created a course") == "Created a course"


def test_non_course_description_untouched():
    assert (
        format_log_description("contributed_to_alliance", "Contributed to the alliance")
        == "Contributed to the alliance"
    )
    # A parenthetical in a non-course log must be preserved.
    assert (
        format_log_description("building_constructed", "Building constructed (Hospital North)")
        == "Building constructed (Hospital North)"
    )
