"""Parse a ``/buildings/<id>`` detail page for the upgrade automation.

We read three things off a building's page:

* the current **level** (hospitals — bed capacity, raised toward the max),
* the available **extension offers** (``/buildings/<id>/extension/credits/<extId>``
  links, each with an id + price), and
* the page **CSRF token** (needed to POST an extension purchase).

Plus a small classifier for the alliance building list so we can pick out
which alliance buildings are hospitals and prisons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

_BUILDING_ID_RE = re.compile(r"/buildings/(\d+)")
_LEVEL_DT_DD_RE = re.compile(
    r"Level:?\s*</strong>\s*</dt>\s*<dd>\s*(\d+)", re.IGNORECASE
)
_LEVEL_TEXT_RE = re.compile(r"\blevel\b[:\s]*?(\d+)", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d[\d.,\s]*")

# Alliance building name/label keywords → kind.
_HOSPITAL_TERMS = ("hospital", "krankenhaus", "ziekenhuis", "hôpital", "hopital",
                   "clinic centre")
_PRISON_TERMS = ("prison", "jail", "gefängnis", "gefangnis", "gevangenis",
                 "penitentiary")


@dataclass(frozen=True)
class ExtensionOffer:
    ext_id: int
    price: int | None
    href: str
    label: str


@dataclass(frozen=True)
class AllianceBuilding:
    building_id: int
    name: str
    kind: str | None  # "hospital" | "prison" | None


def parse_csrf_token(html: str) -> str | None:
    """The page's ``meta[name=csrf-token]`` value (for POST actions)."""
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta is not None and meta.get("content"):
        return str(meta["content"])
    return None


def parse_current_level(html: str) -> int | None:
    """Current building level, or None if the page doesn't show one."""
    match = _LEVEL_DT_DD_RE.search(html or "")
    if match:
        return int(match.group(1))
    # Fallback: strip tags and look for "Level: N".
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))
    match = _LEVEL_TEXT_RE.search(text)
    return int(match.group(1)) if match else None


def _parse_price(text: str) -> int | None:
    """The credits price in an extension label.

    Anchored on the "Credits" suffix so a leading count (e.g. "2 Cells —
    100,000 Credits") is never mistaken for the price; falls back to the LAST
    number in the label (labels put the price at the end)."""
    match = re.search(r"(\d[\d.,\s]*?)\s*credits", text or "", re.IGNORECASE)
    if match is None:
        candidates = _DIGITS_RE.findall(text or "")
        if not candidates:
            return None
        match_text = candidates[-1]
    else:
        match_text = match.group(1)
    digits = re.sub(r"[^\d]", "", match_text)
    return int(digits) if digits else None


def parse_extension_offers(html: str, building_id: int) -> list[ExtensionOffer]:
    """Available extension offers, deduped by id and sorted ascending.

    Only the currently-purchasable extensions are shown by MissionChief, so
    for prisons the "next" extension appears only after the previous is
    built — re-fetching after each purchase naturally walks the chain."""
    soup = BeautifulSoup(html, "lxml")
    prefix = f"/buildings/{building_id}/extension/credits/"
    ext_re = re.compile(rf"^/buildings/{building_id}/extension/credits/(\d+)")
    offers: dict[int, ExtensionOffer] = {}
    for link in soup.find_all("a", href=True):
        # A locked next-in-chain extension (e.g. an academy's 2nd/3rd
        # classroom) renders as a `disabled` button that still carries its
        # href — buying it would be rejected, so skip it. It reappears
        # (enabled) once the previous one is built, and the re-fetch loop
        # picks it up then.
        if "disabled" in (link.get("class") or []):
            continue
        href = link["href"]
        if not href.startswith(prefix):
            continue
        match = ext_re.match(href)
        if not match:
            continue
        ext_id = int(match.group(1))
        if ext_id in offers:
            continue
        label = re.sub(r"\s+", " ", link.get_text(" ", strip=True)) or href
        offers[ext_id] = ExtensionOffer(
            ext_id=ext_id, price=_parse_price(label), href=href, label=label
        )
    return [offers[k] for k in sorted(offers)]


def _classify_kind(*texts: str) -> str | None:
    joined = " ".join(t.lower() for t in texts if t)
    is_hospital = any(term in joined for term in _HOSPITAL_TERMS)
    is_prison = any(term in joined for term in _PRISON_TERMS)
    if is_hospital and not is_prison:
        return "hospital"
    if is_prison and not is_hospital:
        return "prison"
    return None


def parse_alliance_building_kinds(html: str) -> list[AllianceBuilding]:
    """Alliance building rows classified as hospital / prison / other."""
    soup = BeautifulSoup(html, "lxml")
    out: list[AllianceBuilding] = []
    for tr in soup.find_all("tr"):
        name = tr.get("search_attribute") or ""
        building_id = None
        img = tr.find("img", attrs={"building_id": True})
        if img is not None:
            try:
                building_id = int(img["building_id"])
            except (ValueError, TypeError):
                building_id = None
        if building_id is None:
            link = tr.find("a", href=_BUILDING_ID_RE)
            if link is not None:
                building_id = int(_BUILDING_ID_RE.search(link["href"]).group(1))
        if building_id is None:
            continue
        hints = []
        for tag in tr.find_all("img"):
            hints.extend(str(tag.get(attr, "")) for attr in ("src", "alt", "title"))
        kind = _classify_kind(name, *hints, tr.get_text(" ", strip=True))
        out.append(
            AllianceBuilding(
                building_id=building_id, name=name or f"Building {building_id}",
                kind=kind,
            )
        )
    return out
