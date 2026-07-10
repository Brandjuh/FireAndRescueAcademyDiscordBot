"""Post-creation tax setting via the building page's alliance_costs
buttons (the reference bot's mechanism — a GET per tax step, no form)."""

import pytest

from fra_bot.mc.building_tax import (
    has_tax_controls,
    read_tax_percent,
    set_building_tax,
)

pytestmark = pytest.mark.asyncio


def _building_page(active=0, building_id=777):
    # The real page shows one alliance-cost button per tax step; the active
    # one carries btn-success.
    buttons = "".join(
        f"<a class='btn btn-xs btn-alliance_costs "
        f"{'btn-success' if tax_id * 10 == active else 'btn-default'}' "
        f"href='/buildings/{building_id}/alliance_costs/{tax_id}'>"
        f"{tax_id * 10}%</a>"
        for tax_id in range(6)
    )
    return f"<html><body><dl><dt>Level:</dt><dd>3</dd></dl>{buttons}</body></html>"


def test_read_tax_percent_and_controls():
    assert read_tax_percent(_building_page(active=20), 777) == 20
    assert read_tax_percent(_building_page(active=0), 777) == 0
    assert has_tax_controls(_building_page(), 777) is True
    # A personal building page has no alliance-cost row.
    assert has_tax_controls("<html><body>no buttons</body></html>", 777) is False
    assert read_tax_percent("<html></html>", 777) is None
    # Another building's buttons don't count.
    assert has_tax_controls(_building_page(building_id=888), 777) is False


class _Client:
    def __init__(self, *, before=0, after=20):
        self.pages = [_building_page(before), _building_page(after)]
        self.fetched = []

    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None):
        self.fetched.append(path)
        if "/alliance_costs/" in path:
            return "OK"                       # the set-tax GET
        return self.pages.pop(0) if self.pages else _building_page(20)


async def test_set_building_tax_gets_link_and_verifies():
    client = _Client(before=0, after=20)
    ok, detail = await set_building_tax(client, 777, 20)
    assert ok and "tax set to 20%" in detail
    assert "/buildings/777/alliance_costs/2" in client.fetched


async def test_set_building_tax_detects_silent_refusal():
    client = _Client(before=0, after=0)       # page still shows 0% active
    ok, detail = await set_building_tax(client, 777, 20)
    assert not ok and "did not take" in detail


async def test_set_building_tax_short_circuits_when_already_set():
    client = _Client(before=20)
    ok, detail = await set_building_tax(client, 777, 20)
    assert ok and "already" in detail
    assert all("/alliance_costs/" not in p for p in client.fetched)


async def test_set_building_tax_refuses_unsupported_percent():
    ok, detail = await set_building_tax(_Client(), 777, 15)
    assert not ok and "unsupported" in detail


async def test_set_building_tax_reports_missing_button_row():
    class NoRowClient(_Client):
        async def fetch_page(self, path, *, referer=None):
            return "<html><body>personal building</body></html>"

    ok, detail = await set_building_tax(NoRowClient(), 777, 20)
    assert not ok and "no alliance tax buttons" in detail
