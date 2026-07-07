"""The parameter set for a custom "Own mission" and how to read one from
a board post.

A custom mission is a large scale alliance mission whose parameters the
member supplies in full: which mission type, the position footprint
(poi type / size / shape / amount) and where. Coins are always pinned to
0 — the scheduler starts free missions only, never paid ones.

Two front-ends produce a :class:`MissionSpec`: the Discord panel/slash
command (structured input) and a structured board post (parsed here).
The board parser is deliberately strict — it only fires on an explicit
``own mission:`` / ``large scale mission:`` marker so it never collides
with the location-only *event* requests that share thread 15293.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...geo.maps_links import find_maps_links

# Reasonable guards so a typo can't request a 9999-wide mission field.
MAX_SIZE = 20
MAX_AMOUNT = 50
VALID_SHAPES = ("circle", "polygon", "rectangle")

# A post is a custom-mission request only when it opens with one of these.
# Horizontal-whitespace classes ([^\S\n]) keep each value on its own line so
# a blank trigger line can't swallow the next parameter line as the location.
_TRIGGER_RE = re.compile(
    r"(?:^|\n)[^\S\n]*(?:!mission|own\s+mission|custom\s+mission|"
    r"large\s+scale\s+(?:alliance\s+)?mission)[^\S\n]*[:\-][^\S\n]*(.*)",
    re.IGNORECASE,
)

# key: value lines for the individual parameters.
_KEY_RE = {
    "mission_type_id": re.compile(r"(?:^|\n)\s*(?:type|mission_?type|type_?id)\s*[:=]\s*(\d+)", re.IGNORECASE),
    "poi_type": re.compile(r"(?:^|\n)\s*poi(?:_?type)?\s*[:=]\s*(\d+)", re.IGNORECASE),
    "size": re.compile(r"(?:^|\n)\s*size\s*[:=]\s*(\d+)", re.IGNORECASE),
    "amount": re.compile(r"(?:^|\n)\s*(?:amount|count|qty)\s*[:=]\s*(\d+)", re.IGNORECASE),
    "shape": re.compile(r"(?:^|\n)\s*shape\s*[:=]\s*([A-Za-z]+)", re.IGNORECASE),
    "location": re.compile(
        r"(?:^|\n)[^\S\n]*(?:location|where|address|loc)[^\S\n]*[:=][^\S\n]*(.+)",
        re.IGNORECASE,
    ),
}


class MissionSpecError(ValueError):
    """A supplied mission parameter is missing or out of range."""


def is_mission_post(content: str) -> bool:
    """True when a board post opens with a custom-mission trigger.

    Used by the *events* poller to yield ownership of these posts: a custom
    mission and a location-only event both live on the same board thread, so
    without this a mission post (which usually carries a maps link) would be
    picked up by BOTH consumers and started twice."""
    return _TRIGGER_RE.search(content or "") is not None


@dataclass
class MissionSpec:
    location_text: str
    mission_type_id: int | None = None
    poi_type: int = 0
    size: int = 1
    shape: str = "circle"
    amount: int = 1

    def validate(self) -> "MissionSpec":
        """Clamp/validate member input; raise on anything unusable."""
        if not (self.location_text or "").strip():
            raise MissionSpecError("a location is required")
        self.location_text = self.location_text.strip()[:200]
        if self.mission_type_id is not None and self.mission_type_id < 0:
            raise MissionSpecError("mission type id must be positive")
        if not (1 <= self.size <= MAX_SIZE):
            raise MissionSpecError(f"size must be between 1 and {MAX_SIZE}")
        if not (1 <= self.amount <= MAX_AMOUNT):
            raise MissionSpecError(f"amount must be between 1 and {MAX_AMOUNT}")
        if self.poi_type < 0:
            raise MissionSpecError("poi type must be positive")
        self.shape = (self.shape or "circle").lower()
        if self.shape not in VALID_SHAPES:
            raise MissionSpecError(f"shape must be one of {', '.join(VALID_SHAPES)}")
        return self


def _int(text: str, default: int) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def parse_mission_spec(content: str) -> MissionSpec | None:
    """Parse a structured board post into a :class:`MissionSpec`.

    Side-effect free. Returns ``None`` when the post isn't a custom-mission
    request; raises :class:`MissionSpecError` when it clearly is one but
    the parameters are invalid.
    """
    trigger = _TRIGGER_RE.search(content or "")
    if not trigger:
        return None

    # Location: an explicit "location:" line, else the trigger line's tail,
    # else the first maps link in the post.
    loc_match = _KEY_RE["location"].search(content)
    location = (loc_match.group(1) if loc_match else trigger.group(1) or "").strip()
    if not location:
        links = find_maps_links(content)
        location = links[0] if links else ""
    if not location:
        raise MissionSpecError("no location given")

    def _grab(key: str, default: int) -> int:
        m = _KEY_RE[key].search(content)
        return _int(m.group(1), default) if m else default

    type_match = _KEY_RE["mission_type_id"].search(content)
    shape_match = _KEY_RE["shape"].search(content)

    spec = MissionSpec(
        location_text=location,
        mission_type_id=int(type_match.group(1)) if type_match else None,
        poi_type=_grab("poi_type", 0),
        size=_grab("size", 1),
        shape=shape_match.group(1) if shape_match else "circle",
        amount=_grab("amount", 1),
    )
    return spec.validate()
