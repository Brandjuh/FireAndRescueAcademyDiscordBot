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
        return {"display_name": "Cached Place"}

    geo._nominatim = fake_nominatim
    a = await geo.reverse(51.5, -0.12)
    b = await geo.reverse(51.5, -0.12)  # second call served from cache
    assert a == b == "Cached Place"
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


async def test_search_no_results_raises(geo):
    async def fake_nominatim(path, params):
        return []

    geo._nominatim = fake_nominatim
    with pytest.raises(GeocodeError):
        await geo.search("nowhere at all")


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
