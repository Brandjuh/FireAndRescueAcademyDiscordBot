"""Per-action-key alliance-log routing.

Each alliance-log line is classified into an ``action_key`` (see
:mod:`fra_bot.mc.parsers.logs`). This module lets an admin **duplicate**
chosen types into extra channels: the log still posts to the main
``alliance_logs`` channel, and a copy also goes to every channel that
subscribed to that type.

Storage is CHANNEL-keyed — ``{channel_id: [target, ...]}`` — so a channel
appears exactly once (a row can never be double-sent to it) and "stop
routing anything to this channel" is a single-key delete. A *target* is one
of:

* an exact ``action_key`` (e.g. ``building_constructed``),
* a GROUP alias (``building``, ``member``, ``admin``, ``moderation``,
  ``course``, ``mission``) that expands to a set of keys,
* ``all`` — every log type, including future/unknown ones,
* ``unknown`` — log lines the classifier didn't recognise (a catch-all for
  new MissionChief wording worth adding to the parser).

The map lives in the state table (like the scheduled-reports override) and
is read live by the publisher, so changes apply without a restart.
"""

from __future__ import annotations

import json

from ..cogs.display import ACTION_DISPLAY

STATE_KEY = "log_routes"

#: Group alias -> the action keys it expands to. Keys must all exist in
#: ACTION_DISPLAY (a startup check enforces this). ``kicked_from_alliance``
#: and ``application_denied`` intentionally sit in two groups — channel-keyed
#: storage dedups the delivery.
GROUPS: dict[str, frozenset[str]] = {
    "building": frozenset({
        "building_constructed", "building_destroyed",
        "extension_started", "expansion_finished",
    }),
    "member": frozenset({
        "added_to_alliance", "left_alliance", "kicked_from_alliance",
        "application_denied",
    }),
    "admin": frozenset({
        "set_admin", "removed_admin", "set_co_admin", "removed_co_admin",
        "set_transport_admin", "removed_transport_admin",
        "set_education_admin", "removed_education_admin",
        "set_finance_admin", "removed_finance_admin",
        "set_mod_action_admin", "removed_mod_action_admin",
        "set_as_staff", "removed_as_staff",
        "promoted_to_event_manager", "removed_event_manager",
    }),
    "moderation": frozenset({
        "chat_ban_set", "chat_ban_removed",
        "allowed_to_apply", "not_allowed_to_apply",
        "kicked_from_alliance", "application_denied",
    }),
    "course": frozenset({"created_course", "course_completed"}),
    "mission": frozenset({
        "large_mission_started", "alliance_event_started",
        "removed_custom_large_scale_mission",
    }),
}

#: The catch-all targets that don't map to a single key.
ALL = "all"
UNKNOWN = "unknown"


def known_action_keys() -> frozenset[str]:
    """Every action key the display layer knows, plus ``unknown``."""
    return frozenset(ACTION_DISPLAY) | {UNKNOWN}


def valid_targets() -> frozenset[str]:
    """Everything ``add`` accepts: keys, group aliases, and ``all``."""
    return known_action_keys() | set(GROUPS) | {ALL}


def group_drift() -> dict[str, set[str]]:
    """Group members that are NOT known action keys — a typo in a GROUP set
    silently drops that category, so a startup check surfaces it (mirrors the
    ACTION_PATTERNS-vs-ACTION_DISPLAY check in the notifications cog)."""
    keys = frozenset(ACTION_DISPLAY)
    return {
        name: (members - keys)
        for name, members in GROUPS.items()
        if members - keys
    }


def normalize_target(token: str) -> str | None:
    """Canonical form of a target token, or None if it isn't valid."""
    wanted = (token or "").strip().lower().replace("-", "_").lstrip("#")
    return wanted if wanted in valid_targets() else None


def target_matches(target: str, action_key: str) -> bool:
    if target == ALL:
        return True
    if target == action_key:
        return True
    group = GROUPS.get(target)
    return group is not None and action_key in group


def channels_for(
    routes: dict[int, list[str]], action_key: str, *, exclude: int | None = None
) -> list[int]:
    """Channel ids that subscribed to *action_key*. Channel-keyed storage
    dedups automatically; ``exclude`` drops the main log channel so a route
    aimed there can't double-post alongside the main feed."""
    out: list[int] = []
    for channel_id, targets in routes.items():
        if channel_id == exclude:
            continue
        if any(target_matches(t, action_key) for t in targets):
            out.append(channel_id)
    return out


# -- persistence -------------------------------------------------------------

async def load(state) -> dict[int, list[str]]:
    """The route map, or ``{}`` on absence / malformed value (never raises —
    a bad state write must not break every publish pass)."""
    raw = await state.get(STATE_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    routes: dict[int, list[str]] = {}
    for key, targets in data.items():
        try:
            channel_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(targets, list):
            clean = [str(t) for t in targets if str(t) in valid_targets()]
            if clean:
                routes[channel_id] = clean
    return routes


async def store(state, routes: dict[int, list[str]]) -> None:
    await state.set(STATE_KEY, json.dumps({str(k): v for k, v in routes.items()}))


async def add(state, channel_id: int, targets: list[str]) -> list[str]:
    """Subscribe *channel_id* to *targets* (validated, normalized, deduped).
    Returns the channel's full target list afterwards."""
    routes = await load(state)
    current = list(routes.get(channel_id, []))
    for token in targets:
        norm = normalize_target(token)
        if norm and norm not in current:
            current.append(norm)
    routes[channel_id] = current
    await store(state, routes)
    return current


async def remove(state, channel_id: int, target: str | None) -> bool:
    """Drop one target from a channel, or the whole channel when *target* is
    None. Returns True if something was removed."""
    routes = await load(state)
    if channel_id not in routes:
        return False
    if target is None:
        del routes[channel_id]
        await store(state, routes)
        return True
    norm = normalize_target(target)
    current = routes.get(channel_id, [])
    if norm not in current:
        return False
    current = [t for t in current if t != norm]
    if current:
        routes[channel_id] = current
    else:
        del routes[channel_id]
    await store(state, routes)
    return True


async def clear(state) -> bool:
    existed = await state.get(STATE_KEY) is not None
    await state.delete(STATE_KEY)
    return existed
