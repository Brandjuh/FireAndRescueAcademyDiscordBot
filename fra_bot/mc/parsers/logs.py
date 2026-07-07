"""Parser for alliance logs (/alliance_logfiles?page=N).

Table columns: DATE | EXECUTED BY | DESCRIPTION | AFFECTED. There is no
machine-readable action type in the HTML, so the action is classified
from the description text. The classification lives in ACTION_PATTERNS
as *data* (ordered substring → key), shared with the Discord publisher,
so scraper and presentation can never drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from .common import extract_user_id, normalize_mc_timestamp, parse_int, signature_of

# Ordered: first match wins. Keep specific phrases before broad ones
# (e.g. the co-admin patterns run before "removed admin"/"set as admin",
# and the broad "contribution" catch-all stays last).
ACTION_PATTERNS: list[tuple[str, str]] = [
    ("added to the alliance", "added_to_alliance"),
    ("application denied", "application_denied"),
    ("left the alliance", "left_alliance"),
    ("kicked from the alliance", "kicked_from_alliance"),
    ("removed co-admin", "removed_co_admin"),
    ("set as co-admin", "set_co_admin"),
    ("promoted to co-admin", "set_co_admin"),
    ("removed transport admin", "removed_transport_admin"),
    ("set as transport admin", "set_transport_admin"),
    ("transport admin set", "set_transport_admin"),
    ("removed education admin", "removed_education_admin"),
    ("set as education admin", "set_education_admin"),
    ("removed finance admin", "removed_finance_admin"),
    ("set as finance admin", "set_finance_admin"),
    ("removed mod action admin", "removed_mod_action_admin"),
    ("set as mod action admin", "set_mod_action_admin"),
    ("removed admin", "removed_admin"),
    ("set as admin", "set_admin"),
    ("promoted to admin", "set_admin"),
    ("chat ban removed", "chat_ban_removed"),
    ("chat ban set", "chat_ban_set"),
    ("not allowed to apply", "not_allowed_to_apply"),
    ("allowed to apply", "allowed_to_apply"),
    ("created a course", "created_course"),
    ("created course", "created_course"),
    ("course completed", "course_completed"),
    ("completed a course", "course_completed"),
    ("building destroyed", "building_destroyed"),
    ("building constructed", "building_constructed"),
    ("extension started", "extension_started"),
    ("expansion finished", "expansion_finished"),
    ("large scale mission started", "large_mission_started"),
    ("large mission started", "large_mission_started"),
    ("alliance event started", "alliance_event_started"),
    ("removed as staff", "removed_as_staff"),
    ("set as staff", "set_as_staff"),
    ("removed event manager", "removed_event_manager"),
    ("promoted to event manager", "promoted_to_event_manager"),
    ("removed custom large scale mission", "removed_custom_large_scale_mission"),
    ("contributed to the alliance", "contributed_to_alliance"),
    ("contribution", "contributed_to_alliance"),
]

_AFFECTED_TYPES = [
    (re.compile(r"/buildings/(\d+)"), "building"),
    (re.compile(r"/(?:users|profile)/(\d+)"), "user"),
    (re.compile(r"/missions/(\d+)"), "mission"),
    (re.compile(r"/vehicles/(\d+)"), "vehicle"),
]


def classify_action(description: str) -> str:
    lowered = description.lower()
    for needle, key in ACTION_PATTERNS:
        if needle in lowered:
            return key
    return "unknown"


@dataclass
class LogsPage:
    rows: list[dict[str, Any]]  # newest first, as displayed
    has_table: bool


def parse_logs_page(html: str) -> LogsPage:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="table") or soup.find("table")
    if table is None:
        return LogsPage(rows=[], has_table=False)
    body = table.find("tbody")
    if body is None:
        return LogsPage(rows=[], has_table=False)

    rows: list[dict[str, Any]] = []
    for tr in body.find_all("tr"):
        cols = tr.find_all("td")
        if len(cols) < 3:
            continue

        raw_timestamp = cols[0].get_text(strip=True)

        executed_name, executed_mc_id = None, None
        executed_link = cols[1].find("a", href=True)
        if executed_link is not None:
            executed_name = executed_link.get_text(strip=True)
            executed_mc_id = extract_user_id(executed_link["href"])
        else:
            executed_name = cols[1].get_text(strip=True) or None

        desc_col = cols[2]
        contribution_amount = None
        label = desc_col.find("span", class_="label")
        label_text = label.get_text(strip=True) if label else ""
        if label_text:
            match = re.search(r"([-+]?[\d,]+)", label_text)
            if match:
                contribution_amount = parse_int(match.group(1))
                if contribution_amount is not None and match.group(1).startswith("-"):
                    contribution_amount = -contribution_amount
        description = desc_col.get_text(" ", strip=True)
        if label_text:
            description = description.replace(label_text, "").strip()

        affected_name, affected_type, affected_mc_id = None, None, None
        if len(cols) > 3:
            affected_link = cols[3].find("a", href=True)
            if affected_link is not None:
                affected_name = affected_link.get_text(strip=True)
                href = affected_link["href"]
                for pattern, type_name in _AFFECTED_TYPES:
                    match = pattern.search(href)
                    if match:
                        affected_type = type_name
                        affected_mc_id = int(match.group(1))
                        break
            else:
                affected_name = cols[3].get_text(strip=True) or None

        action_key = classify_action(description)
        rows.append(
            {
                "raw_timestamp": raw_timestamp,
                "event_at": normalize_mc_timestamp(raw_timestamp),
                "action_key": action_key,
                "description": description,
                "executed_name": executed_name,
                "executed_mc_id": executed_mc_id,
                "affected_name": affected_name,
                "affected_type": affected_type,
                "affected_mc_id": affected_mc_id,
                "contribution_amount": contribution_amount,
                "signature": signature_of(
                    raw_timestamp,
                    action_key,
                    executed_name,
                    affected_name,
                    description,
                ),
            }
        )

    return LogsPage(rows=rows, has_table=True)
