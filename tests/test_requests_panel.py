"""The Discord request panel's payload helpers (the pure parts — the
views/buttons are exercised live)."""

from fra_bot.cogs.requests_panel import (
    DISCORD_THREAD,
    building_request_payload,
    training_request_payload,
)


def test_discord_thread_sentinel_is_falsy():
    # is_discord_request() keys off a falsy thread_id.
    assert not DISCORD_THREAD


def test_training_request_payload_matches_board_shape():
    p = training_request_payload(
        "fire", "HazMat", user_id=42, channel_id=7, remind=True
    )
    assert p["trainings"] == [
        {"discipline": "fire", "name": "HazMat", "duration": 3, "count": 1}
    ]
    assert p["ambiguous"] == []
    assert p["discord_user_id"] == 42 and p["channel_id"] == 7
    assert p["remind"] is True
    # Unknown course still builds a payload (duration 0 -> no reminder later).
    p2 = training_request_payload("fire", "Bogus", user_id=1, channel_id=None, remind=False)
    assert p2["trainings"][0]["duration"] == 0


def test_training_request_payload_clamps_the_class_count():
    over = training_request_payload(
        "fire", "HazMat", user_id=1, channel_id=None, remind=False, count=9
    )
    assert over["trainings"][0]["count"] == 4  # MAX_CLASSES_PER_REQUEST
    under = training_request_payload(
        "fire", "HazMat", user_id=1, channel_id=None, remind=False, count=0
    )
    assert under["trainings"][0]["count"] == 1


def test_building_request_payload_requires_maps_link():
    assert building_request_payload("just words", user_id=1, channel_id=None) is None
    p = building_request_payload(
        "here you go https://maps.app.goo.gl/AbCdEf123", user_id=9, channel_id=3
    )
    assert p is not None
    assert "maps.app.goo.gl" in p["link"]
    assert p["discord_user_id"] == 9 and p["channel_id"] == 3
