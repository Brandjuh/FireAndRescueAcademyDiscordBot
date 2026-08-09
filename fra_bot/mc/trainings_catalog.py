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
    #: Copies of the class requested ("3x HazMat", "x3 HazMat"); the
    #: services clamp this to their per-request maximum (4).
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


#: Extra accepted spellings per course, keyed by the NORMALIZED catalog
#: name (so they apply to the live-harvested catalog too).
#:
#: Fuzzy matching cannot cover SHORT names: "swot" against "swat" scores
#: 0.5 — nowhere near MATCH_THRESHOLD — so a member typing the very common
#: "SWOT" got silence. Typos actually seen on the board therefore get an
#: explicit entry here, which is exact (score 1.0) and loosens no
#: threshold. Matcher-side only: ``_normalize``/``normalized_equals`` stay
#: untouched, so the academy dropdown lookup keeps using the real name.
#: NB: an alias containing the word "and" can never match — the chunk
#: splitter in ``match_trainings`` splits on it.
COURSE_ALIASES: dict[str, tuple[str, ...]] = {
    "swat": ("SWOT", "S.W.A.T."),
}

#: Ambiguous names (same course in several academies) that a BARE post
#: may still resolve, keyed by normalized name → discipline. Reserved for
#: names that literally name their academy: "EMS Mobile Command" says
#: EMS, so demanding "EMS - EMS Mobile Command" only produced fire's
#: plain "Mobile command" by accident. An explicit prefix always wins
#: over this preference; every other ambiguous name keeps requiring one.
PREFERRED_DISCIPLINE: dict[str, str] = {
    "ems mobile command": "ems",
}

#: Board shorthand codes: one per (discipline, course), shown as
#: ``[CODE]`` before each guide line and typed by members in any casing
#: (with or without the brackets — normalization strips them). A code
#: names its academy too, so duplicated courses get an academy letter
#: (CCF/CCE, LGTF/LGTC) and a bare code never needs a discipline prefix.
#: Word-like codes (RIOT, BOMB, DRONE) are deliberate: someone typing
#: that word alone on the TRAINING board means exactly that course.
#: "MOB" not "MC" (members use MC for MissionChief); "ABF" not "AIR"
#: (too generic a word). Codes are STABLE — never repurpose one, or an
#: old guide screenshot opens the wrong class.
TRAINING_CODES: tuple[tuple[str, str, str], ...] = (
    ("fire", "ALS Medical Training for Fire Apparatus", "ALSF"),
    ("fire", "ARFF-Training", "ARFF"),
    ("fire", "Airborne firefighting", "ABF"),
    ("fire", "Critical Care", "CCF"),
    ("fire", "EMS Mobile Command", "EMCF"),
    ("fire", "HazMat", "HAZ"),
    ("fire", "Heavy Machinery Operating", "HMO"),
    ("fire", "Hooklift Truck Driving", "HTD"),
    ("fire", "Hotshot Crew Training", "HSC"),
    ("fire", "Law Enforcement for Arson Investigation", "ARSON"),
    ("fire", "Lifeguard Supervisor", "LGSF"),
    ("fire", "Lifeguard Training", "LGTF"),
    ("fire", "Mobile command", "MOB"),
    ("fire", "Ocean Navigation", "ONF"),
    ("fire", "Search and Rescue Training", "SAR"),
    ("fire", "Smoke Jumper Training", "SJT"),
    ("fire", "Swift water rescue", "SWRF"),
    ("fire", "Tactical Medic Training", "TMF"),
    ("fire", "Technical Rescue Training", "TRT"),
    ("fire", "Traffic Control Training", "TCF"),
    ("fire", "Truck Driver's License", "TDLF"),
    ("fire", "Wildland Lead Pilot Training", "WLP"),
    ("fire", "Wildland Mobile Command Center Training", "WMC"),
    ("police", "Drone Operator", "DRONE"),
    ("police", "Environmental Game Warden", "EGW"),
    ("police", "FBI Bomb Technician", "BOMB"),
    ("police", "FBI Mobile Center Commander", "FBIMC"),
    ("police", "K-9", "K9"),
    ("police", "Ocean Navigation", "ONP"),
    ("police", "Police Aviation", "AVI"),
    ("police", "Police Motorcycle", "MOTO"),
    ("police", "Police Operations Management", "POM"),
    ("police", "Police Supervisor / Sheriff", "SHER"),
    ("police", "Riot Police Training", "RIOT"),
    ("police", "SWAT", "SWAT"),
    ("police", "Sharpshooter Training", "SSP"),
    ("police", "Swift water rescue", "SWRP"),
    ("police", "Tactical Rescue Training", "TACR"),
    ("police", "Traffic Control Training", "TCP"),
    ("ems", "ALS Medical Training for Fire Apparatus", "ALSE"),
    ("ems", "Critical Care", "CCE"),
    ("ems", "EMS Mobile Command", "EMC"),
    ("ems", "Hazmat Medic Training", "HMT"),
    ("ems", "Mountain Dog Training", "MDT"),
    ("ems", "Mountain Rescue Certificate", "MRC"),
    ("ems", "Tactical Medic Training", "TME"),
    ("ems", "Truck Driver's License", "TDLE"),
    ("coastal", "Coastal Air Rescue Operations", "CARO"),
    ("coastal", "Lifeguard Supervisor", "LGSC"),
    ("coastal", "Lifeguard Training", "LGTC"),
    ("coastal", "Ocean Navigation", "ONC"),
    ("coastal", "Sharpshooter Training", "SSC"),
    ("coastal", "Swift water rescue", "SWRC"),
    ("coastal", "TACLET", "TACLET"),
)


def _build_code_lookup() -> dict[str, tuple[str, str]]:
    lookup: dict[str, tuple[str, str]] = {}
    for discipline, name, code in TRAINING_CODES:
        lookup[_normalize(code)] = (discipline, name)
    return lookup


_CODE_LOOKUP = _build_code_lookup()


def course_code(discipline: str, name: str) -> str | None:
    """The board shorthand for one course, for the guide renderer.
    Normalized name comparison, so a live-harvested spelling that only
    differs in casing still finds its code."""
    key = _normalize(name)
    for code_discipline, code_name, code in TRAINING_CODES:
        if code_discipline == discipline and _normalize(code_name) == key:
            return code
    return None


def _strip_suffixes(variant: str) -> list[str]:
    out = [variant]
    for suffix in (" training", " course", " certification", " certificate"):
        if variant.endswith(suffix):
            out.append(variant[: -len(suffix)].strip())
    return out


def _match_variants(name: str) -> list[tuple[str, bool]]:
    """``(variant, exact_only)`` spellings to test a chunk against.

    The course's own name (and its suffix-stripped forms) goes through the
    full scoring ladder — substring and fuzzy included, so typos of long
    names still land. Entries from :data:`COURSE_ALIASES` are marked
    ``exact_only``: they are alternative SPELLINGS, not fuzz seeds. A
    4-letter alias like "swot" would otherwise match inside longer words
    through the substring branch ("swotting").
    """
    normalized = _normalize(name)
    pairs: list[tuple[str, bool]] = [
        (variant, False) for variant in _strip_suffixes(normalized)
    ]
    for alias in COURSE_ALIASES.get(normalized, ()):
        pairs.extend(
            (variant, True) for variant in _strip_suffixes(_normalize(alias))
        )
    seen: set[str] = set()
    ordered: list[tuple[str, bool]] = []
    for variant, exact_only in pairs:
        if variant and variant not in seen:
            seen.add(variant)
            ordered.append((variant, exact_only))
    return ordered


def _alias_variants(name: str) -> list[str]:
    return [variant for variant, _ in _match_variants(name)]


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


#: Both orders of the leading copy count. Members write "x4 SWAT" at
#: least as often as "4x SWAT"; the x-first form used to fall through
#: with count=1 while the leftover "x4 " still matched the course name on
#: a word boundary — so the request silently opened ONE class.
_COUNT_PREFIX_RE = re.compile(
    r"^\s*(?:(\d+)\s*[x×]|[x×]\s*(\d+))\s+", re.IGNORECASE
)
_COUNT_SUFFIX_RE = re.compile(r"\s+[x×]\s*(\d+)\s*$", re.IGNORECASE)
#: Board copy-count cap — mirrors the services' MAX_CLASSES_PER_REQUEST.
_MAX_COUNT = 4


def _clamp_count(raw: str) -> int:
    return max(1, min(_MAX_COUNT, int(raw)))


def _extract_count(chunk: str) -> tuple[str, int]:
    """Split a copy count off a chunk: "3x HazMat" / "x3 HazMat" /
    "HazMat x3" → 3. A bare leading number ("3 HazMat") is deliberately
    NOT a count: course text carries stray numbers too."""
    match = _COUNT_PREFIX_RE.match(chunk)
    if match:
        return chunk[match.end():], _clamp_count(match.group(1) or match.group(2))
    match = _COUNT_SUFFIX_RE.search(chunk)
    if match:
        return chunk[: match.start()], _clamp_count(match.group(1))
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

        # Board codes first: an exact whole-word code resolves straight
        # to its (discipline, course) — never fuzzed, like the
        # exact_only aliases. When a chunk carries a code the chunk is
        # DONE: a copied guide line "[HAZ] HazMat (3 days)" holds the
        # code AND the name of the same course, and letting both paths
        # score would double the copy count. Distinct courses dedupe on
        # (discipline, name) for the same reason.
        coded: dict[tuple[str, str], int] = {}
        for word in normalized_chunk.split():
            resolved = _CODE_LOOKUP.get(word)
            if resolved is None:
                continue
            discipline, name = resolved
            if forced_discipline and discipline != forced_discipline:
                continue
            entry = next(
                (
                    (cname, cdays)
                    for cname, cdays in catalog.get(discipline, {}).items()
                    if _normalize(cname) == _normalize(name)
                ),
                None,
            )
            if entry is None:  # course gone from the active catalog
                continue
            coded[(discipline, entry[0])] = entry[1]
        if coded:
            for (discipline, name), days in coded.items():
                existing = matches.get((discipline, name))
                chunk_count = count
                if existing is not None:
                    chunk_count = min(_MAX_COUNT, existing.count + count)
                matches[(discipline, name)] = TrainingMatch(
                    discipline=discipline, name=name, duration_days=days,
                    count=chunk_count,
                )
            continue

        # Every candidate first, the verdict after: ambiguous names used
        # to be diverted to the warnings the moment they hit, which let a
        # SHORTER name contained in the same text ("mobile command" ⊂
        # "ems mobile command") take the chunk with a word-boundary 1.0
        # — the member got a fire "Mobile command" instead of the course
        # they named. (score, matched-variant length, disc, name, days):
        # the length breaks score ties toward the most specific name, so
        # "Wildland Mobile Command Center Training" beats "Mobile
        # command" instead of losing on dict order.
        best: tuple[float, int, str, str, int] | None = None
        for discipline, trainings in catalog.items():
            if forced_discipline and discipline != forced_discipline:
                continue
            for name, days in trainings.items():
                for variant, exact_only in _match_variants(name):
                    compact = variant.replace(" ", "")
                    score = 0.0
                    if re.search(rf"\b{re.escape(variant)}\b", normalized_chunk) or (
                        compact != variant
                        and re.search(rf"\b{re.escape(compact)}\b", normalized_chunk)
                    ):
                        score = 1.0
                    elif exact_only:
                        # A known alternative spelling only counts when it
                        # stands as a whole word — never as a fuzz seed.
                        continue
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
                    candidate = (score, len(variant), discipline, name, days)
                    if best is None or candidate[:2] > best[:2]:
                        best = candidate
        if best is not None:
            _, _, discipline, name, days = best
            key = _normalize(name)
            if forced_discipline is None and key in ambiguous:
                # The winner exists in several academies. A name that
                # literally names its academy resolves by preference;
                # everything else stays an explicit-prefix question — and
                # deliberately does NOT fall back to a lesser candidate
                # (a worse reading of the same words was the old hijack).
                preferred = PREFERRED_DISCIPLINE.get(key)
                # Normalized lookup: a live-harvested catalog may spell
                # the same course with different casing per agency.
                preferred_entry = (
                    next(
                        (
                            (pname, pdays)
                            for pname, pdays in catalog[preferred].items()
                            if _normalize(pname) == key
                        ),
                        None,
                    )
                    if preferred and preferred in ambiguous[key]
                    else None
                )
                if preferred_entry is not None:
                    discipline = preferred
                    name, days = preferred_entry
                else:
                    ambiguities[key] = AmbiguousMatch(
                        name=name, disciplines=ambiguous[key]
                    )
                    continue
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


#: Suggestion-only floor, deliberately far below MATCH_THRESHOLD: a
#: near-miss is worth naming in a board hint ("did you mean …?") but must
#: never open a class by itself.
SUGGEST_THRESHOLD = 0.45


def suggest_courses(text: str, catalog=None, *, limit: int = 3) -> list[str]:
    """Closest course names for text that matched NOTHING — used purely to
    make the board hint helpful. Never opens anything."""
    normalized = _normalize(text)
    if not normalized:
        return []
    scored: list[tuple[float, str]] = []
    for trainings in (catalog or DISCIPLINES).values():
        for name in trainings:
            best = max(
                SequenceMatcher(None, variant, normalized).ratio()
                for variant in _alias_variants(name)
            )
            # Also score against each word, so one wrong word in a longer
            # post still surfaces the course the member meant.
            for word in normalized.split():
                if len(word) < 3:
                    continue
                best = max(
                    best,
                    max(
                        SequenceMatcher(None, variant, word).ratio()
                        for variant in _alias_variants(name)
                    ),
                )
            if best >= SUGGEST_THRESHOLD:
                scored.append((best, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    out: list[str] = []
    for _, name in scored:
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def normalized_equals(a: str, b: str) -> bool:
    return _normalize(a) == _normalize(b)
