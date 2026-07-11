"""Geocoder orchestration tests (M11) with Nominatim mocked out."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo
from fra_bot.geo.geocoder import GeocodeError, Geocoder

# asyncio_mode = auto (pytest.ini) runs the async tests without a mark.


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "g.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def geo(db):
    g = Geocoder(StateRepo(db))
    await g.start()
    yield g
    await g.close()


async def test_url_coordinates_use_reverse_not_search(geo):
    calls = []

    async def fake_nominatim(path, params):
        calls.append(path)
        return {"display_name": "123 Main St, Anytown"}

    geo._nominatim = fake_nominatim
    result = await geo.resolve_maps_link(
        "https://www.google.com/maps/@40.7128,-74.0060,15z"
    )
    assert abs(result.latitude - 40.7128) < 1e-6
    assert result.source == "url"
    assert result.address == "123 Main St, Anytown"
    assert calls == ["/reverse"]  # reverse-geocoded, never searched


async def test_reverse_result_is_cached(geo):
    calls = []

    async def fake_nominatim(path, params):
        calls.append(path)
        return {"display_name": "Cached Place", "type": "hospital"}

    geo._nominatim = fake_nominatim
    a = await geo.reverse(51.5, -0.12)
    b = await geo.reverse(51.5, -0.12)  # second call served from cache
    assert a == b == ("Cached Place", "hospital")
    assert len(calls) == 1


async def test_place_only_link_falls_back_to_search(geo):
    async def fake_nominatim(path, params):
        assert path == "/search"
        return [{"lat": "34.05", "lon": "-118.24", "display_name": "LA"}]

    geo._nominatim = fake_nominatim
    result = await geo.resolve_maps_link(
        "https://www.google.com/maps/place/Los+Angeles/"
    )
    assert result.source == "nominatim_search"
    assert abs(result.latitude - 34.05) < 1e-6


async def test_maps_place_link_carries_place_text_for_type_detection(geo):
    # A hospital pin often reverse-geocodes to a plain street with no
    # "hospital" in it; the place NAME from the link is what makes detection
    # work — exactly the "member drops a maps pin" flow.
    from fra_bot.services.buildings import detect_building_type

    async def fake_nominatim(path, params):
        assert path == "/search"
        return [{
            "lat": "47.02", "lon": "4.83",
            "display_name": "Av. Guigone de Salins, Beaune",
            "type": "hospital",  # the OSM amenity tag
        }]

    geo._nominatim = fake_nominatim
    result = await geo.resolve_maps_link(
        "https://www.google.com/maps/place/Centre+Hospitalier+de+Beaune/"
    )
    assert result.place_text == "Centre Hospitalier de Beaune"
    assert result.place_type == "hospital"
    assert "hospital" not in (result.address or "").lower()  # street only
    # Address alone misses it; the OSM type (or the place name) detects it.
    assert detect_building_type(result.address, None) is None
    assert detect_building_type(result.address, None, result.place_type) == "hospital"
    assert detect_building_type(result.address, result.place_text) == "hospital"


async def test_search_no_results_raises(geo):
    async def fake_nominatim(path, params):
        return []

    geo._nominatim = fake_nominatim
    with pytest.raises(GeocodeError):
        await geo.search("nowhere at all")


async def test_search_retries_with_simplified_punctuation(geo):
    # OSM names often lack the dots/apostrophes people (and Google) use:
    # "St. Olav's University Hospital" exists as "St Olavs hospital".
    queries = []

    async def fake_nominatim(path, params):
        queries.append(params["q"])
        if "." in params["q"] or "'" in params["q"]:
            return []  # exact form unknown to OSM
        return [{"lat": "63.42", "lon": "10.39", "display_name": "St Olavs hospital",
                 "type": "hospital"}]

    geo._nominatim = fake_nominatim
    result = await geo.search("St. Olav's University Hospital")
    assert queries == [
        "St. Olav's University Hospital",
        "St Olavs University Hospital",
    ]
    assert abs(result.latitude - 63.42) < 1e-6
    assert result.place_type == "hospital"


async def test_search_simplified_retry_still_raises_when_empty(geo):
    async def fake_nominatim(path, params):
        return []

    geo._nominatim = fake_nominatim
    with pytest.raises(GeocodeError, match="found nothing"):
        await geo.search("St. Nowhere's Clinic")


async def test_search_near_phrase_falls_back_to_each_side(geo):
    # "X near Y" is how members describe places, but geocoders can't parse
    # relative descriptions — the live failure was 'Okanogan-Wenatchee
    # National Forest near Yakima, WA, USA'. Each side alone resolves.
    queries = []

    async def fake_nominatim(path, params):
        queries.append(params["q"])
        if params["q"] == "Okanogan-Wenatchee National Forest":
            return [{"lat": "47.5", "lon": "-120.9",
                     "display_name": "Okanogan-Wenatchee National Forest, WA"}]
        return []

    geo._nominatim = fake_nominatim
    result = await geo.search("Okanogan-Wenatchee National Forest near Yakima, WA, USA")
    assert abs(result.latitude - 47.5) < 1e-6
    # Full query, punctuation-stripped retry, then the "near" split.
    assert queries[-1] == "Okanogan-Wenatchee National Forest"


async def test_search_near_fallback_tries_second_side_too(geo):
    async def fake_nominatim(path, params):
        if params["q"] == "Yakima, WA, USA":
            return [{"lat": "46.6", "lon": "-120.5", "display_name": "Yakima"}]
        return []

    geo._nominatim = fake_nominatim
    result = await geo.search("Somewhere Unknown near Yakima, WA, USA")
    assert abs(result.latitude - 46.6) < 1e-6


def test_default_geocoder_uses_nominatim_without_key(db):
    g = Geocoder(StateRepo(db))
    url = g._geocode_url("/search", {"q": "NYC"})
    assert url.startswith("https://nominatim.openstreetmap.org/search")
    assert "api_key" not in url


def test_configured_key_is_injected(db):
    g = Geocoder(
        StateRepo(db),
        base_url="https://geocode.maps.co/",
        api_key="secret123",
        api_key_param="api_key",
    )
    url = g._geocode_url("/search", {"q": "NYC"})
    assert url.startswith("https://geocode.maps.co/search")
    assert "api_key=secret123" in url


def test_locationiq_style_key_param(db):
    g = Geocoder(
        StateRepo(db),
        base_url="https://us1.locationiq.com/v1",
        api_key="abc",
        api_key_param="key",
    )
    url = g._geocode_url("/reverse", {"lat": "1", "lon": "2"})
    assert "key=abc" in url and "api_key=" not in url


async def test_short_link_interstitial_body_is_mined_for_coordinates(geo):
    """A short link that does NOT redirect (Google interstitial, HTTP 200)
    still resolves when the page body carries the real Maps URL."""
    async def fake_expand(url):
        return url, (
            "<html><link rel=\"canonical\" href=\"https://www.google.com/"
            "maps/place/Somewhere/@40.1,-74.2,15z\"/></html>"
        )

    async def fake_reverse(lat, lng):
        return "Somewhere, USA", "forest"

    geo._expand_short_link = fake_expand
    geo.reverse = fake_reverse
    result = await geo.resolve_maps_link("https://maps.app.goo.gl/uRjpSAxq7")
    assert abs(result.latitude - 40.1) < 1e-6
    assert abs(result.longitude + 74.2) < 1e-6


async def test_unresolvable_short_link_error_names_the_interstitial(geo):
    async def fake_expand(url):
        return url, "<html>consent wall with nothing useful</html>"

    geo._expand_short_link = fake_expand
    with pytest.raises(GeocodeError) as excinfo:
        await geo.resolve_maps_link("https://maps.app.goo.gl/uRjpSAxq7")
    assert "did not redirect" in str(excinfo.value)
