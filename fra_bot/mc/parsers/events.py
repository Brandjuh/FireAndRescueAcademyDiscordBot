"""Parser + payload builder for alliance mission/event creation forms.

MissionChief serves a Rails form at ``/missionAllianceNew`` (large scale
alliance mission) and ``/missionAllianceEventNew`` (alliance event). We
read the form, inject coordinates and safe defaults, and refuse anything
that would spend coins.

The page also shows a "Last free mission: <date>" line used to compute
when the next free start is allowed.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

MC_TIMEZONE = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

EVENT_KINDS = {
    "large": {
        "new_path": "/missionAllianceNew",
        "create_path": "/missionAllianceCreate",
        "free_interval_days": 1,
    },
    "event": {
        "new_path": "/missionAllianceEventNew",
        "create_path": "/missionAllianceEventCreate",
        "free_interval_days": 7,
    },
}

# Alliance event types, from the real /missionAllianceEventNew form. The
# submitted value is the data-event-id (0-7); id 8 (Soccer Game,
# data-event-tag="football") is a seasonal currency event we skip for
# "random" and don't offer as a standard pick.
EVENT_TYPES: dict[int, str] = {
    0: "Storm",
    1: "Civil Unrest",
    2: "Storm Surge",
    3: "Fall weather",
    4: "Winter weather",
    5: "Spring weather",
    6: "Summer weather",
    7: "Sports Event",
}
EVENT_NAME_TO_ID = {name.lower(): tid for tid, name in EVENT_TYPES.items()}

# Area -> mission_position[size]; Call volume -> mission_position[amount].
EVENT_AREAS = {"small": "0", "medium": "1", "large": "2"}
EVENT_CALL_VOLUMES = {"30": "0", "45": "1", "60": "2"}
EVENT_SHAPES = ("rectangle", "circle")


def resolve_event_type(text: str | None) -> tuple[int | None, bool]:
    """Map a user string to ``(event_type_id, is_random)``.

    Accepts a type name ("Storm"), an id (0-7), "random"/"any", or blank
    (treated as random). Raises ``ValueError`` on an unknown value."""
    raw = (text or "").strip().lower()
    if raw in ("", "random", "rnd", "any"):
        return None, True
    if raw.isdigit():
        tid = int(raw)
        if tid in EVENT_TYPES:
            return tid, False
        raise ValueError(f"event type id must be 0-7 (got {tid})")
    if raw in EVENT_NAME_TO_ID:
        return EVENT_NAME_TO_ID[raw], False
    raise ValueError(f"unknown event type: {text!r}")

_LAST_FREE_RE = re.compile(
    r"Last\s+free\s+mission[:\s]*"
    r"([A-Za-z]{3},?\s+\d{1,2}\s+[A-Za-z]{3}\.?\s+\d{4}"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"   # optional H:M[:S]
    r"(?:\s+[+-]\d{4})?)",                # optional explicit tz offset
    re.IGNORECASE,
)

# Defaults applied when the form doesn't dictate a value.
_LARGE_DEFAULTS = {
    "mission_position[poi_type]": "0",
    "mission_position[shape]": "circle",
    "mission_position[size]": "1",
    "mission_position[amount]": "1",
    "mission_position[coins]": "0",
}
_EVENT_DEFAULTS = {
    "mission_position[size]": "2",
    "mission_position[shape]": "circle",
    "mission_position[amount]": "0",
    "mission_position[coins]": "0",
}


@dataclass
class EventForm:
    action: str | None = None
    authenticity_token: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    submit_value: str | None = None
    last_free_at: str | None = None  # UTC ISO


def _parse_last_free(text: str, *, reference: dt.datetime | None = None) -> str | None:
    match = _LAST_FREE_RE.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "").replace(".", "").strip()
    # MissionChief prints the timestamp WITH an explicit timezone offset
    # (e.g. "Mon 01 Jul 2024 12:00:00 +0000"); parse that as-is — assuming a
    # timezone would be wrong by hours and skew the free-mission cooldown.
    for fmt in ("%a %d %b %Y %H:%M:%S %z", "%a %d %b %Y %H:%M %z"):
        try:
            aware = dt.datetime.strptime(raw, fmt)
            return aware.astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            pass
    # Fallback when no offset is shown: treat it as the NY game timezone.
    for fmt in ("%a %d %b %Y %H:%M:%S", "%a %d %b %Y %H:%M", "%a %d %b %Y"):
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=MC_TIMEZONE).astimezone(UTC).isoformat(
                timespec="seconds"
            )
        except ValueError:
            continue
    return None


def parse_event_form(html: str) -> EventForm:
    soup = BeautifulSoup(html, "lxml")
    form_el = None
    for candidate in soup.find_all("form"):
        if candidate.find("input", attrs={"name": "authenticity_token"}):
            form_el = candidate
            break
    result = EventForm()
    if form_el is None:
        result.last_free_at = _parse_last_free(soup.get_text(" ", strip=True))
        return result

    result.action = form_el.get("action")
    for inp in form_el.find_all("input"):
        input_type = (inp.get("type") or "text").lower()
        if input_type == "submit":
            # Submit buttons often have no name; capture the label anyway.
            result.submit_value = inp.get("value", result.submit_value)
            continue
        name = inp.get("name")
        if not name:
            continue
        if input_type in ("radio", "checkbox"):
            if inp.has_attr("checked"):
                result.fields[name] = inp.get("value", "")
            else:
                result.fields.setdefault(name, "")
        else:
            result.fields[name] = inp.get("value", "")
    for select in form_el.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        selected = select.find("option", selected=True) or select.find("option")
        result.fields[name] = selected.get("value", "") if selected else ""

    if result.submit_value is None:
        button = form_el.find("button", attrs={"type": "submit"}) or form_el.find("button")
        if button is not None:
            result.submit_value = button.get_text(strip=True) or button.get("value")

    result.authenticity_token = result.fields.get("authenticity_token")
    result.last_free_at = _parse_last_free(soup.get_text(" ", strip=True))
    return result


def build_event_payload(
    form: EventForm,
    *,
    kind: str,
    latitude: float,
    longitude: float,
    address: str,
    mission_type_id: int | None = None,
) -> list[tuple[str, str]]:
    """Merge form fields with our coordinates + safe defaults.

    ``mission_type_id`` optionally selects a specific preset (e.g. Major
    fire = 41); when omitted the form's already-checked default is kept.
    """
    merged = dict(form.fields)
    defaults = _LARGE_DEFAULTS if kind == "large" else _EVENT_DEFAULTS
    for key, value in defaults.items():
        merged.setdefault(key, value)
    merged["mission_position[latitude]"] = f"{latitude:.6f}"
    merged["mission_position[longitude]"] = f"{longitude:.6f}"
    merged["mission_position[address]"] = address
    merged["mission_position[coins]"] = "0"
    if mission_type_id is not None:
        merged["mission_position[mission_type_id]"] = str(mission_type_id)

    if kind == "event":
        # Event type radio is mirrored into mission_type_id.
        radio = merged.get("event_radio_group") or merged.get(
            "mission_position[mission_type_id]"
        )
        if radio:
            merged["event_radio_group"] = radio
            merged["mission_position[mission_type_id]"] = radio

    return [(key, value) for key, value in merged.items() if value != "" or key.endswith("]")]


def parse_large_mission_types(html: str) -> list[int]:
    """All selectable large-mission type ids on ``/missionAllianceNew`` —
    the ``mission_position[mission_type_id]`` radios. Sorted, deduped;
    empty when the form exposes no choice."""
    soup = BeautifulSoup(html, "lxml")
    out: set[int] = set()
    for inp in soup.select('input[name="mission_position[mission_type_id]"]'):
        value = inp.get("value")
        if value and str(value).isdigit():
            out.add(int(value))
    return sorted(out)


def parse_event_types(html: str) -> list[dict]:
    """Read the event-type radios from /missionAllianceEventNew.

    Each entry: ``{"id": int, "name": str, "tag": str}`` where ``tag`` is the
    ``data-event-tag`` (empty for the standard weather/civil events, e.g.
    ``"football"`` for the seasonal Soccer Game)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for inp in soup.select('input[name="event_radio_group"]'):
        raw_id = inp.get("data-event-id")
        if raw_id is None:
            continue
        try:
            event_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        label = inp.find_parent("label")
        name = label.get_text(" ", strip=True) if label else ""
        out.append({"id": event_id, "name": name, "tag": inp.get("data-event-tag") or ""})
    return out


def standard_event_ids(html: str | None = None) -> list[int]:
    """Ids eligible for a 'random' event: the standard (non-currency) ones.

    When given the live form, use the radios whose ``data-event-tag`` is
    empty (skips seasonal currency events like Soccer Game); otherwise fall
    back to the known catalog (0-7)."""
    if html:
        ids = [
            e["id"] for e in parse_event_types(html)
            if not e["tag"] and e["id"] in EVENT_TYPES
        ]
        if ids:
            return ids
    return list(EVENT_TYPES)


def build_alliance_event_payload(
    form: EventForm,
    *,
    latitude: float,
    longitude: float,
    address: str,
    event_type_id: int,
    event_tag: str = "",
    area: str = "medium",
    shape: str = "rectangle",
    call_volume: str = "45",
) -> list[tuple[str, str]]:
    """Body for /missionAllianceEventCreate.

    Selects the event type (``mission_position[mission_type_id]`` = the
    data-event-id, mirrored into ``event_radio_group``/``event_identifier``),
    sets Area (size), Shape and Call volume (amount), and pins ``coins`` to 0.

    The event form defaults coins to 1 ("Start Event (20 Coins)"); we always
    submit 0 — the scheduler only ever starts the free weekly event, gated by
    the cooldown, so it can never spend coins.
    """
    merged = dict(form.fields)
    merged["mission_position[mission_type_id]"] = str(event_type_id)
    merged["event_radio_group"] = "on"
    merged["event_identifier"] = event_tag or ""
    merged["mission_position[latitude]"] = f"{latitude:.6f}"
    merged["mission_position[longitude]"] = f"{longitude:.6f}"
    merged["mission_position[address]"] = address
    merged.setdefault("mission_position[poi_type]", "0")
    merged["mission_position[shape]"] = shape if shape in EVENT_SHAPES else "rectangle"
    merged["mission_position[size]"] = EVENT_AREAS.get(area, "1")
    merged["mission_position[amount]"] = EVENT_CALL_VOLUMES.get(str(call_volume), "1")
    merged.setdefault("mission_position[duration]", "2")
    merged["mission_position[coins]"] = "0"

    body = [(key, value) for key, value in merged.items() if value != "" or key.endswith("]")]
    # event_identifier is blank for standard events but the form always sends
    # it; keep it so the create endpoint sees the same shape as the browser.
    if not any(key == "event_identifier" for key, _ in body):
        body.append(("event_identifier", merged.get("event_identifier", "")))
    return body


def is_free_submit(form: EventForm) -> bool:
    """True when submitting won't spend coins."""
    if (form.fields.get("mission_position[coins]") or "0") not in ("", "0"):
        return False
    submit = (form.submit_value or "").lower()
    if "coin" in submit:
        return False
    return True


def next_free_at(kind: str, last_free_iso: str | None, *, grace_seconds: int = 75) -> str | None:
    if not last_free_iso:
        return None
    interval = EVENT_KINDS[kind]["free_interval_days"]
    last = dt.datetime.fromisoformat(last_free_iso)
    nxt = last + dt.timedelta(days=interval, seconds=grace_seconds)
    return nxt.isoformat(timespec="seconds")
