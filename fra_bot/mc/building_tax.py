"""Set an alliance building's tax percentage.

The reference bot sets the tax on the BUILDING PAGE itself (alliance
buildings carry an inline tax form there), finding the field generically
(any select/input whose name mentions tax/share/fee/…) so a layout tweak
degrades to a clear error. Same here: the building page is tried first,
the edit page second, and the result is VERIFIED by re-reading the same
page — only a field that afterwards shows the target percentage counts.
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
_PAGE_CANDIDATES = ("/buildings/{id}", "/buildings/{id}/edit")


def _collect_form(form) -> tuple[dict, str | None]:
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
    return payload, tax_field


def find_tax_form(html: str, building_id: int) -> tuple[str, dict, str]:
    """(action, payload, tax_field_name) of the form carrying the tax
    field. A form scoped to this building wins; otherwise the first form
    with a tax-ish field is used. Raises ParseError when none exists."""
    soup = BeautifulSoup(html, "lxml")
    fallback: tuple[str, dict, str] | None = None
    for form in soup.find_all("form"):
        payload, tax_field = _collect_form(form)
        if tax_field is None:
            continue
        action = form.get("action") or ""
        scoped = (
            f"/buildings/{building_id}" in action
            or f"/alliance_buildings/{building_id}" in action
            or "tax" in action.lower()
        )
        if scoped:
            return action, payload, tax_field
        if fallback is None:
            fallback = (action, payload, tax_field)
    if fallback is not None:
        return fallback
    raise ParseError(
        f"No tax field found for building {building_id} — layout change?"
    )


def _matches(value, percent: int) -> bool:
    try:
        return float(value) == float(percent)
    except (TypeError, ValueError):
        return str(value) == str(percent)


def read_tax_value(html: str, building_id: int) -> str | None:
    try:
        _, payload, tax_field = find_tax_form(html, building_id)
    except ParseError:
        return None
    return payload.get(tax_field)


async def set_building_tax(
    client, building_id: int, percent: int
) -> tuple[bool, str]:
    """Set the building's tax and VERIFY it stuck. Returns (ok, detail).

    Tries the building page first (where the reference bot found the
    field), then the edit page."""
    last_detail = "no tax field found on the building pages"
    for template in _PAGE_CANDIDATES:
        path = template.format(id=building_id)
        try:
            html = await client.fetch_page(path)
        except MissionChiefError as exc:
            last_detail = str(exc)
            continue
        try:
            action, payload, tax_field = find_tax_form(html, building_id)
        except ParseError as exc:
            last_detail = str(exc)
            continue
        if _matches(payload.get(tax_field), percent):
            return True, f"tax already {percent}%"
        payload[tax_field] = str(percent)
        try:
            status, _, _ = await client.post_form(
                action, payload, referer=client.url(path)
            )
        except MissionChiefError as exc:
            return False, f"tax update failed ({exc})"
        if status >= 400:
            return False, f"tax update rejected (HTTP {status})"
        try:
            after = read_tax_value(await client.fetch_page(path), building_id)
        except MissionChiefError as exc:
            return False, f"tax set but could not verify ({exc})"
        if _matches(after, percent):
            return True, f"tax set to {percent}%"
        return False, f"tax update did not take (form still shows {after!r})"
    return False, last_detail
