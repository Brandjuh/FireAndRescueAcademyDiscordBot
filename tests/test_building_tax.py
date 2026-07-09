"""Post-creation tax setting: form discovery, update, verification."""

import pytest

from fra_bot.mc.building_tax import find_tax_form, read_tax_value, set_building_tax
from fra_bot.mc.errors import ParseError

pytestmark = pytest.mark.asyncio


def _edit_page(share="0"):
    options = "".join(
        f"<option value='{v}'{' selected' if v == share else ''}>{v}%</option>"
        for v in ("0", "10", "20", "30", "40", "50")
    )
    return (
        "<form action='/buildings/777' method='post'>"
        "<input type='hidden' name='_method' value='patch'/>"
        "<input type='hidden' name='authenticity_token' value='tok'/>"
        "<input type='text' name='building[name]' value='Hospital X'/>"
        f"<select name='building[alliance_share]'>{options}</select>"
        "<input type='submit' value='Save'/>"
        "</form>"
    )


def test_find_tax_form_extracts_action_payload_and_field():
    action, payload, tax_field = find_tax_form(_edit_page(), 777)
    assert action == "/buildings/777"
    assert tax_field == "building[alliance_share]"
    assert payload["_method"] == "patch"
    assert payload["authenticity_token"] == "tok"
    assert payload["building[name]"] == "Hospital X"
    assert payload[tax_field] == "0"
    assert read_tax_value(_edit_page("20"), 777) == "20"


def test_find_tax_form_fails_loud_without_tax_field():
    with pytest.raises(ParseError):
        find_tax_form("<form action='/buildings/777'>"
                      "<input name='building[name]'/></form>", 777)


class _Client:
    def __init__(self, *, before="0", after="20", post_status=200):
        self.pages = [_edit_page(before), _edit_page(after)]
        self.post_status = post_status
        self.posts = []

    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None):
        return self.pages.pop(0) if self.pages else _edit_page("20")

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, dict(data)))
        return (self.post_status, {}, "")


async def test_set_building_tax_posts_and_verifies():
    client = _Client(before="0", after="20")
    ok, detail = await set_building_tax(client, 777, 20)
    assert ok and "tax set to 20%" in detail
    path, data = client.posts[0]
    assert path == "/buildings/777"
    assert data["building[alliance_share]"] == "20"
    assert data["_method"] == "patch"          # full form carried along


async def test_set_building_tax_detects_silent_refusal():
    client = _Client(before="0", after="0")    # form still shows 0 after POST
    ok, detail = await set_building_tax(client, 777, 20)
    assert not ok and "did not take" in detail


async def test_set_building_tax_short_circuits_when_already_set():
    client = _Client(before="20")
    ok, detail = await set_building_tax(client, 777, 20)
    assert ok and "already" in detail
    assert client.posts == []                   # nothing submitted
