"""Classroom occupancy model built from the alliance log (SHADOW mode).

Instead of loading every academy page, predict the free classrooms per
academy from what the alliance log already tells us:

* **rooms** = 1 + every finished "Additional classroom" / "More classrooms"
  expansion on that building (a new academy starts with one classroom);
* **busy**  = every "Created a course" on that building that has not ended
  yet. A class ends at its start + the course duration in days (measured
  against 178 logged completions: the log stamps the start when signup
  closes), or earlier when a "Course completed" line shows up (a class
  finished early with coins).

Everyone's classes are in the log (most are opened in-game by hand, not by
the bot), so this sees all of them, with the log-sync delay (up to ~15 min).

For now the model only RUNS NEXT TO the availability walk: when a walk
completes, every academy the walk read is compared with the model's
prediction for the moment it was read, and the result is logged and kept
in state. Nothing decides on the model yet.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..db.database import Database
from ..db.repos import StateRepo

log = logging.getLogger(__name__)

#: Rolling comparison results (see :func:`compare_with_walk`).
CHECK_STATE_KEY = "classroom_model_check"
#: Course length used when a course name is not in the catalog. Long on
#: purpose: an unknown class then holds its room (the model errs towards
#: "full", never towards a free room that isn't there).
UNKNOWN_COURSE_DAYS = 7
_PAREN_RE = re.compile(r"\(([^)]+)\)\s*$")
_CLASSROOM_RE = re.compile(r"classroom", re.IGNORECASE)
_MAX_MISMATCH_SAMPLES = 15


def _course_name(description: str | None) -> str | None:
    text = " ".join((description or "").split())
    match = _PAREN_RE.search(text)
    return match.group(1).strip() if match else None


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


@dataclass
class _Class:
    course: str
    start: dt.datetime
    end: dt.datetime


@dataclass
class AcademyModel:
    building_id: int
    rooms: int = 1
    classes: list[_Class] = field(default_factory=list)

    def busy_at(self, when: dt.datetime) -> int:
        return sum(1 for c in self.classes if c.start <= when < c.end)

    def free_at(self, when: dt.datetime) -> int:
        return max(0, self.rooms - self.busy_at(when))

    def next_free_at(self, when: dt.datetime) -> dt.datetime | None:
        """When the next room frees up, if none is free at *when*."""
        if self.free_at(when) > 0:
            return when
        ends = sorted(c.end for c in self.classes if c.start <= when < c.end)
        return ends[0] if ends else None


def _days_lookup(catalog: dict) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for discipline, courses in (catalog or {}).items():
        for name, days in (courses or {}).items():
            if isinstance(days, int) and days > 0:
                out[(discipline, name.casefold())] = days
    return out


async def build_models(
    db: Database,
    academies: dict[int, str],
    catalog: dict,
    *,
    since: dt.datetime,
) -> dict[int, AcademyModel]:
    """Models for *academies* (building id -> discipline).

    *since* bounds how far back classes are read (no course runs longer than
    a week). Expansions are always read in full: the room count is the sum
    of every classroom expansion ever finished."""
    if not academies:
        return {}
    days = _days_lookup(catalog)
    models = {bid: AcademyModel(bid) for bid in academies}
    ids = list(academies)
    marks = ",".join("?" for _ in ids)

    async with db.conn.execute(
        f"SELECT affected_mc_id, description FROM alliance_logs "
        f"WHERE action_key = 'expansion_finished' AND affected_mc_id IN ({marks})",
        ids,
    ) as cur:
        for row in await cur.fetchall():
            if _CLASSROOM_RE.search(row["description"] or ""):
                models[int(row["affected_mc_id"])].rooms += 1

    since_iso = since.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    async with db.conn.execute(
        f"SELECT action_key, affected_mc_id, description, event_at FROM alliance_logs "
        f"WHERE action_key IN ('created_course', 'course_completed') "
        f"AND affected_mc_id IN ({marks}) AND event_at >= ? "
        f"ORDER BY event_at, id",
        [*ids, since_iso],
    ) as cur:
        rows = await cur.fetchall()

    open_by_course: dict[tuple[int, str], list[_Class]] = defaultdict(list)
    for row in rows:
        building_id = int(row["affected_mc_id"])
        when = _parse_ts(row["event_at"])
        name = _course_name(row["description"])
        if when is None or not name:
            continue
        key = (building_id, name.casefold())
        if row["action_key"] == "created_course":
            length = days.get((academies[building_id], name.casefold()), UNKNOWN_COURSE_DAYS)
            cls = _Class(name, when, when + dt.timedelta(days=length))
            models[building_id].classes.append(cls)
            open_by_course[key].append(cls)
        else:
            # A logged completion ends the OLDEST still-running class of that
            # course in that building (early, when it was bought with coins).
            running = [c for c in open_by_course[key] if c.start <= when < c.end]
            if running:
                running[0].end = when
                open_by_course[key].remove(running[0])
    return models


async def compare_with_walk(
    db: Database,
    state: StateRepo,
    catalog: dict,
    readings: list[tuple[int, str, int, float]],
) -> dict | None:
    """Compare a finished availability walk with the model.

    *readings* holds one ``(building_id, discipline, free_rooms, read_at)``
    per academy the walk could read (``read_at`` is a UTC epoch). Logs a
    summary, keeps it in state, and returns it. Never raises: the model is
    a side experiment and must not break the walk."""
    try:
        return await _compare(db, state, catalog, readings)
    except Exception:  # noqa: BLE001 — shadow mode must never break the walk
        log.exception("classroom model: comparison failed")
        return None


async def _compare(db, state, catalog, readings) -> dict | None:
    if not readings:
        return None
    academies = {int(b): d for b, d, _, _ in readings}
    earliest = min(t for *_, t in readings)
    since = dt.datetime.fromtimestamp(earliest, dt.timezone.utc) - dt.timedelta(
        days=UNKNOWN_COURSE_DAYS + 1
    )
    models = await build_models(db, academies, catalog, since=since)

    exact = too_high = too_low = 0
    by_discipline: dict[str, dict[str, int]] = defaultdict(
        lambda: {"walk": 0, "model": 0}
    )
    samples: list[str] = []
    for building_id, discipline, actual, read_at in readings:
        when = dt.datetime.fromtimestamp(read_at, dt.timezone.utc)
        model = models[int(building_id)]
        predicted = model.free_at(when)
        by_discipline[discipline]["walk"] += actual
        by_discipline[discipline]["model"] += predicted
        if predicted == actual:
            exact += 1
            continue
        if predicted > actual:
            too_high += 1
        else:
            too_low += 1
        if len(samples) < _MAX_MISMATCH_SAMPLES:
            samples.append(
                f"{building_id} ({discipline}): walk {actual} free, model "
                f"{predicted} (rooms {model.rooms}, busy {model.busy_at(when)})"
            )

    total = len(readings)
    summary = {
        "at": int(dt.datetime.now(dt.timezone.utc).timestamp()),
        "academies": total,
        "exact": exact,
        "model_too_high": too_high,
        "model_too_low": too_low,
        "by_discipline": dict(by_discipline),
        "mismatches": samples,
    }
    history = await _load_history(state)
    history.append(summary)
    await state.set(CHECK_STATE_KEY, json.dumps(history[-30:]))
    log.info(
        "classroom model: %d/%d academies exact, %d said free while full, "
        "%d said full while free — free rooms walk vs model: %s",
        exact, total, too_high, too_low,
        ", ".join(f"{d} {v['walk']}/{v['model']}" for d, v in sorted(by_discipline.items())),
    )
    for line in samples:
        log.info("classroom model mismatch: %s", line)
    return summary


async def _load_history(state: StateRepo) -> list[dict]:
    raw = await state.get(CHECK_STATE_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []
