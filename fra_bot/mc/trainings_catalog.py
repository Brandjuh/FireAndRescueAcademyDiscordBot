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

# Sourced from the live MissionChief USA academy education dropdowns (the
# authoritative list). Names match the dropdown labels EXACTLY so the guide,
# the request matcher and the academy `find_course_value` all agree; durations
# are the "(N days)" from each label. The live harvest still overrides this per
# agency once it has walked the academies, so a future course appears without a
# code change — this is the accurate fallback when no harvest has run yet.
DISCIPLINES: dict[str, dict[str, int]] = {
    "fire": {
        "ALS Medical Training for Fire Apparatus": 3,
        "ARFF-Training": 3,
        "Airborne firefighting": 5,
        "Critical Care": 5,
        "EMS Mobile Command": 7,
        "HazMat": 3,
        "Heavy Machinery Operating": 3,
        "Hooklift Truck Driving": 4,
        "Hotshot Crew Training": 3,
        "Law Enforcement for Arson Investigation": 4,
        "Lifeguard Supervisor": 5,
        "Lifeguard Training": 5,
        "Mobile command": 5,
        "Ocean Navigation": 5,
        "Search and Rescue Training": 4,
        "Smoke Jumper Training": 3,
        "Swift water rescue": 4,
        "Tactical Medic Training": 4,
        "Technical Rescue Training": 4,
        "Traffic Control Training": 3,
        "Truck Driver's License": 2,
        "Wildland Lead Pilot Training": 7,
        "Wildland Mobile Command Center Training": 5,
    },
    "police": {
        "Drone Operator": 5,
        "Environmental Game Warden": 4,
        "FBI Bomb Technician": 5,
        "FBI Mobile Center Commander": 7,
        "K-9": 5,
        "Ocean Navigation": 5,
        "Police Aviation": 7,
        "Police Motorcycle": 3,
        "Police Operations Management": 5,
        "Police Supervisor / Sheriff": 5,
        "Riot Police Training": 3,
        "SWAT": 5,
        "Sharpshooter Training": 5,
        "Swift water rescue": 4,
        "Tactical Rescue Training": 5,
        "Traffic Control Training": 3,
    },
    "ems": {
        "ALS Medical Training for Fire Apparatus": 3,
        "Critical Care": 5,
        "EMS Mobile Command": 7,
        "Hazmat Medic Training": 3,
        "Mountain Dog Training": 5,
        "Mountain Rescue Certificate": 5,
        "Tactical Medic Training": 4,
        "Truck Driver's License": 2,
    },
    "coastal": {
        "Coastal Air Rescue Operations": 5,
        "Lifeguard Supervisor": 5,
        "Lifeguard Training": 5,
        "Ocean Navigation": 5,
        "Sharpshooter Training": 5,
        "Swift water rescue": 4,
        "TACLET": 3,
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
    #: Copies of the class requested ("3x HazMat"); the services clamp
    #: this to their per-request maximum (4).
    count: int = 1


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


def ambiguous_names(catalog=None) -> dict[str, tuple[str, ...]]:
    """Normalized training name → disciplines it exists in (if > 1)."""
    seen: dict[str, list[str]] = {}
    for discipline, trainings in (catalog or DISCIPLINES).items():
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


_COUNT_PREFIX_RE = re.compile(r"^\s*(\d+)\s*[x×]\s+", re.IGNORECASE)
_COUNT_SUFFIX_RE = re.compile(r"\s+[x×]\s*(\d+)\s*$", re.IGNORECASE)
#: Board copy-count cap — mirrors the services' MAX_CLASSES_PER_REQUEST.
_MAX_COUNT = 4


def _extract_count(chunk: str) -> tuple[str, int]:
    """Split a copy count off a chunk: "3x HazMat" / "HazMat x3" → 3."""
    match = _COUNT_PREFIX_RE.match(chunk)
    if match:
        return chunk[match.end():], max(1, min(_MAX_COUNT, int(match.group(1))))
    match = _COUNT_SUFFIX_RE.search(chunk)
    if match:
        return chunk[: match.start()], max(1, min(_MAX_COUNT, int(match.group(1))))
    return chunk, 1


def match_trainings(
    text: str, catalog=None
) -> tuple[list[TrainingMatch], list[AmbiguousMatch]]:
    """Extract training requests from free-form board text.

    ``catalog`` (discipline → {name: days}) overrides the built-in list —
    the trainings service passes the live-harvested academy courses, so a
    course the game added yesterday matches exactly instead of fuzzing
    onto the nearest stale name.
    """
    catalog = catalog or DISCIPLINES
    ambiguous = ambiguous_names(catalog)
    matches: dict[tuple[str, str], TrainingMatch] = {}
    ambiguities: dict[str, AmbiguousMatch] = {}

    chunks = re.split(r"[\n;,/|]+|\band\b|&|\+", text, flags=re.IGNORECASE)
    for raw_chunk in chunks:
        raw_chunk = raw_chunk.strip()
        if not raw_chunk:
            continue
        forced_discipline, remainder = _detect_prefix(raw_chunk)
        remainder, count = _extract_count(remainder)
        normalized_chunk = _normalize(remainder)
        if not normalized_chunk:
            continue

        best: tuple[float, str, str, int] | None = None  # score, disc, name, days
        for discipline, trainings in catalog.items():
            if forced_discipline and discipline != forced_discipline:
                continue
            for name, days in trainings.items():
                is_ambiguous = (
                    forced_discipline is None and _normalize(name) in ambiguous
                )
                for variant in _alias_variants(name):
                    compact = variant.replace(" ", "")
                    score = 0.0
                    if re.search(rf"\b{re.escape(variant)}\b", normalized_chunk) or (
                        compact != variant
                        and re.search(rf"\b{re.escape(compact)}\b", normalized_chunk)
                    ):
                        score = 1.0
                    elif variant in normalized_chunk or normalized_chunk in variant:
                        score = max(
                            0.88,
                            SequenceMatcher(None, variant, normalized_chunk).ratio(),
                        )
                    else:
                        # Pure fuzz is for typos of the WHOLE name. Different
                        # courses sharing a tail ("… Rescue Training") score
                        # deceptively high on raw ratio — "technical rescue
                        # training" hit Search and Rescue Training at 0.784.
                        # Comparing sorted-token forms as well kills those
                        # while genuine typos stay well above the threshold.
                        ratio = SequenceMatcher(
                            None, variant, normalized_chunk
                        ).ratio()
                        token_ratio = SequenceMatcher(
                            None,
                            " ".join(sorted(variant.split())),
                            " ".join(sorted(normalized_chunk.split())),
                        ).ratio()
                        score = min(ratio, token_ratio)
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
            existing = matches.get((discipline, name))
            if existing is not None:
                count = min(_MAX_COUNT, existing.count + count)
            matches[(discipline, name)] = TrainingMatch(
                discipline=discipline, name=name, duration_days=days,
                count=count,
            )

    # Drop ambiguity warnings for names that also matched unambiguously
    # (an explicit prefix elsewhere in the post resolved them).
    for match in matches.values():
        ambiguities.pop(_normalize(match.name), None)
    return list(matches.values()), list(ambiguities.values())


def normalized_equals(a: str, b: str) -> bool:
    return _normalize(a) == _normalize(b)
