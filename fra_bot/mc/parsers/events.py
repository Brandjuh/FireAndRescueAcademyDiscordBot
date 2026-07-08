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
) -> list[tuple[str, str]]:
    """Merge form fields with our coordinates + safe defaults."""
    merged = dict(form.fields)
    defaults = _LARGE_DEFAULTS if kind == "large" else _EVENT_DEFAULTS
    for key, value in defaults.items():
        merged.setdefault(key, value)
    merged["mission_position[latitude]"] = f"{latitude:.6f}"
    merged["mission_position[longitude]"] = f"{longitude:.6f}"
    merged["mission_position[address]"] = address
    merged["mission_position[coins]"] = "0"

    if kind == "event":
        # Event type radio is mirrored into mission_type_id.
        radio = merged.get("event_radio_group") or merged.get(
            "mission_position[mission_type_id]"
        )
        if radio:
            merged["event_radio_group"] = radio
            merged["mission_position[mission_type_id]"] = radio

    return [(key, value) for key, value in merged.items() if value != "" or key.endswith("]")]


def build_custom_mission_payload(
    form: EventForm,
    *,
    latitude: float,
    longitude: float,
    address: str,
    mission_type_id: int | None,
    poi_type: int,
    size: int,
    shape: str,
    amount: int,
) -> list[tuple[str, str]]:
    """Large scale alliance mission body with member-supplied parameters.

    Starts from the large-mission form defaults, then overrides the
    position footprint with the caller's values. ``coins`` is always 0 —
    the scheduler only ever starts free missions.
    """
    merged = dict(form.fields)
    for key, value in _LARGE_DEFAULTS.items():
        merged.setdefault(key, value)
    merged["mission_position[latitude]"] = f"{latitude:.6f}"
    merged["mission_position[longitude]"] = f"{longitude:.6f}"
    merged["mission_position[address]"] = address
    merged["mission_position[poi_type]"] = str(poi_type)
    merged["mission_position[size]"] = str(size)
    merged["mission_position[shape]"] = shape
    merged["mission_position[amount]"] = str(amount)
    merged["mission_position[coins]"] = "0"
    if mission_type_id is not None:
        merged["mission_position[mission_type_id]"] = str(mission_type_id)

    return [(key, value) for key, value in merged.items() if value != "" or key.endswith("]")]


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
