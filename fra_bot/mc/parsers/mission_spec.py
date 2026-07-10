"""The unified mission/event request and how to read one from a board post.

A single :class:`MissionSpec` describes everything a member can ask for:

* **location** — free text ("Grand Rapids", "Wal Amsterdam") or a maps link,
* **kind** — an alliance ``event`` or a ``large`` scale alliance mission,
* **source** — a ``preset`` mission, a member-supplied ``custom`` Own mission,
  or one picked from MissionChief's ``saved`` missions dropdown,
* **schedule** — ``once`` (a one-off queue item) or ``recurring`` (added to
  the admin rotation list so the bot keeps cycling it).

Coins are never part of a request: the scheduler starts free missions only.

Two front-ends build a spec: the Discord slash command / panel (structured
input, see :mod:`fra_bot.cogs.missions`) and a structured board post (parsed
here). The board parser is deliberately strict — it only fires on an explicit
``own mission:`` / ``large scale mission:`` marker so it never collides with
the bare-location *event* requests that share the board thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...geo.maps_links import find_maps_links
from .missions_custom import CustomMission, CustomMissionError, parse_custom_values

KINDS = ("large", "event")
SOURCES = ("preset", "custom", "saved")
# The four selectable presets on /missionAllianceNew (Own mission is -1 and is
# handled via ``source='custom'``, not as a preset).
PRESET_TYPE_IDS = {41: "Major fire", 61: "Unannounced demonstration",
                   62: "Pile-up", 112: "Bomb Explosion"}

# A post is a structured mission request only when it opens with one of these.
# Horizontal-whitespace classes ([^\S\n]) keep each value on its own line so a
# blank trigger line can't swallow the next parameter line as the location.
_TRIGGER_RE = re.compile(
    r"(?:^|\n)[^\S\n]*(?:!mission|own\s+mission|custom\s+mission|"
    r"large\s+scale\s+(?:alliance\s+)?mission)[^\S\n]*[:\-][^\S\n]*(.*)",
    re.IGNORECASE,
)

_KEY_RE = {
    "kind": re.compile(r"(?:^|\n)\s*kind\s*[:=]\s*([A-Za-z]+)", re.IGNORECASE),
    "preset": re.compile(r"(?:^|\n)\s*(?:type|mission_?type|type_?id|preset)\s*[:=]\s*(\d+)", re.IGNORECASE),
    "saved": re.compile(r"(?:^|\n)\s*(?:saved|saved_?mission|preset_?name)\s*[:=]\s*(.+)", re.IGNORECASE),
    "name": re.compile(r"(?:^|\n)\s*(?:name|caption|title)\s*[:=]\s*(.+)", re.IGNORECASE),
    "custom": re.compile(r"(?:^|\n)\s*(?:custom|units|params|requirements)\s*[:=]\s*(.+)", re.IGNORECASE),
    "event": re.compile(r"(?:^|\n)\s*(?:event|event_?type)\s*[:=]\s*(.+)", re.IGNORECASE),
    "area": re.compile(r"(?:^|\n)\s*area\s*[:=]\s*([A-Za-z]+)", re.IGNORECASE),
    "shape": re.compile(r"(?:^|\n)\s*shape\s*[:=]\s*([A-Za-z]+)", re.IGNORECASE),
    "call_volume": re.compile(r"(?:^|\n)\s*(?:call\s*volume|volume|call)\s*[:=]\s*(\d+)", re.IGNORECASE),
    "location": re.compile(
        r"(?:^|\n)[^\S\n]*(?:location|where|address|loc)[^\S\n]*[:=][^\S\n]*(.+)",
        re.IGNORECASE,
    ),
}
_RECURRING_RE = re.compile(
    r"(?:^|\n)\s*(?:recurring|schedule[d]?|rotation|rotate|repeat)\b", re.IGNORECASE
)


class MissionSpecError(ValueError):
    """A supplied request parameter is missing or out of range."""


def is_mission_post(content: str) -> bool:
    """True when a board post opens with a structured-mission trigger.

    Used by the *events* poller to yield ownership of these posts: a custom
    mission and a bare-location event both live on the same board thread, so
    without this a mission post (which usually carries a maps link) would be
    picked up by BOTH consumers and started twice."""
    if _TRIGGER_RE.search(content or "") is not None:
        return True
    from .mission_template import looks_like_template

    return looks_like_template(content or "")


@dataclass
class MissionSpec:
    location_text: str
    kind: str = "large"
    source: str = "preset"
    preset_type_id: int | None = None
    custom: CustomMission | None = None
    saved_name: str | None = None
    recurring: bool = False
    # Alliance-event knobs (kind == "event"). event_type_id None + random True
    # means "pick a standard event at start time".
    event_type_id: int | None = None
    event_random: bool = False
    area: str = "medium"
    shape: str = "rectangle"
    call_volume: str = "45"

    def validate(self) -> "MissionSpec":
        """Clamp/validate the request; raise on anything unusable."""
        self.location_text = (self.location_text or "").strip()[:200]
        if not self.location_text:
            raise MissionSpecError("a location is required")

        self.kind = (self.kind or "large").lower()
        if self.kind not in KINDS:
            raise MissionSpecError(f"kind must be one of {', '.join(KINDS)}")

        self.source = (self.source or "preset").lower()
        if self.source not in SOURCES:
            raise MissionSpecError(f"source must be one of {', '.join(SOURCES)}")

        # Own-mission data (custom / saved) exists only on the large scale
        # form; events are always a preset free start.
        if self.source in ("custom", "saved") and self.kind != "large":
            raise MissionSpecError(
                "custom and saved missions are large scale only — set kind to 'large'"
            )

        if self.source == "custom":
            if self.custom is None:
                raise MissionSpecError("custom mission data is required")
            try:
                self.custom = self.custom.clamped()
            except CustomMissionError as exc:
                raise MissionSpecError(str(exc)) from exc
        else:
            self.custom = None

        if self.source == "saved":
            self.saved_name = (self.saved_name or "").strip()
            if not self.saved_name:
                raise MissionSpecError("a saved-mission name is required")
        else:
            self.saved_name = None

        if self.source == "preset" and self.preset_type_id is not None:
            if self.kind != "large" or self.preset_type_id not in PRESET_TYPE_IDS:
                # Unknown/irrelevant preset id: drop it and use the form default.
                self.preset_type_id = None
        else:
            self.preset_type_id = None

        self._validate_event_fields()
        return self

    def _validate_event_fields(self) -> None:
        from .events import EVENT_AREAS, EVENT_CALL_VOLUMES, EVENT_SHAPES, EVENT_TYPES

        if self.kind != "event":
            # Event knobs are meaningless for large missions; reset to defaults.
            self.event_type_id, self.event_random = None, False
            self.area, self.shape, self.call_volume = "medium", "rectangle", "45"
            return
        self.area = (self.area or "medium").lower()
        if self.area not in EVENT_AREAS:
            raise MissionSpecError(f"area must be one of {', '.join(EVENT_AREAS)}")
        self.shape = (self.shape or "rectangle").lower()
        if self.shape not in EVENT_SHAPES:
            raise MissionSpecError(f"shape must be one of {', '.join(EVENT_SHAPES)}")
        self.call_volume = str(self.call_volume or "45").strip()
        if self.call_volume not in EVENT_CALL_VOLUMES:
            raise MissionSpecError(
                f"call volume must be one of {', '.join(EVENT_CALL_VOLUMES)} (seconds)"
            )
        if self.event_type_id is not None and self.event_type_id not in EVENT_TYPES:
            raise MissionSpecError("event type id must be 0-7")
        # No concrete type + not random → default to random.
        if self.event_type_id is None:
            self.event_random = True

    # Convenience for the queue/rotation storage layer.
    def describe(self) -> str:
        if self.kind == "event":
            from .events import EVENT_TYPES

            if self.event_random or self.event_type_id is None:
                etype = "random"
            else:
                etype = EVENT_TYPES.get(self.event_type_id, self.event_type_id)
            body = (
                f"{etype} · {self.area}/{self.shape}/{self.call_volume}s"
            )
        elif self.source == "custom" and self.custom is not None:
            body = f"custom '{self.custom.caption}' ({self.custom.summary()})"
        elif self.source == "saved":
            body = f"saved '{self.saved_name}'"
        elif self.preset_type_id is not None:
            body = f"preset {PRESET_TYPE_IDS.get(self.preset_type_id, self.preset_type_id)}"
        else:
            body = "preset"
        sched = "recurring" if self.recurring else "one-time"
        return f"{self.kind} · {body} · {sched}"


def parse_mission_spec(content: str) -> MissionSpec | None:
    """Parse a structured board post into a :class:`MissionSpec`.

    Side-effect free. Returns ``None`` when the post isn't a structured
    mission request; raises :class:`MissionSpecError` when it clearly is one
    but the parameters are invalid.
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

    kind_match = _KEY_RE["kind"].search(content)
    kind = (kind_match.group(1).lower() if kind_match else "large")

    saved_match = _KEY_RE["saved"].search(content)
    custom_match = _KEY_RE["custom"].search(content)
    name_match = _KEY_RE["name"].search(content)
    preset_match = _KEY_RE["preset"].search(content)
    recurring = _RECURRING_RE.search(content) is not None

    source = "preset"
    custom: CustomMission | None = None
    saved_name: str | None = None
    if saved_match:
        source = "saved"
        saved_name = saved_match.group(1).strip()
    elif custom_match:
        source = "custom"
        try:
            values = parse_custom_values(custom_match.group(1))
        except CustomMissionError as exc:
            raise MissionSpecError(str(exc)) from exc
        caption = (name_match.group(1).strip() if name_match else "") or location
        custom = CustomMission(caption=caption, values=values)

    # Event knobs (only used when kind == "event").
    event_type_id: int | None = None
    event_random = False
    area = shape = call_volume = None
    if kind == "event":
        from .events import resolve_event_type

        event_match = _KEY_RE["event"].search(content)
        try:
            event_type_id, event_random = resolve_event_type(
                event_match.group(1) if event_match else ""
            )
        except ValueError as exc:
            raise MissionSpecError(str(exc)) from exc
        area_match = _KEY_RE["area"].search(content)
        shape_match = _KEY_RE["shape"].search(content)
        volume_match = _KEY_RE["call_volume"].search(content)
        area = area_match.group(1) if area_match else "medium"
        shape = shape_match.group(1) if shape_match else "rectangle"
        call_volume = volume_match.group(1) if volume_match else "45"

    spec = MissionSpec(
        location_text=location,
        kind=kind,
        source=source,
        preset_type_id=int(preset_match.group(1)) if preset_match else None,
        custom=custom,
        saved_name=saved_name,
        recurring=recurring,
        event_type_id=event_type_id,
        event_random=event_random,
        area=area or "medium",
        shape=shape or "rectangle",
        call_volume=call_volume or "45",
    )
    return spec.validate()


# ---------------------------------------------------------------------------
# Dedicated-board intake: a member just posts a location, no trigger word.
# ---------------------------------------------------------------------------

# On an EVENT request board, unspecified footprint uses these defaults.
EVENT_BOARD_DEFAULTS = {"area": "large", "shape": "circle", "call_volume": "30"}

# A line is "structured" (a key: value refinement) if it starts with one of
# these keys — such a line is never treated as the location.
_STRUCTURED_LINE_RE = re.compile(
    r"^\s*(kind|event|event_?type|preset|type|type_?id|saved|saved_?mission|"
    r"custom|units|params|requirements|name|caption|title|area|shape|"
    r"call\s*volume|volume|call|schedule[d]?|location|where|address|loc)\s*[:=]",
    re.IGNORECASE,
)
# A leading label to strip off the location line itself.
_LOCATION_LABEL_RE = re.compile(
    r"^\s*(event|events|mission|missions|alliance\s+event|large\s+scale\s+"
    r"(?:alliance\s+)?mission|own\s+mission|location|request|loc|where)\s*[:\-]\s*",
    re.IGNORECASE,
)


def _plain_location_line(content: str) -> str:
    """First line that isn't a key:value refinement, minus any leading label."""
    for line in (content or "").splitlines():
        line = line.strip()
        if not line or _STRUCTURED_LINE_RE.match(line):
            continue
        return _LOCATION_LABEL_RE.sub("", line).strip()
    return ""


def parse_board_request(
    content: str, *, default_kind: str = "large"
) -> MissionSpec | None:
    """Parse a *dedicated request board* post into a :class:`MissionSpec`.

    Unlike :func:`parse_mission_spec`, no trigger word is required — a bare
    location line ("New York City") is the request. ``default_kind`` sets the
    kind for that board (event vs large); optional ``key: value`` lines refine
    it (kind / event / preset / saved / custom / name / area / shape / call /
    schedule). On an event board, unspecified footprint uses the alliance's
    defaults (large / circle / 30s).

    Returns ``None`` for an empty/bot post; raises :class:`MissionSpecError`
    when the post clearly asks for something but a field is invalid (the
    caller replies asking the member to clarify).
    """
    raw = (content or "").strip()
    # Skip our own posts: the reply marker "[FRA]" and guide markers "[FRA-…]".
    if not raw or raw.startswith("[FRA"):
        return None

    # The copy-paste Own-mission template from the guide gets first go: a
    # filled-in field list is a complete custom request on its own (a
    # deleted line means 0), regardless of which board it lands on.
    from .mission_template import looks_like_template, parse_template

    if looks_like_template(raw):
        try:
            location, caption, values = parse_template(raw)
        except CustomMissionError as exc:
            raise MissionSpecError(str(exc)) from exc
        if not location:
            raise MissionSpecError(
                "the template is missing its Location — put the place name "
                "(or a maps link) on the line under 'Location:'"
            )
        return MissionSpec(
            location_text=location,
            kind="large",  # Own missions are large scale by definition
            source="custom",
            custom=CustomMission(caption=(caption or location), values=values),
            recurring=_RECURRING_RE.search(content) is not None,
        ).validate()

    kind_match = _KEY_RE["kind"].search(content)
    kind = (kind_match.group(1).lower() if kind_match else default_kind)

    # Location: explicit "location:" line, else a maps link, else the first
    # plain (non-refinement) line.
    loc_match = _KEY_RE["location"].search(content)
    if loc_match:
        location = loc_match.group(1).strip()
    else:
        links = find_maps_links(content)
        location = links[0] if links else _plain_location_line(content)
    if not location:
        return None

    saved_match = _KEY_RE["saved"].search(content)
    custom_match = _KEY_RE["custom"].search(content)
    name_match = _KEY_RE["name"].search(content)
    preset_match = _KEY_RE["preset"].search(content)
    recurring = _RECURRING_RE.search(content) is not None

    source = "preset"
    custom: CustomMission | None = None
    saved_name: str | None = None
    if saved_match:
        source = "saved"
        saved_name = saved_match.group(1).strip()
    elif custom_match:
        source = "custom"
        try:
            values = parse_custom_values(custom_match.group(1))
        except CustomMissionError as exc:
            raise MissionSpecError(str(exc)) from exc
        caption = (name_match.group(1).strip() if name_match else "") or location
        custom = CustomMission(caption=caption, values=values)

    event_type_id: int | None = None
    event_random = False
    area = shape = call_volume = None
    if kind == "event":
        from .events import resolve_event_type

        event_match = _KEY_RE["event"].search(content)
        try:
            event_type_id, event_random = resolve_event_type(
                event_match.group(1) if event_match else ""
            )
        except ValueError as exc:
            raise MissionSpecError(str(exc)) from exc
        area_match = _KEY_RE["area"].search(content)
        shape_match = _KEY_RE["shape"].search(content)
        volume_match = _KEY_RE["call_volume"].search(content)
        area = area_match.group(1) if area_match else EVENT_BOARD_DEFAULTS["area"]
        shape = shape_match.group(1) if shape_match else EVENT_BOARD_DEFAULTS["shape"]
        call_volume = (
            volume_match.group(1) if volume_match else EVENT_BOARD_DEFAULTS["call_volume"]
        )

    spec = MissionSpec(
        location_text=location,
        kind=kind,
        source=source,
        preset_type_id=int(preset_match.group(1)) if preset_match else None,
        custom=custom,
        saved_name=saved_name,
        recurring=recurring,
        event_type_id=event_type_id,
        event_random=event_random,
        area=area or "large",
        shape=shape or "circle",
        call_volume=call_volume or "30",
    )
    return spec.validate()
