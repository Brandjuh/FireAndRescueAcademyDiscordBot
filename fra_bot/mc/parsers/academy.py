"""Parsers for alliance academies (training buildings).

Two pages are involved in opening a training:

* ``/verband/gebauede`` — the alliance building list. Rows carry a
  ``search_attribute`` with the building name; the building id comes
  from an ``<img building_id=...>`` or a ``/buildings/<id>`` link; the
  discipline is inferred from the row's image/alt/title keywords.
* ``/buildings/<id>`` — the academy page with the education form:
  hidden ``authenticity_token``, ``building_rooms_use`` (free
  classrooms), ``alliance[cost]`` options and the ``education_select``
  course list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

_BUILDING_ID_RE = re.compile(r"/buildings/(\d+)")

_DISCIPLINE_KEYWORDS = [
    ("coastal", ("coastal", "water rescue", "water_rescue", "lifeguard")),
    ("fire", ("fire", "feuerwehr", "fireacademy", "fire_academy")),
    ("police", ("police", "polizei", "swat")),
    ("ems", ("ems", "ambulance", "rescue_school", "rettung", "medical")),
]


@dataclass
class AcademyListing:
    building_id: int
    name: str
    discipline: str | None
    has_start_button: bool


def infer_discipline(*texts: str) -> str | None:
    joined = " ".join(t.lower() for t in texts if t)
    for discipline, keywords in _DISCIPLINE_KEYWORDS:
        if any(keyword in joined for keyword in keywords):
            return discipline
    return None


def parse_alliance_buildings_page(html: str) -> list[AcademyListing]:
    soup = BeautifulSoup(html, "lxml")
    listings: list[AcademyListing] = []
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

        image_hints = []
        for tag in tr.find_all("img"):
            image_hints.extend(
                str(tag.get(attr, "")) for attr in ("src", "alt", "title")
            )
        discipline = infer_discipline(name, *image_hints, tr.get_text(" ", strip=True))

        has_start = False
        for link in tr.find_all("a", href=_BUILDING_ID_RE):
            classes = " ".join(link.get("class") or [])
            text = link.get_text(" ", strip=True).lower()
            if "btn-success" in classes or "start a new training course" in text:
                has_start = True
                break

        listings.append(
            AcademyListing(
                building_id=building_id,
                name=name or f"Building {building_id}",
                discipline=discipline,
                has_start_button=has_start,
            )
        )
    return listings


def find_next_page_path(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    link = soup.find("a", rel="next")
    if link is None:
        for candidate in soup.find_all("a", href=True):
            if candidate.get_text(strip=True).lower() in ("next", "next ›", "›"):
                link = candidate
                break
    return link["href"] if link is not None and link.get("href") else None


@dataclass
class AcademyPage:
    action: str | None = None
    authenticity_token: str | None = None
    available_rooms: int = 0
    costs: list[int] = field(default_factory=list)
    courses: dict[str, str] = field(default_factory=dict)  # label -> option value

    def find_course_value(self, training_name: str) -> str | None:
        from ..trainings_catalog import normalized_equals

        for label, value in self.courses.items():
            if normalized_equals(label, training_name):
                return value
        return None


def parse_academy_page(html: str) -> AcademyPage:
    soup = BeautifulSoup(html, "lxml")
    page = AcademyPage()

    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "education" not in action:
            continue
        page.action = action
        token = form.find("input", attrs={"name": "authenticity_token"})
        if token is not None:
            page.authenticity_token = token.get("value")

        rooms_select = form.find("select", attrs={"name": "building_rooms_use"})
        if rooms_select is not None:
            options = [
                int(opt.get("value"))
                for opt in rooms_select.find_all("option")
                if (opt.get("value") or "").isdigit()
            ]
            page.available_rooms = max(options) if options else 0

        cost_select = form.find("select", attrs={"name": "alliance[cost]"})
        if cost_select is not None:
            page.costs = [
                int(opt.get("value"))
                for opt in cost_select.find_all("option")
                if (opt.get("value") or "").lstrip("-").isdigit()
            ]

        course_select = form.find("select", attrs={"name": "education_select"})
        if course_select is not None:
            for opt in course_select.find_all("option"):
                value = opt.get("value")
                label = opt.get_text(strip=True)
                if value and label:
                    page.courses[label] = value
        break

    return page
