from fra_bot.cogs.display import affected_url, profile_url


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
