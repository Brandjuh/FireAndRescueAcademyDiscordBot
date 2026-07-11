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


def test_consent_wrapper_is_unwrapped():
    # EU consent interstitial: the real maps URL is percent-encoded in
    # ?continue= — mobile share links regularly expand to this.
    inner = "https://www.google.com/maps/place/Somewhere/@63.4205,10.3851,17z"
    import urllib.parse
    url = "https://consent.google.com/m?continue=" + urllib.parse.quote(inner, safe="")
    loc = parse_maps_url(url)
    assert loc.has_coordinates
    assert abs(loc.latitude - 63.4205) < 1e-6
    assert loc.place_text == "Somewhere"


def test_percent_encoded_pin_coordinates_found():
    # Coordinates hidden behind percent-encoding (!3d → %213d) inside a
    # wrapped/re-encoded URL must still be found.
    url = (
        "https://www.google.com/maps/place/Hospital/data="
        "%213m1%214b1%218m2%213d63.4205000%214d10.3851000"
    )
    loc = parse_maps_url(url)
    assert loc.has_coordinates
    assert abs(loc.latitude - 63.4205) < 1e-6
    assert abs(loc.longitude - 10.3851) < 1e-6


def test_double_encoded_place_text_cleaned():
    # %2B decodes to a literal '+' after one unquote round — those must
    # become spaces, not reach the geocoder verbatim (the St. Olav's case).
    url = "https://www.google.com/maps/place/St.%2BOlav's%2BUniversity%2BHospital/"
    loc = parse_maps_url(url)
    assert not loc.has_coordinates
    assert loc.place_text == "St. Olav's University Hospital"


def test_search_and_dir_urls_yield_place_text():
    from fra_bot.geo.maps_links import parse_maps_url

    assert parse_maps_url(
        "https://www.google.com/maps/search/Grand+Canyon"
    ).place_text == "Grand Canyon"
    assert parse_maps_url(
        "https://www.google.com/maps/dir/Yosemite+Village/"
    ).place_text == "Yosemite Village"


def test_extract_location_from_interstitial_html():
    from fra_bot.geo.maps_links import extract_location_from_html

    canonical = (
        "<html><link rel=\"canonical\" href=\"https://www.google.com/maps/"
        "place/Yosemite/@37.865,-119.538,15z\"/></html>"
    )
    loc = extract_location_from_html(canonical)
    assert (loc.latitude, loc.longitude, loc.place_text) == (
        37.865, -119.538, "Yosemite"
    )

    refresh = (
        "<meta http-equiv=\"refresh\" "
        "content=\"0; url=https://www.google.com/maps?q=48.85,2.35\">"
    )
    loc = extract_location_from_html(refresh)
    assert (loc.latitude, loc.longitude) == (48.85, 2.35)

    app_state = "window.APP_INITIALIZATION_STATE=[[[12.0,-119.538,37.865]"
    loc = extract_location_from_html(app_state)  # [zoom, lng, lat] order
    assert (loc.latitude, loc.longitude) == (37.865, -119.538)

    body_link = (
        "<a href=\"https://www.google.com/maps/search/Grand+Canyon/data\">x</a>"
    )
    assert extract_location_from_html(body_link).place_text == "Grand Canyon"

    assert extract_location_from_html("<html>nothing here</html>").latitude is None
