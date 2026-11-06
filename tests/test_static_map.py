"""Static hotspot map: Mercator math, zoom picking and the renderer
(offline — tile fetches are stubbed)."""

import io

import pytest

import fra_bot.geo.static_map as sm

pytestmark = pytest.mark.asyncio


def _png_tile(colour=(120, 160, 120)):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (sm.TILE_SIZE, sm.TILE_SIZE), colour).save(out, "PNG")
    return out.getvalue()


def test_global_px_is_monotonic_and_wraps_correctly():
    x_west, y_north = sm._global_px(60.0, -120.0, 4)
    x_east, y_south = sm._global_px(-60.0, 120.0, 4)
    assert x_west < x_east          # east of west
    assert y_north < y_south        # Mercator y grows southward
    # Poles clamp instead of exploding at the Mercator singularity.
    _, y_pole = sm._global_px(90.0, 0.0, 4)
    assert y_pole >= 0


def test_pick_zoom_single_point_uses_city_level():
    assert sm.pick_zoom([(40.7, -74.0)], 900, 600) == 11


def test_pick_zoom_spread_points_zoom_out_until_they_fit():
    # NYC + LA never fit a 900px canvas at city zoom.
    zoom = sm.pick_zoom([(40.7, -74.0), (34.05, -118.24)], 900, 600)
    assert 2 <= zoom < 8
    xs = [sm._global_px(lat, lng, zoom)[0] for lat, lng in
          [(40.7, -74.0), (34.05, -118.24)]]
    assert max(xs) - min(xs) <= 900 - 2 * 60   # honours the padding


async def test_render_map_returns_png_with_requested_size(monkeypatch):
    from PIL import Image

    tile = _png_tile()

    async def fake_fetch(session, template, zoom, x, y):
        return tile

    monkeypatch.setattr(sm, "_fetch_tile", fake_fetch)
    png = await sm.render_map([(40.7, -74.0, 10), (40.8, -74.1, 3)])
    assert png is not None
    image = Image.open(io.BytesIO(png))
    assert image.size == (1000, 667)


async def test_render_map_falls_back_to_the_second_tile_style(monkeypatch):
    tile = _png_tile()
    asked = []

    async def fake_fetch(session, template, zoom, x, y):
        asked.append(template)
        # osm.org unreachable; OSM France still answers.
        return tile if "openstreetmap.fr" in template else None

    monkeypatch.setattr(sm, "_fetch_tile", fake_fetch)
    png = await sm.render_map([(40.7, -74.0, 5)])
    assert png is not None
    assert any("tile.openstreetmap.org" in template for template in asked)  # first
    assert any("openstreetmap.fr" in template for template in asked)        # fallback


def test_no_tile_server_needs_an_api_key():
    # CARTO started watermarking keyless tiles ("API KEY REQUIRED") in
    # autumn 2026; the map must only use servers that work without a key.
    for style in sm.STYLES:
        assert "cartocdn" not in style.template
        assert "key" not in style.template.lower()


class _FakeResponse:
    def __init__(self, status=200, etag=None, body=b"png"):
        self.status = status
        self.headers = {"ETag": etag} if etag else {}
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return self.response


async def test_watermark_tiles_are_refused(monkeypatch):
    sm._TILE_CACHE.clear()
    session = _FakeSession(_FakeResponse(etag='"wm-da89c20e77c1-dark"'))
    assert await sm._fetch_tile(session, "https://x.test/{z}/{x}/{y}.png", 3, 1, 1) is None
    assert sm._TILE_CACHE == {}  # and never cached


async def test_real_tiles_are_cached(monkeypatch):
    sm._TILE_CACHE.clear()
    session = _FakeSession(_FakeResponse(etag='"abc123"', body=b"tile"))
    url = "https://x.test/{z}/{x}/{y}.png"
    assert await sm._fetch_tile(session, url, 3, 1, 1) == b"tile"
    assert await sm._fetch_tile(session, url, 3, 1, 1) == b"tile"
    assert session.calls == 1
    sm._TILE_CACHE.clear()


def test_darken_turns_a_light_tile_dark():
    from PIL import Image, ImageStat

    light = Image.new("RGB", (8, 8), (242, 239, 233))   # OSM land colour
    light.putpixel((0, 0), (40, 40, 40))                  # a dark label pixel
    dark = sm._darken(light)
    assert ImageStat.Stat(dark.convert("L")).mean[0] < 60
    # the formerly dark label is now the light one
    assert dark.convert("L").getpixel((0, 0)) > dark.convert("L").getpixel((4, 4))


async def test_render_map_without_any_tile_returns_none(monkeypatch):
    async def fake_fetch(session, template, zoom, x, y):
        return None   # every tile server unreachable

    monkeypatch.setattr(sm, "_fetch_tile", fake_fetch)
    assert await sm.render_map([(40.7, -74.0, 5)]) is None


async def test_render_map_without_points_returns_none():
    assert await sm.render_map([]) is None
