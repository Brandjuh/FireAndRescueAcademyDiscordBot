"""The alliance snapshot infographic (offline: pure Pillow rendering)."""

import io

import pytest

from fra_bot.services.game_sync import Hotspot, top_building_types
from fra_bot.services.infographic import AllianceSnapshot, render_infographic

pytestmark = pytest.mark.asyncio


def _snapshot(**overrides):
    base = dict(
        title="Fire & Rescue Academy",
        date_label="17 Jul 2026",
        members_synced=12,
        building_total=823,
        vehicle_total=2450,
        top_types=[("fire station", 400), ("hospital", 120), ("prison", 40)],
        spots=[
            Hotspot(40.72, -74.02, 220, 5, place="Jersey City, New Jersey"),
            Hotspot(29.76, -95.37, 87, 2, place="Houston, Texas"),
            Hotspot(41.88, -87.63, 30, 1),
        ],
        map_png=None,
    )
    base.update(overrides)
    return AllianceSnapshot(**base)


def _tiny_map():
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1000, 667), (34, 36, 42)).save(out, "PNG")
    return out.getvalue()


def test_top_building_types_sums_and_names():
    types = top_building_types([{"0": 3, "2": 1}, {"0": 2, "77": 5}])
    assert types[0] == ("fire station", 5)     # summed across members
    assert ("type 77", 5) in types             # unknown id still shown
    assert ("hospital", 1) in types
    # Garbage keys/values are skipped, not fatal.
    assert top_building_types([{"x": "y"}, {"0": "2"}]) == [("fire station", 2)]


def test_render_infographic_produces_a_png_card():
    from PIL import Image

    png = render_infographic(_snapshot(map_png=_tiny_map()))
    assert png is not None
    image = Image.open(io.BytesIO(png))
    assert image.width == 1080
    assert image.height > 800          # header + tiles + chart + map + list


def test_render_infographic_survives_minimal_data():
    # No chart rows, no spots, no map — still a valid card with the tiles.
    png = render_infographic(_snapshot(top_types=[], spots=[], map_png=None))
    assert png is not None
    # Corrupt map bytes are ignored, not fatal.
    assert render_infographic(_snapshot(map_png=b"not a png")) is not None
