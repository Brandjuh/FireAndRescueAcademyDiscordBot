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

    async def fake_fetch(session, zoom, x, y):
        return tile

    monkeypatch.setattr(sm, "_fetch_tile", fake_fetch)
    png = await sm.render_map([(40.7, -74.0, 10), (40.8, -74.1, 3)])
    assert png is not None
    image = Image.open(io.BytesIO(png))
    assert image.size == (900, 600)


async def test_render_map_without_any_tile_returns_none(monkeypatch):
    async def fake_fetch(session, zoom, x, y):
        return None   # tile server unreachable

    monkeypatch.setattr(sm, "_fetch_tile", fake_fetch)
    assert await sm.render_map([(40.7, -74.0, 5)]) is None


async def test_render_map_without_points_returns_none():
    assert await sm.render_map([]) is None
