"""Parser for the alliance members list (/verband/mitglieder/<id>?page=N).

Columns: NAME | ROLE | EARNED CREDITS | DISCOUNT | ALLIANCE CONTRIBUTION
RATE | MEMBER SINCE. The list is sorted by earned credits, so ordering
carries no meaning for us — rows are keyed by the /users/<id> link.

Parsing is header-positional: we map column indexes from the <th> texts
so DISCOUNT and CONTRIBUTION RATE can never be confused (the old bot
grabbed "the first cell containing %", which was usually the discount).
A heuristic fallback covers header-less layouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from ..errors import ParseError
from .common import extract_user_id, parse_int, parse_percent


@dataclass
class MembersPage:
    members: list[dict[str, Any]]
    has_table: bool


_HEADER_ALIASES = {
    "name": "name",
    "member": "name",
    "user": "name",
    "role": "role",
    "rank": "role",
    "earned credits": "credits",
    "credits": "credits",
    "discount": "discount",
    "alliance contribution rate": "contribution",
    "contribution rate": "contribution",
    "contribution": "contribution",
    "member since": "member_since",
    "since": "member_since",
}


def _map_headers(header_cells: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, text in enumerate(header_cells):
        key = _HEADER_ALIASES.get(text.strip().lower())
        if key and key not in mapping:
            mapping[key] = idx
    return mapping


def parse_members_page(html: str) -> MembersPage:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        return MembersPage(members=[], has_table=False)

    header_cells = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    columns = _map_headers(header_cells)

    body = table.find("tbody") or table
    members: list[dict[str, Any]] = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        link = tr.find("a", href=True)
        if link is None:
            continue
        mc_user_id = extract_user_id(link["href"])
        name = link.get_text(strip=True)
        if not name or mc_user_id is None:
            # A row without a usable user link is not a member row.
            continue

        entry: dict[str, Any] = {"mc_user_id": mc_user_id, "name": name}

        def cell_text(key: str) -> str | None:
            idx = columns.get(key)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx].get_text(" ", strip=True)

        if columns:
            entry["role"] = cell_text("role")
            entry["earned_credits"] = parse_int(cell_text("credits"))
            entry["contribution_rate"] = parse_percent(cell_text("contribution"))
            entry["raw_member_since"] = cell_text("member_since")
        else:
            _apply_heuristics(entry, cells, name)

        members.append(entry)

    return MembersPage(members=members, has_table=True)


def _apply_heuristics(entry: dict[str, Any], cells, name: str) -> None:
    """Fallback when the table has no headers.

    Contribution rate is taken as the LAST percentage cell (the discount
    column precedes the contribution column on MissionChief).
    """
    entry.setdefault("role", None)
    entry.setdefault("earned_credits", None)
    entry.setdefault("contribution_rate", None)
    entry.setdefault("raw_member_since", None)

    percents: list[float] = []
    for td in cells:
        text = td.get_text(" ", strip=True)
        if not text:
            continue
        if entry["role"] is None and not any(c.isdigit() for c in text) and name not in text:
            entry["role"] = text
        if entry["earned_credits"] is None and "credit" in text.lower():
            entry["earned_credits"] = parse_int(text)
        pct = parse_percent(text)
        if pct is not None:
            percents.append(pct)
    if percents:
        entry["contribution_rate"] = percents[-1]


def validate_members_page(page: MembersPage, page_number: int) -> None:
    """Page 1 must contain a members table, otherwise the layout changed
    or we're looking at an error page — fail loudly instead of treating
    it as an empty roster."""
    if page_number == 1 and not page.has_table:
        raise ParseError("Members page 1 has no table — layout change or not logged in")
