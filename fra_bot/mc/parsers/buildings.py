"""Parse the building-type dropdown on /buildings/new.

Used to verify (and, if needed, resolve) the numeric building-type ids the
browser builder submits, so a MissionChief reordering of the options is
caught rather than silently building the wrong type.
"""

from __future__ import annotations

from bs4 import BeautifulSoup


def parse_building_type_options(html: str) -> dict[str, str]:
    """Map building-type label (lowercased) -> option value.

    e.g. ``{"hospital": "2", "prison": "10", ...}``. Empty when the select
    isn't present (e.g. not logged in).
    """
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", attrs={"name": "building[building_type]"})
    if select is None:
        return {}
    options: dict[str, str] = {}
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        label = option.get_text(strip=True).lower()
        if value and label:
            options[label] = value
    return options
