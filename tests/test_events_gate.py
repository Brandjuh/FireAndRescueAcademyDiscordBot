"""Tests for the events request gate (H1) — chatter must NOT be
geocoded, only real requests (maps link or explicit prefix)."""

from types import SimpleNamespace

from fra_bot.services.events import EventsService


def _service():
    # Only _extract_location is exercised; pass minimal stand-ins.
    cfg = SimpleNamespace(
        automation=SimpleNamespace(
            events=SimpleNamespace(thread_id=15293, min_contribution_rate=5.0)
        )
    )
    svc = EventsService.__new__(EventsService)
    svc._auto = cfg.automation.events
    return svc


def test_chatter_is_ignored():
    svc = _service()
    assert svc._extract_location("thanks everyone, great work today!") is None
    assert svc._extract_location("nice job on that mission") is None
    assert svc._extract_location("") is None


def test_explicit_prefix_accepted():
    svc = _service()
    assert svc._extract_location("event: Kansas City, Kansas") == "Kansas City, Kansas"
    assert svc._extract_location("Location - Amsterdam") == "Amsterdam"


def test_maps_link_accepted():
    svc = _service()
    link = "https://maps.app.goo.gl/abc123"
    assert svc._extract_location(f"start here {link}") == link


def test_bare_place_without_prefix_ignored():
    svc = _service()
    # No prefix, no link -> not a request (avoids geocoding random words).
    assert svc._extract_location("Kansas City") is None
