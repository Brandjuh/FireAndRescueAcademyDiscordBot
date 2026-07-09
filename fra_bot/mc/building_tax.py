"""Set an alliance building's tax percentage (/buildings/<id>/edit).

The reference bot sets the tax right after creating a building, finding
the field generically (any select/input whose name/label mentions
tax/share/fee/…) so a layout tweak degrades to a clear error. Same here,
plus verification: the edit page is re-fetched and only a field that now
shows the target percentage counts as set.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from .errors import MissionChiefError, ParseError

log = logging.getLogger(__name__)

_TAX_FIELD_RE = re.compile(
    r"tax|share|fee|percent|abgabe|steuer|beitrag", re.IGNORECASE
)


def _edit_path(building_id: int) -> str:
    return f"/buildings/{building_id}/edit"


def find_tax_form(html: str, building_id: int) -> tuple[str, dict, str]:
    """(action, payload, tax_field_name) for the building edit form with
    the tax field set — raises ParseError when no tax-ish field exists."""
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if f"/buildings/{building_id}" not in action:
            continue
        payload: dict[str, str] = {}
        tax_field: str | None = None
        for tag in form.find_all(("input", "textarea", "select")):
            name = tag.get("name")
            if not name or tag.get("type") in ("submit", "button"):
                continue
            if tag.name == "select":
                selected = tag.find("option", selected=True) or tag.find("option")
                value = selected.get("value", "") if selected else ""
            elif tag.name == "textarea":
                value = tag.get_text() or ""
            elif tag.get("type") in ("checkbox", "radio"):
                if not tag.has_attr("checked"):
                    continue
                value = tag.get("value", "1")
            else:
                value = tag.get("value") or ""
            payload[name] = value
            if tax_field is None and _TAX_FIELD_RE.search(name):
                tax_field = name
        if tax_field is not None:
            return action, payload, tax_field
    raise ParseError(
        f"No tax field found on the edit form of building {building_id} — "
        "layout change?"
    )


def read_tax_value(html: str, building_id: int) -> str | None:
    """The currently-selected tax value on the edit page (verification)."""
    try:
        _, payload, tax_field = find_tax_form(html, building_id)
    except ParseError:
        return None
    return payload.get(tax_field)


async def set_building_tax(
    client, building_id: int, percent: int
) -> tuple[bool, str]:
    """Set the building's tax and VERIFY it stuck. Returns (ok, detail)."""
    try:
        html = await client.fetch_page(_edit_path(building_id))
        action, payload, tax_field = find_tax_form(html, building_id)
        if payload.get(tax_field) == str(percent):
            return True, f"tax already {percent}%"
        payload[tax_field] = str(percent)
        status, _, _ = await client.post_form(
            action, payload, referer=client.url(_edit_path(building_id))
        )
        if status >= 400:
            return False, f"tax update rejected (HTTP {status})"
        after = read_tax_value(
            await client.fetch_page(_edit_path(building_id)), building_id
        )
        if after == str(percent):
            return True, f"tax set to {percent}%"
        return False, (
            f"tax update did not take (form still shows {after!r})"
        )
    except (MissionChiefError, ParseError) as exc:
        return False, str(exc)
