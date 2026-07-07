"""Training catalog + free-text matching for board requests.

The catalog mirrors MissionChief's academy courses per discipline
(name → duration in days). Matching accepts free-form board text:

* exact / alias matches (whole word, "training"/"course" suffixes
  optional),
* fuzzy matches via difflib with a 0.78 threshold,
* names existing in multiple academy types are AMBIGUOUS and require a
  discipline prefix (e.g. "Water Rescue - Lifeguard Training") so we
  never open a class in the wrong academy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

DISCIPLINES: dict[str, dict[str, int]] = {
    "fire": {
        "Airport Firefighter": 3,
        "ARFF Crash Captain": 3,
        "Dive Certification": 4,
        "EMS Mobile Command Specialist": 3,
        "EMS Mobile Intensive Care Training": 3,
        "Firefighter Boat Docking Training": 3,
        "Foam Firefighting Training": 2,
        "HazMat": 3,
        "Heavy machinery operation certification": 2,
        "Lifeguard Training": 5,
        "Mobile Air Technician": 2,
        "Mobile Command Specialist": 3,
        "Ocean Navigation": 4,
        "Swift Water Rescue Training": 4,
        "Tractor operation certification": 2,
        "Truck Driver's License": 2,
        "Wildland fire suppression aircraft pilot training": 5,
        "Airborne firefighting observer training": 3,
        "Crane Operator": 2,
    },
    "police": {
        "K-9": 5,
        "Police Aviation": 5,
        "Riot Police Training": 3,
        "Riot police commander training": 3,
        "SWAT": 5,
        "SWAT Commander Training": 3,
        "Sharpshooter Training": 4,
        "Traffic Control Training": 2,
        "Police Motorcycle Training": 2,
        "FBI Bomb Technician": 4,
        "FBI Field Agent": 4,
        "FBI Special Agent in Charge": 5,
        "FBI Mobile Command Specialist": 3,
        "Drone Operator Training": 2,
    },
    "ems": {
        "Critical Care": 3,
        "EMS Command": 3,
        "HEMS Doctor": 5,
        "HEMS Paramedic": 5,
        "Search and Rescue Training": 3,
        "Ocean Navigation": 4,
        "Swift Water Rescue Training": 4,
        "Coastal Rescue Training": 4,
        "Lifeguard Training": 5,
        "Tactical medic training": 4,
        "Mass Casualty Unit Training": 3,
    },
    "coastal": {
        "Lifeguard Training": 5,
        "TACLET": 3,
        "Coastal Rescue Training": 4,
        "Ocean Navigation": 4,
        "Swift Water Rescue Training": 4,
        "Coastal Air Rescue Operator": 5,
        "Interception Officer": 4,
    },
}

# Prefixes members may use to disambiguate ("Fire Station - Lifeguard
# Training"). Keys are search tokens, values are discipline keys.
DISCIPLINE_PREFIXES: dict[str, str] = {
    "fire station": "fire",
    "fire": "fire",
    "police": "police",
    "ems": "ems",
    "rescue": "ems",
    "ems / rescue": "ems",
    "water rescue": "coastal",
    "coastal": "coastal",
}

MATCH_THRESHOLD = 0.78


@dataclass(frozen=True)
class TrainingMatch:
    discipline: str
    name: str
    duration_days: int


@dataclass(frozen=True)
class AmbiguousMatch:
    name: str
    disciplines: tuple[str, ...]


def _normalize(text: str) -> str:
    text = re.sub(r"\(\s*\d+\s*days?\s*\)", " ", text, flags=re.IGNORECASE)
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _alias_variants(name: str) -> list[str]:
    normalized = _normalize(name)
    variants = [normalized]
    for suffix in (" training", " course", " certification", " certificate"):
        if normalized.endswith(suffix):
            variants.append(normalized[: -len(suffix)].strip())
    return [v for v in variants if v]


def ambiguous_names() -> dict[str, tuple[str, ...]]:
    """Normalized training name → disciplines it exists in (if > 1)."""
    seen: dict[str, list[str]] = {}
    for discipline, trainings in DISCIPLINES.items():
        for name in trainings:
            seen.setdefault(_normalize(name), []).append(discipline)
    return {
        name: tuple(disciplines)
        for name, disciplines in seen.items()
        if len(disciplines) > 1
    }


def _detect_prefix(chunk: str) -> tuple[str | None, str]:
    """Split an explicit discipline prefix off a request chunk."""
    for separator in (" - ", ": ", " – "):
        if separator in chunk:
            head, tail = chunk.split(separator, 1)
            discipline = DISCIPLINE_PREFIXES.get(_normalize(head))
            if discipline:
                return discipline, tail
    return None, chunk


def match_trainings(text: str) -> tuple[list[TrainingMatch], list[AmbiguousMatch]]:
    """Extract training requests from free-form board text."""
    ambiguous = ambiguous_names()
    matches: dict[tuple[str, str], TrainingMatch] = {}
    ambiguities: dict[str, AmbiguousMatch] = {}

    chunks = re.split(r"[\n;,/|]+|\band\b|&|\+", text, flags=re.IGNORECASE)
    for raw_chunk in chunks:
        raw_chunk = raw_chunk.strip()
        if not raw_chunk:
            continue
        forced_discipline, remainder = _detect_prefix(raw_chunk)
        normalized_chunk = _normalize(remainder)
        if not normalized_chunk:
            continue

        best: tuple[float, str, str, int] | None = None  # score, disc, name, days
        for discipline, trainings in DISCIPLINES.items():
            if forced_discipline and discipline != forced_discipline:
                continue
            for name, days in trainings.items():
                is_ambiguous = (
                    forced_discipline is None and _normalize(name) in ambiguous
                )
                for variant in _alias_variants(name):
                    score = 0.0
                    if re.search(rf"\b{re.escape(variant)}\b", normalized_chunk):
                        score = 1.0
                    elif variant in normalized_chunk or normalized_chunk in variant:
                        score = max(
                            0.88,
                            SequenceMatcher(None, variant, normalized_chunk).ratio(),
                        )
                    else:
                        score = SequenceMatcher(None, variant, normalized_chunk).ratio()
                    if score < MATCH_THRESHOLD:
                        continue
                    if is_ambiguous:
                        key = _normalize(name)
                        ambiguities[key] = AmbiguousMatch(
                            name=name, disciplines=ambiguous[key]
                        )
                        continue
                    if best is None or score > best[0]:
                        best = (score, discipline, name, days)
        if best is not None:
            _, discipline, name, days = best
            matches[(discipline, name)] = TrainingMatch(
                discipline=discipline, name=name, duration_days=days
            )

    # Drop ambiguity warnings for names that also matched unambiguously
    # (an explicit prefix elsewhere in the post resolved them).
    for match in matches.values():
        ambiguities.pop(_normalize(match.name), None)
    return list(matches.values()), list(ambiguities.values())


def normalized_equals(a: str, b: str) -> bool:
    return _normalize(a) == _normalize(b)
