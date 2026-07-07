import datetime as dt

from fra_bot.mc.parsers.events import (
    build_event_payload,
    is_free_submit,
    next_free_at,
    parse_event_form,
)

EVENT_FORM_HTML = """
<html><body>
<p>Last free mission: Mon, 06 Jul 2026 14:00</p>
<form action="/missionAllianceCreate" method="post">
  <input type="hidden" name="authenticity_token" value="csrf-1"/>
  <input type="hidden" name="mission_position[latitude]" value=""/>
  <input type="hidden" name="mission_position[longitude]" value=""/>
  <input type="hidden" name="mission_position[address]" value=""/>
  <input type="hidden" name="mission_position[coins]" value="0"/>
  <input type="radio" name="mission_position[mission_type_id]" value="7" checked/>
  <input type="submit" value="Start free mission"/>
</form>
</body></html>
"""


def test_parse_event_form():
    form = parse_event_form(EVENT_FORM_HTML)
    assert form.action == "/missionAllianceCreate"
    assert form.authenticity_token == "csrf-1"
    assert form.fields["mission_position[mission_type_id]"] == "7"
    assert form.submit_value == "Start free mission"
    assert form.last_free_at is not None
    assert form.last_free_at.startswith("2026-07-06T18:00")  # 14:00 EDT -> 18:00 UTC


def test_is_free_submit():
    form = parse_event_form(EVENT_FORM_HTML)
    assert is_free_submit(form)


def test_is_not_free_when_coins():
    html = EVENT_FORM_HTML.replace(
        'name="mission_position[coins]" value="0"',
        'name="mission_position[coins]" value="500"',
    )
    form = parse_event_form(html)
    assert not is_free_submit(form)


def test_is_not_free_when_submit_mentions_coins():
    html = EVENT_FORM_HTML.replace("Start free mission", "Start with coins")
    form = parse_event_form(html)
    assert not is_free_submit(form)


def test_build_event_payload_injects_coordinates():
    form = parse_event_form(EVENT_FORM_HTML)
    payload = build_event_payload(
        form, kind="large", latitude=40.7128, longitude=-74.006, address="NYC"
    )
    keys = dict(payload)
    assert keys["mission_position[latitude]"] == "40.712800"
    assert keys["mission_position[longitude]"] == "-74.006000"
    assert keys["mission_position[address]"] == "NYC"
    assert keys["mission_position[coins]"] == "0"
    assert keys["mission_position[poi_type]"] == "0"  # large default


def test_next_free_at_cooldown():
    # large = 1 day interval
    nxt = next_free_at("large", "2026-07-06T18:00:00+00:00")
    assert nxt is not None
    parsed = dt.datetime.fromisoformat(nxt)
    assert parsed > dt.datetime.fromisoformat("2026-07-07T17:00:00+00:00")


def test_next_free_at_event_is_weekly():
    nxt = next_free_at("event", "2026-07-06T18:00:00+00:00")
    parsed = dt.datetime.fromisoformat(nxt)
    # 7 days later
    assert parsed.day == 13


def test_next_free_at_none_without_last():
    assert next_free_at("large", None) is None
