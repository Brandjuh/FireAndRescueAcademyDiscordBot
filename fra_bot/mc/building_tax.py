"""Set an alliance building's tax percentage.

The building page carries a row of alliance-cost buttons —
``/buildings/<id>/alliance_costs/<tax_id>`` with ``tax_id = percent / 10``
— and the active one is marked ``btn-success``. Setting the tax is a plain
GET to the target link (the reference bot's mechanism; there is no tax
form to POST), and the result is VERIFIED by re-reading the page: only a
button row that afterwards shows the target percentage active counts.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .errors import MissionChiefError

log = logging.getLogger(__name__)

TAX_LEVELS = (0, 10, 20, 30, 40, 50)


def _cost_links(html: str, building_id: int):
    soup = BeautifulSoup(html, "lxml")
    pattern = re.compile(rf"/buildings/{building_id}/alliance_costs/(\d+)")
    for link in soup.find_all("a", href=True):
        match = pattern.search(link["href"])
        if match:
            yield link, int(match.group(1))


def has_tax_controls(html: str, building_id: int) -> bool:
    """True when the building page shows the alliance tax button row."""
    return next(_cost_links(html, building_id), None) is not None


def read_tax_percent(html: str, building_id: int) -> int | None:
    """The building's current tax: the ``btn-success`` alliance-cost button.
    Returns None when no button is active (or the row is missing)."""
    for link, tax_id in _cost_links(html, building_id):
        classes = " ".join(link.get("class") or [])
        if "btn-success" not in classes:
            continue
        text_match = re.search(r"(\d+)\s*%", link.get_text())
        if text_match:
            return int(text_match.group(1))
        return tax_id * 10
    return None


async def set_building_tax(
    client, building_id: int, percent: int
) -> tuple[bool, str]:
    """Set the building's tax via its alliance_costs link and VERIFY it
    stuck. Returns (ok, detail)."""
    if percent not in TAX_LEVELS:
        return False, (
            f"unsupported tax {percent}% — use one of "
            + "/".join(str(l) for l in TAX_LEVELS)
        )
    page_path = f"/buildings/{building_id}"
    try:
        html = await client.fetch_page(page_path)
    except MissionChiefError as exc:
        return False, f"could not load the building page ({exc})"
    if not has_tax_controls(html, building_id):
        return False, (
            "no alliance tax buttons on the building page — not an alliance "
            "building, or the layout changed"
        )
    if read_tax_percent(html, building_id) == percent:
        return True, f"tax already {percent}%"

    tax_id = percent // 10
    try:
        await client.fetch_page(
            f"/buildings/{building_id}/alliance_costs/{tax_id}",
            referer=client.url(page_path),
            ajax=True,  # the page's own tax buttons call this via XHR
        )
    except MissionChiefError as exc:
        return False, f"tax update failed ({exc})"

    try:
        after = read_tax_percent(
            await client.fetch_page(page_path), building_id
        )
    except MissionChiefError as exc:
        return False, f"tax set but could not verify ({exc})"
    if after == percent:
        return True, f"tax set to {percent}%"
    shown = f"{after}%" if after is not None else "no active tax button"
    return False, f"tax update did not take (page shows {shown})"
