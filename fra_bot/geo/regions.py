"""Region resolution for event pings — the reference bot's logic, verbatim.

Maps a mission/event address to the Discord region role that should be
pinged alongside Notify-Event: US state roles ("New York (NY)"), the
Bermuda role, or a country role ("Germany (DE)") for worldwide play.

Resolution order (same as the reference eventpinger):

1. Geocoded address details (authoritative): country/state from the
   geocoder's structured result.
2. Text heuristics on the address string: Bermuda postal prefixes and
   parish names, US ZIP codes (only with US context), well-known place
   aliases, spelled-out state names.

An unresolved address pings Notify-Event only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

US_REGION_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

REGION_ROLE_NAMES = {
    **{code: f"{name} ({code})" for code, name in US_REGION_NAMES.items()},
    "BM": "Bermuda (BM)",
}
US_STATE_NAME_TO_CODE = {name.casefold(): code for code, name in US_REGION_NAMES.items()}

# ZIP prefixes are intentionally state-level only. Ambiguous or non-state US
# territories are omitted.
US_ZIP3_RANGES = (
    (10, 27, "MA"),
    (28, 29, "RI"),
    (30, 38, "NH"),
    (39, 49, "ME"),
    (50, 59, "VT"),
    (60, 62, "CT"),
    (64, 69, "CT"),
    (70, 89, "NJ"),
    (100, 149, "NY"),
    (150, 196, "PA"),
    (197, 199, "DE"),
    (200, 200, "DC"),
    (201, 201, "VA"),
    (202, 205, "DC"),
    (206, 219, "MD"),
    (220, 246, "VA"),
    (247, 268, "WV"),
    (270, 289, "NC"),
    (290, 299, "SC"),
    (300, 319, "GA"),
    (320, 349, "FL"),
    (350, 369, "AL"),
    (370, 385, "TN"),
    (386, 397, "MS"),
    (398, 399, "GA"),
    (400, 427, "KY"),
    (430, 459, "OH"),
    (460, 479, "IN"),
    (480, 499, "MI"),
    (500, 528, "IA"),
    (530, 549, "WI"),
    (550, 567, "MN"),
    (570, 577, "SD"),
    (580, 588, "ND"),
    (590, 599, "MT"),
    (600, 629, "IL"),
    (630, 658, "MO"),
    (660, 679, "KS"),
    (680, 693, "NE"),
    (700, 714, "LA"),
    (716, 729, "AR"),
    (730, 749, "OK"),
    (750, 799, "TX"),
    (800, 816, "CO"),
    (820, 831, "WY"),
    (832, 838, "ID"),
    (840, 849, "UT"),
    (850, 865, "AZ"),
    (870, 884, "NM"),
    (885, 885, "TX"),
    (889, 898, "NV"),
    (900, 961, "CA"),
    (967, 968, "HI"),
    (970, 979, "OR"),
    (980, 994, "WA"),
    (995, 999, "AK"),
)

US_PLACE_ALIASES = {
    "new york": "NY",
    "the bronx": "NY",
    "bronx": "NY",
    "manhattan": "NY",
    "brooklyn": "NY",
    "queens": "NY",
    "staten island": "NY",
    "los angeles": "CA",
    "san francisco": "CA",
    "san diego": "CA",
    "miami": "FL",
    "orlando": "FL",
    "tampa": "FL",
    "jacksonville": "FL",
    "houston": "TX",
    "dallas": "TX",
    "austin": "TX",
    "san antonio": "TX",
    "chicago": "IL",
    "philadelphia": "PA",
    "washington dc": "DC",
    "washington, dc": "DC",
    "district of columbia": "DC",
}

BERMUDA_POSTAL_PREFIXES = {"CR", "DV", "FL", "GE", "HA", "HM", "HS", "MA", "PG", "SB", "SN", "WK"}
BERMUDA_PLACE_ALIASES = {
    "bermuda",
    "flatts",
    "hamilton",
    "devonshire",
    "paget",
    "pembroke",
    "sandys",
    "smiths",
    "smith's",
    "southampton",
    "st george",
    "st. george",
    "st georges",
    "st. georges",
    "warwick",
}

ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
BERMUDA_POSTAL_RE = re.compile(r"\b([A-Z]{2})\s?\d{2}\b", re.IGNORECASE)


@dataclass(frozen=True)
class RegionMatch:
    code: str
    name: str
    source: str
    role_names: tuple[str, ...] = ()


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def resolve_region(address: str) -> RegionMatch | None:
    """Text-heuristic fallback: Bermuda first (its postal codes look like
    US state abbreviations), then US."""
    text = normalize_text(address)
    if not text:
        return None

    for resolver in (resolve_bermuda, resolve_us):
        match = resolver(text)
        if match:
            return match
    return None


def region_from_address_details(details: Any) -> RegionMatch | None:
    """RegionMatch from a geocoder's structured address dict (the
    ``address`` object of a Nominatim/maps.co result)."""
    if not isinstance(details, dict):
        return None

    country_code = normalize_text(details.get("country_code", "")).casefold()
    country_name = normalize_text(details.get("country", ""))
    country = country_name.casefold()
    if country_code == "bm" or country == "bermuda":
        return RegionMatch("BM", REGION_ROLE_NAMES["BM"], "geocode_country")

    if country_code == "us" or country == "united states":
        state_name = normalize_text(details.get("state", "")).casefold()
        state_code = normalize_text(details.get("state_code", "")).upper()
        if state_code in REGION_ROLE_NAMES:
            return RegionMatch(state_code, REGION_ROLE_NAMES[state_code], "geocode_state_code")

        resolved_code = US_STATE_NAME_TO_CODE.get(state_name)
        if resolved_code:
            return RegionMatch(resolved_code, REGION_ROLE_NAMES[resolved_code], "geocode_state")

        return None

    if country_code or country_name:
        return country_region_match(country_name, country_code, "geocode_country")

    return None


def country_region_match(country_name: str, country_code: str, source: str) -> RegionMatch | None:
    clean_country = normalize_text(country_name)
    clean_code = normalize_text(country_code).upper()
    if not clean_country and not clean_code:
        return None

    display_name = f"{clean_country} ({clean_code})" if clean_country and clean_code else clean_country or clean_code
    role_names = tuple(dict.fromkeys(name for name in (display_name, clean_country) if name))
    code = f"COUNTRY:{clean_code}" if clean_code else f"COUNTRY:{display_name.casefold()}"
    return RegionMatch(code, display_name, source, role_names)


def resolve_bermuda(address: str) -> RegionMatch | None:
    for match in BERMUDA_POSTAL_RE.finditer(address):
        prefix = match.group(1).upper()
        if prefix in BERMUDA_POSTAL_PREFIXES:
            return RegionMatch("BM", REGION_ROLE_NAMES["BM"], "bermuda_postal_code")

    lowered = address.casefold()
    for alias in BERMUDA_PLACE_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return RegionMatch("BM", REGION_ROLE_NAMES["BM"], "bermuda_place")
    return None


def resolve_us(address: str) -> RegionMatch | None:
    has_context = has_us_context(address)
    zip_match = ZIP_RE.search(address)
    if zip_match and has_context:
        state_code = state_from_zip(zip_match.group(1))
        if state_code:
            return RegionMatch(state_code, REGION_ROLE_NAMES[state_code], "us_zip")

    lowered = address.casefold()
    for alias, code in sorted(US_PLACE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return RegionMatch(code, REGION_ROLE_NAMES[code], "us_place")

    for code, name in US_REGION_NAMES.items():
        if re.search(rf"\b{re.escape(name.casefold())}\b", lowered):
            return RegionMatch(code, REGION_ROLE_NAMES[code], "us_state_name")

    return None


def has_us_context(address: str) -> bool:
    lowered = address.casefold()
    if re.search(r"\b(?:united states|usa|u\.s\.a\.|u\.s\.)\b", lowered):
        return True

    for alias in US_PLACE_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return True

    for name in US_REGION_NAMES.values():
        if re.search(rf"\b{re.escape(name.casefold())}\b", lowered):
            return True

    return False


def state_from_zip(zip_code: str) -> str | None:
    try:
        prefix = int(str(zip_code)[:3])
    except (TypeError, ValueError):
        return None

    for start, end, state_code in US_ZIP3_RANGES:
        if start <= prefix <= end:
            return state_code
    return None


def find_role_by_name(guild: Any, expected_name: str):
    expected = expected_name.casefold()
    for role in getattr(guild, "roles", []) or []:
        if getattr(role, "name", "").casefold() == expected:
            return role
    return None


def find_region_role(guild: Any, region: RegionMatch | str | None):
    """The guild role for a region: exact (case-insensitive) name match on
    the match's candidate names, then any role ending in "(CODE)"."""
    if not region:
        return None

    if isinstance(region, RegionMatch):
        region_code = region.code
        candidates = list(region.role_names)
        if region_code in REGION_ROLE_NAMES:
            candidates.append(REGION_ROLE_NAMES[region_code])
        candidates.append(region.name)
    else:
        region_code = region
        candidates = [REGION_ROLE_NAMES.get(region_code, "")]

    seen = set()
    for expected_name in candidates:
        normalized = normalize_text(expected_name)
        if not normalized or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())

        role = find_role_by_name(guild, normalized)
        if role:
            return role

    if region_code not in REGION_ROLE_NAMES:
        return None

    suffix = f"({region_code})".casefold()
    for role in getattr(guild, "roles", []) or []:
        if getattr(role, "name", "").casefold().endswith(suffix):
            return role
    return None
