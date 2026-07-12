"""The MissionChief vehicle catalog, sourced from the community LSS-Manager
project (the same data the game's own frontend maps to, kept current in
``en_US``). MissionChief itself exposes no clean vehicle-list endpoint the
way missions have ``/einsaetze.json``; LSSM's ``vehicles.ts`` is the
authoritative machine-readable catalog and is what the reference bot used.

Two files are fetched:

* ``vehicles.ts`` — every vehicle type keyed by its numeric id, with the
  name (``caption``), price (``credits``/``coins``), crew (``staff``),
  which building types can buy it (``possibleBuildings``), tanks, pump, and
  any required trainings (nested under ``staff.training``),
* ``buildings.ts`` — building id → name, to turn ``possibleBuildings`` into
  readable building names.

``vehicles.ts`` is a plain TypeScript object literal (``export default { … }
satisfies …``), JSON-incompatible only in small ways — unquoted keys,
single-quoted strings, ``19_200`` numeric separators and trailing commas —
so :func:`parse_ts_module` tokenises it into data. ``buildings.ts`` is NOT
pure data: it carries arrow functions (``maxExtensionsFunction``), spreads
(``...Array(n).fill(x)``) and function calls in value positions, so it can't
be parsed as a literal. We only ever need the id → name map from it, so
:func:`parse_building_names` extracts that with a targeted line scan and
ignores everything else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

LSSM_BASE = "https://raw.githubusercontent.com/LSS-Manager/LSSM-V.4/dev/src/i18n/en_US"
VEHICLES_URL = f"{LSSM_BASE}/vehicles.ts"
BUILDINGS_URL = f"{LSSM_BASE}/buildings.ts"

FORMAT_VERSION = "vehicles-forum-v1"


# ---------------------------------------------------------------------------
# TypeScript object-literal parser (tokeniser + recursive descent)
# ---------------------------------------------------------------------------

class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any = None) -> None:
        self.kind = kind
        self.value = value


def _tokenize(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in "{}[]:,":
            toks.append(_Tok(c))
            i += 1
            continue
        if c in "'\"":
            # String: single- or double-quoted, JS escapes honoured. A
            # single-quoted literal never contains a bare single quote (LSSM
            # switches to double quotes for names with apostrophes).
            quote = c
            i += 1
            buf: list[str] = []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    esc = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc))
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1  # closing quote
            toks.append(_Tok("str", "".join(buf)))
            continue
        # Number (with 19_200 separators) or bare identifier / keyword.
        j = i
        while j < n and text[j] not in " \t\r\n{}[]:,'\"":
            j += 1
        word = text[i:j]
        i = j
        cleaned = word.replace("_", "")
        if _looks_numeric(cleaned):
            toks.append(_Tok("num", float(cleaned) if "." in cleaned else int(cleaned)))
        elif word == "true":
            toks.append(_Tok("lit", True))
        elif word == "false":
            toks.append(_Tok("lit", False))
        elif word in ("null", "undefined"):
            toks.append(_Tok("lit", None))
        else:
            toks.append(_Tok("ident", word))
    return toks


def _looks_numeric(word: str) -> bool:
    if not word:
        return False
    try:
        float(word)
        return True
    except ValueError:
        return False


class _Parser:
    def __init__(self, toks: list[_Tok]) -> None:
        self._toks = toks
        self._i = 0

    def _peek(self) -> _Tok | None:
        return self._toks[self._i] if self._i < len(self._toks) else None

    def _next(self) -> _Tok:
        tok = self._toks[self._i]
        self._i += 1
        return tok

    def parse_value(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise ValueError("unexpected end of vehicles data")
        if tok.kind == "{":
            return self._parse_object()
        if tok.kind == "[":
            return self._parse_array()
        tok = self._next()
        if tok.kind in ("str", "num", "lit"):
            return tok.value
        if tok.kind == "ident":
            return tok.value  # a bare identifier used as a value (rare)
        raise ValueError(f"unexpected token {tok.kind}")

    def _parse_object(self) -> dict:
        self._next()  # {
        out: dict = {}
        while True:
            tok = self._peek()
            if tok is None:
                raise ValueError("unterminated object")
            if tok.kind == "}":
                self._next()
                return out
            key_tok = self._next()
            if key_tok.kind not in ("str", "ident", "num"):
                raise ValueError(f"bad object key {key_tok.kind}")
            key = str(key_tok.value)
            colon = self._next()
            if colon.kind != ":":
                raise ValueError("expected ':' after key")
            out[key] = self.parse_value()
            sep = self._peek()
            if sep is not None and sep.kind == ",":
                self._next()

    def _parse_array(self) -> list:
        self._next()  # [
        out: list = []
        while True:
            tok = self._peek()
            if tok is None:
                raise ValueError("unterminated array")
            if tok.kind == "]":
                self._next()
                return out
            out.append(self.parse_value())
            sep = self._peek()
            if sep is not None and sep.kind == ",":
                self._next()


def parse_ts_module(text: str) -> dict:
    """A LSSM ``export default { … } satisfies …`` module → a plain dict
    keyed by the original (string) keys."""
    start = text.find("export default")
    if start != -1:
        text = text[start + len("export default"):]
    brace = text.find("{")
    if brace == -1:
        raise ValueError("no object literal found")
    toks = _tokenize(text[brace:])
    return _Parser(toks).parse_value()


# buildings.ts is not pure data (arrow functions, spreads, function calls in
# value positions), so it can't go through the literal parser. We only need
# the id → name map, and LSSM formats each top-level building as
# ``    <id>: {`` with its caption as the first field, so a targeted line
# scan is both simpler and far more robust than parsing arbitrary JS.
_BUILDING_OPEN_RE = re.compile(r"^ {4}(\d+):\s*\{")
_BUILDING_CAPTION_RE = re.compile(r"""caption:\s*(['"])(.*?)\1""")


def parse_building_names(text: str) -> dict[int, str]:
    """Map building-type id → display name from LSSM ``buildings.ts``.

    Reads only the top-level ``<id>: { caption: '…' }`` entries; the file's
    function/spread-valued fields (and nested extension captions) are
    ignored — we never need them."""
    start = text.find("export default")
    if start != -1:
        text = text[start:]
    names: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        opened = _BUILDING_OPEN_RE.match(line)
        if opened:
            current = int(opened.group(1))
            inline = _BUILDING_CAPTION_RE.search(line)  # caption same line (rare)
            if inline:
                names[current] = inline.group(2).strip()
                current = None
            continue
        if current is not None:
            cap = _BUILDING_CAPTION_RE.search(line)
            if cap:
                names[current] = cap.group(2).strip()
                current = None
    return names


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------

async def _fetch(session: "aiohttp.ClientSession", url: str) -> str:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"{url} returned HTTP {resp.status}")
        return await resp.text()


def normalize_vehicle(vid: int, raw: dict, building_names: dict[int, str]) -> dict:
    """One LSSM vehicle entry → a flat record for the forum."""
    staff = raw.get("staff") or {}
    trainings = _flatten_trainings(staff.get("training") or {})
    buildings = [
        building_names.get(int(b), f"Building {b}")
        for b in (raw.get("possibleBuildings") or [])
    ]
    return {
        "id": int(vid),
        "name": str(raw.get("caption") or f"Vehicle {vid}"),
        "credits": _int_or_none(raw.get("credits")),
        "coins": _int_or_none(raw.get("coins")),
        "staff_min": _int_or_none((staff.get("min"))),
        "staff_max": _int_or_none((staff.get("max"))),
        "buildings": buildings,
        "water_tank": _int_or_none(raw.get("waterTank")),
        "foam_tank": _int_or_none(raw.get("foamTank")),
        "pump_capacity": _int_or_none(raw.get("pumpCapacity")),
        "pump_type": raw.get("pumpType") or None,
        "equipment_capacity": _int_or_none(raw.get("equipmentCapacity")),
        "is_trailer": bool(raw.get("isTrailer")),
        "special": (str(raw.get("special")).strip() or None) if raw.get("special") else None,
        "trainings": trainings,
    }


def _flatten_trainings(training: dict) -> list[str]:
    """``{'Fire Station': {gw_gefahrgut: {all: true}}}`` → readable training
    names, deduped, order-stable."""
    out: list[str] = []
    for building, courses in training.items():
        if not isinstance(courses, dict):
            continue
        for course_key in courses:
            label = _training_label(str(course_key))
            entry = f"{building}: {label}"
            if entry not in out:
                out.append(entry)
    return out


# LSSM keys the training by its German-derived internal id; map the ones the
# US game surfaces to readable names, fall back to a tidied key.
_TRAINING_LABELS = {
    "gw_gefahrgut": "HazMat",
    "gw_hoehenrettung": "Heavy Rescue",
    "erkunder": "Reconnaissance",
    "elw2": "Mobile Command",
    "polizeihubschrauber": "Police Aviation",
    "wasserwerfer": "Water Cannon",
    "sek": "SWAT",
    "fbi": "FBI",
    "atf": "ATF",
}


def _training_label(key: str) -> str:
    if key in _TRAINING_LABELS:
        return _TRAINING_LABELS[key]
    return key.replace("_", " ").title()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def fetch_catalog(session: "aiohttp.ClientSession") -> list[dict]:
    """The full vehicle catalog as normalized records, sorted by id. Raises
    ``ValueError`` when the data can't be fetched or parsed."""
    import aiohttp

    # A total ClientTimeout raises asyncio.TimeoutError (≡ builtins.TimeoutError),
    # which is neither ClientError nor ValueError — catch it (and any raw OSError)
    # explicitly so a slow GitHub never escapes the intended handling: the
    # vehicles fetch degrades to a clean "unusable" summary, the buildings fetch
    # to "Building N" labels — not a bogus 45-minute-timeout admin alert.
    fetch_errors = (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError)
    try:
        vehicles_raw = parse_ts_module(await _fetch(session, VEHICLES_URL))
    except fetch_errors as exc:
        raise ValueError(f"could not load the LSSM vehicle catalog: {exc}") from exc
    building_names: dict[int, str] = {}
    try:
        building_names = parse_building_names(await _fetch(session, BUILDINGS_URL))
    except fetch_errors as exc:
        # Building names are a nicety; degrade to "Building N" rather than fail.
        log.warning("vehicle catalog: could not read building names (%s)", exc)

    catalog: list[dict] = []
    for vid, raw in vehicles_raw.items():
        if not isinstance(raw, dict):
            continue
        try:
            catalog.append(normalize_vehicle(int(vid), raw, building_names))
        except (TypeError, ValueError):
            log.warning("vehicle catalog: skipped unparseable entry %r", vid)
    catalog.sort(key=lambda v: v["id"])
    if not catalog:
        raise ValueError("LSSM vehicle catalog parsed to zero vehicles")
    return catalog


# ---------------------------------------------------------------------------
# Keys, hashes, tags
# ---------------------------------------------------------------------------

def vehicle_key(vehicle: dict) -> str:
    """Stable per-vehicle key (its LSSM id — names can change, ids don't)."""
    return f"veh-{vehicle['id']}"


def _stable(vehicle: dict) -> str:
    return json.dumps(vehicle, sort_keys=True, ensure_ascii=False, default=str)


def data_hash(vehicle: dict) -> str:
    """Hash of the vehicle DATA only (drives update notices)."""
    return hashlib.sha1(_stable(vehicle).encode("utf-8")).hexdigest()[:16]


def content_hash(vehicle: dict) -> str:
    """Hash of data + FORMAT_VERSION (drives re-render on a format bump)."""
    payload = _stable(vehicle) + "|" + FORMAT_VERSION
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# Tag inference: building/category first, then capability tags. The forum is
# capped at 20 tags and 5 per post, so keep the vocabulary tight.
# Needles matched against a vehicle's buyable-building names (lower-cased).
# Chosen against the real LSSM building list so each maps cleanly:
#   Fire  → Fire station/academy, Fire Boat Dock, Firefighting plane, Marshal
#   EMS   → Hospital, Ambulance, Medical helicopter, Clinic, Rescue (EMS)
#   Police→ Police station/academy/aviation, Prison, Federal Police
#   Water → Rescue Boat Dock, Coastal Rescue/Air, Lifeguard Post
# "rescue" alone is deliberately NOT used — it would fold the EMS academy into
# Water Rescue; the boat/coastal/lifeguard needles keep them separate.
_CATEGORY_TAGS = {
    "Fire": ("fire",),
    "EMS": ("ambulance", "hospital", "medical helicopter", "clinic", "(ems)"),
    "Police": ("police", "prison"),
    "Water Rescue": ("rescue boat", "coastal", "lifeguard"),
}
FALLBACK_TAG = "Other"
MAX_TAGS_PER_POST = 5


def infer_tags(vehicle: dict) -> list[str]:
    """Up to :data:`MAX_TAGS_PER_POST` tags: the agency category, plus
    capability flags that make the forum filterable."""
    tags: list[str] = []
    buildings_l = " ".join(vehicle.get("buildings") or []).lower()
    for category, needles in _CATEGORY_TAGS.items():
        if any(n in buildings_l for n in needles):
            tags.append(category)
    if vehicle.get("water_tank") or vehicle.get("pump_type") == "fire":
        tags.append("Water/Pump")
    if vehicle.get("trainings"):
        tags.append("Training required")
    if vehicle.get("is_trailer"):
        tags.append("Trailer")
    if not tags:
        tags.append(FALLBACK_TAG)
    # Dedup, cap, and guarantee at least the fallback.
    out: list[str] = []
    for tag in tags:
        if tag not in out:
            out.append(tag)
    return out[:MAX_TAGS_PER_POST] or [FALLBACK_TAG]


# The full tag vocabulary with a display emoji each, in forum-display order.
# This is the single source of truth: infer_tags only ever returns names from
# here, and the forum's ensure_tags creates exactly these. 8 tags — well under
# Discord's 20-per-forum limit.
FORUM_TAG_EMOJI = {
    "Fire": "🚒",
    "EMS": "🚑",
    "Police": "🚓",
    "Water Rescue": "🌊",
    "Water/Pump": "🚰",
    "Training required": "🎓",
    "Trailer": "🚚",
    FALLBACK_TAG: "📦",
}

# No tag renames have happened yet; the forum's ensure_tags accepts the map.
RENAMED_TAGS: dict[str, str] = {}


def all_tag_names() -> list[str]:
    """Every tag the forum needs (for ensure_tags)."""
    return list(FORUM_TAG_EMOJI)
