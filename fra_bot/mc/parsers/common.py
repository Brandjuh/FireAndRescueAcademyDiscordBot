"""Shared parsing helpers.

MissionChief timestamp quirks handled here:

* Recent rows: ``"06 Jul 14:23"`` — no year, no seconds, in the game's
  local timezone (America/New_York for missionchief.com).
* Older rows: ``"July 06, 2026 14:23"`` — absolute, with year.

Yearless timestamps are only normalized when they fall in a sane recent
window; ambiguous rows keep ``None`` so we never store a wrong instant.
The raw string is always stored alongside.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from zoneinfo import ZoneInfo

MC_TIMEZONE = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

_NUMBER_RE = re.compile(r"([\d][\d.,]*)")


def parse_int(text: str | None) -> int | None:
    """Extract the first integer from text like '1,234,567 Credits'."""
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    if not digits:
        return None
    value = int(digits)
    # Guard against markup glitches producing absurd numbers.
    if value > 10**15:
        return None
    return value


def parse_percent(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def signature_of(*parts: object) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def extract_user_id(href: str | None) -> int | None:
    """MC user id from /users/<id> or /profile/<id> links."""
    if not href:
        return None
    match = re.search(r"/(?:users|profile)/(\d+)", href)
    return int(match.group(1)) if match else None


def normalize_mc_timestamp(
    raw: str,
    *,
    reference: dt.datetime | None = None,
    max_age_days: int = 8,
) -> str | None:
    """Convert an MC-displayed timestamp to a UTC ISO string, or None.

    ``reference`` is "now" (UTC, aware); injectable for tests.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    reference = reference or dt.datetime.now(UTC)

    # Absolute format: "July 06, 2026 14:23"
    try:
        parsed = dt.datetime.strptime(raw, "%B %d, %Y %H:%M")
        return parsed.replace(tzinfo=MC_TIMEZONE).astimezone(UTC).isoformat(
            timespec="seconds"
        )
    except ValueError:
        pass

    # Yearless format: "06 Jul 14:23" — infer year from the reference.
    try:
        parsed = dt.datetime.strptime(raw, "%d %b %H:%M")
    except ValueError:
        return None

    ref_local = reference.astimezone(MC_TIMEZONE)
    candidate = parsed.replace(year=ref_local.year, tzinfo=MC_TIMEZONE)
    if candidate > ref_local + dt.timedelta(minutes=5):
        candidate = candidate.replace(year=candidate.year - 1)
    age = ref_local - candidate
    if age < dt.timedelta(minutes=-5) or age > dt.timedelta(days=max_age_days):
        return None  # ambiguous — keep only the raw string
    return candidate.astimezone(UTC).isoformat(timespec="seconds")
