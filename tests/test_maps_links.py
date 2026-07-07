from fra_bot.geo.maps_links import find_maps_links, is_short_link, parse_maps_url


def test_find_maps_links_in_post():
    text = (
        "Please build here: https://maps.app.goo.gl/AbC123xyz thanks!\n"
        "also https://www.google.com/maps/place/Fire+Station/@40.7128,-74.006,17z"
    )
    links = find_maps_links(text)
    assert len(links) == 2
    assert links[0] == "https://maps.app.goo.gl/AbC123xyz"
    assert "google.com/maps/place" in links[1]


def test_short_link_detection():
    assert is_short_link("https://maps.app.goo.gl/AbC123")
    assert is_short_link("https://goo.gl/maps/xYz9")
    assert not is_short_link("https://www.google.com/maps/@40.7,-74.0,15z")


def test_parse_pin_coordinates_preferred_over_viewport():
    url = (
        "https://www.google.com/maps/place/Somewhere/@40.7128,-74.0060,17z/"
        "data=!3m1!4b1!4m6!3m5!1s0x0:0x0!8m2!3d40.7130000!4d-74.0055000"
    )
    loc = parse_maps_url(url)
    assert loc.has_coordinates
    assert abs(loc.latitude - 40.713) < 1e-6      # !3d pin, not @ viewport
    assert abs(loc.longitude - -74.0055) < 1e-6
    assert loc.place_text == "Somewhere"


def test_parse_viewport_fallback():
    loc = parse_maps_url("https://www.google.com/maps/@51.5074,-0.1278,15z")
    assert loc.has_coordinates
    assert abs(loc.latitude - 51.5074) < 1e-6


def test_parse_query_coordinates():
    loc = parse_maps_url("https://maps.google.com/?q=34.0522,-118.2437")
    assert loc.has_coordinates
    assert abs(loc.latitude - 34.0522) < 1e-6
    assert abs(loc.longitude - -118.2437) < 1e-6


def test_parse_place_only():
    loc = parse_maps_url(
        "https://www.google.com/maps/place/Empire+State+Building,+New+York/"
    )
    assert not loc.has_coordinates
    assert loc.place_text == "Empire State Building, New York"


def test_parse_query_place_text():
    loc = parse_maps_url("https://maps.google.com/?q=Main+Street+Springfield")
    assert not loc.has_coordinates
    assert loc.place_text == "Main Street Springfield"


def test_invalid_coordinates_rejected():
    loc = parse_maps_url("https://maps.google.com/?q=999.0,999.0")
    assert not loc.has_coordinates
