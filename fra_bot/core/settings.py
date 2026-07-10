"""Runtime-adjustable settings: everything config.yaml can set, via Discord.

A declarative registry lists every adjustable key with its type, bounds and
whether it applies LIVE or needs a restart. ``!fra set <key> <value>``
validates + applies the change in memory and persists an override in the
database (``scraper_state``), which is re-applied on every startup ON TOP of
config.yaml — the YAML file stays the documented default, the overrides are
the operator's runtime tweaks. ``!fra settings`` shows effective values and
marks overrides; ``!fra settings reset <key>`` returns to the file value.

Values parse naturally: booleans accept on/off, yes/no, aan/uit, ja/nee;
channels accept ``#mentions``; roles accept ``@mentions``; times are HH:MM.
Keys resolve by unique suffix (``dry_run`` → ``automation.dry_run``) with
did-you-mean suggestions on typos.

Secrets (token, MC login, geocoder key) live in ``.env`` and are deliberately
NOT settable here.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

OVERRIDE_PREFIX = "config_override:"

_TRUE_WORDS = {"on", "true", "yes", "ja", "aan", "1", "enable", "enabled"}
_FALSE_WORDS = {"off", "false", "no", "nee", "uit", "0", "disable", "disabled"}
_MENTION_RE = re.compile(r"<[#@&!]*(\d+)>")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class SettingError(ValueError):
    """A human-readable problem with a key or value."""


@dataclass(frozen=True)
class Setting:
    path: str                # dataclass attribute path, e.g. "automation.dry_run"
    kind: str                # bool | int | float | str | time | intlist
    live: bool               # True: applies immediately; False: after restart
    description: str
    choices: tuple = ()      # allowed values (after parsing), if restricted
    minimum: float | None = None
    maximum: float | None = None
    aliases: tuple[str, ...] = ()  # extra spellings (e.g. the YAML key name)

    @property
    def group(self) -> str:
        return self.path.split(".", 1)[0]


SETTINGS: tuple[Setting, ...] = (
    # -- missionchief pacing (live: the pacer is rewired on change) -------
    Setting("missionchief.base_url", "str", True, "MissionChief base URL"),
    Setting("missionchief.alliance_id", "int", True, "Alliance id", minimum=1),
    Setting("missionchief.min_delay", "float", True,
            "Minimum seconds between MC requests", minimum=0.5, maximum=120),
    Setting("missionchief.max_delay", "float", True,
            "Maximum seconds between MC requests (keep ≤ ~12 — this caps total "
            "throughput for everything)", minimum=1, maximum=120),
    Setting("missionchief.max_requests_per_minute", "int", True,
            "Hard cap on MC requests per minute", minimum=1, maximum=30),
    Setting("missionchief.circuit_breaker_cooldown_minutes", "int", True,
            "Pause after repeated failures (minutes)", minimum=1, maximum=180),
    # -- sync intervals (jobs are scheduled at startup) --------------------
    Setting("sync.members_interval", "int", False, "Member sweep interval (min)", minimum=15),
    Setting("sync.applications_interval", "int", False, "Applications interval (min)", minimum=1),
    Setting("sync.logs_interval", "int", False, "Alliance-log sync interval (min)", minimum=5),
    Setting("sync.treasury_interval", "int", False, "Treasury interval (min)", minimum=5),
    Setting("sync.expenses_interval", "int", False, "Expenses interval (min)", minimum=5),
    Setting("sync.expenses_backfill_pages_per_chunk", "int", True,
            "Expense-backfill pages per chunk", minimum=1, maximum=100),
    Setting("sync.expenses_backfill_interval", "int", False,
            "Expense-backfill interval (min)", minimum=5),
    Setting("sync.logs_backfill_pages_per_chunk", "int", True,
            "Log-backfill pages per chunk", minimum=1, maximum=100),
    Setting("sync.logs_backfill_interval", "int", False,
            "Log-backfill interval (min)", minimum=5),
    # -- discord ------------------------------------------------------------
    Setting("discord.guild_id", "int", False, "Discord server id"),
    Setting("discord.channels.admin_log", "int", True, "Admin log channel"),
    Setting("discord.channels.applications", "int", True, "Applications channel"),
    Setting("discord.channels.member_events", "int", True, "Member-events channel"),
    Setting("discord.channels.alliance_logs", "int", True, "Alliance-logs channel"),
    Setting("discord.channels.reports", "int", True, "Reports channel"),
    Setting("discord.channels.admin_approvals", "int", True,
            "Approve/deny embeds for requests needing a staff decision"),
    Setting("discord.verified_role_id", "int", True,
            "Role granted by !verify (0 = membersync off)",
            aliases=("verified_role",)),
    Setting("discord.channels.member_panel", "int", True,
            "Member-management panel channel (dossier button)"),
    Setting("discord.channels.request_panel", "int", True,
            "Training/building request panel channel"),
    Setting("discord.channels.event_pings", "int", True,
            "Role pings for alliance mission/event starts"),
    Setting("discord.channels.missions_forum", "int", True,
            "Missions-database forum channel (0 = off)"),
    Setting("discord.channels.mission_announce", "int", True,
            "New-mission announcement channel"),
    Setting("discord.notify_event_role_id", "int", True,
            "Role always pinged on a mission/event start",
            aliases=("notify_event_role",)),
    Setting("discord.staff_role_ids", "intlist", True,
            "Roles allowed to use the staff console (comma-separated)",
            aliases=("staff_roles",)),
    Setting("discord.admin_role_ids", "intlist", True,
            "Roles allowed to use admin commands (comma-separated)"),
    # -- automation ----------------------------------------------------------
    Setting("automation.dry_run", "bool", True,
            "SAFETY: detect + report only, perform NO game actions"),
    Setting("automation.reply_to_board", "bool", True,
            "Post replies/guides on the boards"),
    Setting("automation.training.enabled", "bool", False, "Training auto-start"),
    Setting("automation.training.thread_id", "int", True, "Training board thread"),
    Setting("automation.training.interval", "int", False, "Training poll interval (min)", minimum=2),
    Setting("automation.training.min_contribution_rate", "float", True,
            "Minimum contribution %% for training requests", minimum=0, maximum=100),
    Setting("automation.building.enabled", "bool", False, "Building auto-build"),
    Setting("automation.building.thread_id", "int", True, "Building board thread"),
    Setting("automation.building.interval", "int", False, "Building poll interval (min)", minimum=2),
    Setting("automation.building.min_alliance_funds", "int", True,
            "Never spend below this alliance balance", minimum=0),
    Setting("automation.building.set_tax_percent", "int", True,
            "Tax on new alliance buildings", choices=(0, 10, 20, 30, 40, 50)),
    Setting("automation.building.daily_build_enabled", "bool", False,
            "Daily worldwide hospital+prison build"),
    Setting("automation.building.daily_build_time", "time", False,
            "Daily build time (HH:MM, reports timezone)"),
    Setting("automation.missions_forum.enabled", "bool", False,
            "Daily missions-forum sync"),
    Setting("automation.missions_forum.sync_time", "time", False,
            "Missions-forum sync time (HH:MM, reports timezone)"),
    Setting("automation.missions_forum.announce_new", "bool", True,
            "Ping the announcement channel for new missions"),
    Setting("automation.missions_forum.max_posts_per_run", "int", True,
            "Forum posts/edits per sync run", minimum=1, maximum=500),
    Setting("automation.tax_warnings.enabled", "bool", False,
            "Automated 5%%-donation warnings (in-game PMs)"),
    Setting("automation.tax_warnings.min_rate", "float", True,
            "Minimum donation %% before warnings", minimum=0, maximum=100),
    Setting("automation.tax_warnings.min_days_between", "int", True,
            "Days between warnings to the same member", minimum=1, maximum=90),
    Setting("automation.tax_warnings.grace_hours", "int", True,
            "New-member grace period (hours)", minimum=0, maximum=720),
    Setting("automation.tax_warnings.max_per_run", "int", True,
            "Warnings per scan", minimum=1, maximum=25),
    Setting("automation.tax_warnings.auto_kick", "bool", True,
            "Automatically kick after 3 unresolved warnings"),
    Setting("automation.tax_warnings.interval_hours", "int", False,
            "Hours between warning scans", minimum=1, maximum=48),
    Setting("automation.events.enabled", "bool", False, "Events board polling"),
    Setting("automation.events.thread_id", "int", True, "Events board thread"),
    Setting("automation.events.interval", "int", False, "Events poll interval (min)", minimum=2),
    Setting("automation.events.min_contribution_rate", "float", True,
            "Minimum contribution %% for event requests", minimum=0, maximum=100),
    Setting("automation.mission.enabled", "bool", False, "Mission queue scheduler"),
    Setting("automation.mission.board_enabled", "bool", False, "Mission board polling"),
    Setting("automation.mission.thread_id", "int", True, "Mission board thread"),
    Setting("automation.mission.interval", "int", False, "Mission poll interval (min)", minimum=2),
    Setting("automation.mission.panel_channel_id", "int", True, "Mission panel channel"),
    Setting("automation.mission.min_contribution_rate", "float", True,
            "Minimum contribution %% for mission requests", minimum=0, maximum=100),
    # -- reports / geocoding / logging ---------------------------------------
    Setting("reports.daily_delay_minutes", "int", True,
            "Minutes after the daily reset before reports post", minimum=1, maximum=120),
    Setting("reports.timezone", "str", False, "Reports timezone (IANA name)"),
    Setting("geocoding.base_url", "str", False, "Geocoder base URL"),
    Setting("geocoding.api_key_param", "str", False, "Geocoder API-key query param"),
    Setting("geocoding.contact_email", "str", False, "Geocoder contact email"),
    Setting("geocoding.min_interval", "float", False,
            "Seconds between geocoder calls", minimum=0.2, maximum=30,
            aliases=("min_interval_seconds",)),
    Setting("logging.level", "str", True, "Log level",
            choices=("DEBUG", "INFO", "WARNING", "ERROR")),
)

_BY_PATH = {s.path: s for s in SETTINGS}


def _norm(key: str) -> str:
    return key.strip().lower().replace("-", "_").lstrip(".")


def resolve(key: str) -> Setting:
    """Find the setting for a (possibly partial) key.

    Accepts the full path, any alias, or a unique dotted suffix — so
    ``dry_run`` works, and ``enabled`` complains helpfully that it's
    ambiguous. Raises :class:`SettingError` with suggestions when unknown."""
    wanted = _norm(key)
    if not wanted:
        raise SettingError("give me a setting name, e.g. `dry_run`")

    matches = []
    for setting in SETTINGS:
        path = setting.path.lower()
        names = {path, *(a.lower() for a in setting.aliases)}
        # Alias / exact / suffix-on-a-dot-boundary matches.
        if wanted in names or path.endswith("." + wanted):
            matches.append(setting)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ", ".join(f"`{s.path}`" for s in matches)
        raise SettingError(f"`{key}` matches several settings — pick one: {options}")

    suggestions = difflib.get_close_matches(
        wanted, [s.path for s in SETTINGS]
        + [seg for s in SETTINGS for seg in s.path.split(".")],
        n=3, cutoff=0.6,
    )
    hint = f" Did you mean: {', '.join(f'`{m}`' for m in dict.fromkeys(suggestions))}?" if suggestions else ""
    raise SettingError(f"I don't know a setting `{key}`.{hint} (`!fra settings` lists everything)")


def parse_value(setting: Setting, raw: str, cfg) -> object:
    """Turn the user's text into a typed, validated value."""
    text = raw.strip()
    if not text:
        raise SettingError("give me a value too, e.g. `!fra set dry_run off`")

    if setting.kind == "bool":
        word = text.lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise SettingError(f"`{text}` isn't a yes/no value — use on/off (aan/uit)")

    if setting.kind in ("int", "float"):
        mention = _MENTION_RE.fullmatch(text)
        if mention:
            text = mention.group(1)
        try:
            value = int(text) if setting.kind == "int" else float(text)
        except ValueError:
            noun = "a whole number" if setting.kind == "int" else "a number"
            raise SettingError(f"`{text}` isn't {noun}") from None
        _check_bounds(setting, value)
        _check_cross(setting, value, cfg)
        return value

    if setting.kind == "time":
        if not _TIME_RE.match(text):
            raise SettingError(f"`{text}` isn't a time — use HH:MM, e.g. `03:00`")
        hour, minute = text.split(":")
        return f"{int(hour):02d}:{minute}"

    if setting.kind == "intlist":
        ids = []
        for part in re.split(r"[,\s]+", text):
            if not part:
                continue
            mention = _MENTION_RE.fullmatch(part)
            part = mention.group(1) if mention else part
            if part.lower() in ("none", "clear", "leeg", "-"):
                continue
            try:
                ids.append(int(part))
            except ValueError:
                raise SettingError(f"`{part}` isn't a role id or @mention") from None
        return tuple(ids)

    # str
    if setting.path == "reports.timezone":
        try:
            ZoneInfo(text)
        except Exception:
            raise SettingError(
                f"`{text}` isn't a timezone — use an IANA name like "
                "`Europe/Amsterdam` or `America/New_York`"
            ) from None
    if setting.path == "logging.level":
        text = text.upper()
    if setting.choices and text not in setting.choices:
        raise SettingError(
            f"`{text}` isn't valid — choose from: "
            + ", ".join(str(c) for c in setting.choices)
        )
    return text


def _check_bounds(setting: Setting, value) -> None:
    if setting.choices and value not in setting.choices:
        raise SettingError(
            "choose from: " + ", ".join(str(c) for c in setting.choices)
        )
    if setting.minimum is not None and value < setting.minimum:
        raise SettingError(f"must be at least {setting.minimum:g}")
    if setting.maximum is not None and value > setting.maximum:
        raise SettingError(f"must be at most {setting.maximum:g}")


def _check_cross(setting: Setting, value, cfg) -> None:
    """Cross-field sanity (the delay pair must stay ordered)."""
    if cfg is None:
        return
    if setting.path == "missionchief.min_delay":
        if value > cfg.missionchief.max_delay:
            raise SettingError(
                f"min_delay ({value:g}) can't exceed max_delay "
                f"({cfg.missionchief.max_delay:g})"
            )
    if setting.path == "missionchief.max_delay":
        if value < cfg.missionchief.min_delay:
            raise SettingError(
                f"max_delay ({value:g}) can't be below min_delay "
                f"({cfg.missionchief.min_delay:g})"
            )


def current(cfg, setting: Setting):
    node = cfg
    for part in setting.path.split("."):
        node = getattr(node, part)
    return node


def apply(cfg, setting: Setting, value) -> None:
    """Set the value on the (frozen) config tree. Services hold references to
    the nested objects, so an in-place set propagates everywhere at once —
    this module is the single sanctioned mutation point."""
    node = cfg
    parts = setting.path.split(".")
    for part in parts[:-1]:
        node = getattr(node, part)
    object.__setattr__(node, parts[-1], value)


def format_value(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, tuple):
        return ", ".join(str(v) for v in value) or "(none)"
    return str(value)


def serialize(value) -> str:
    """Stringify a parsed value for storage (parse_value re-reads it)."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, tuple):
        return ",".join(str(v) for v in value) or "none"
    return str(value)


# -- persistence (StateRepo-backed overrides) --------------------------------

def _override_key(setting: Setting) -> str:
    return f"{OVERRIDE_PREFIX}{setting.path}"


async def store_override(state, setting: Setting, value) -> None:
    await state.set(_override_key(setting), serialize(value))


async def clear_override(state, setting: Setting) -> bool:
    existed = await state.get(_override_key(setting)) is not None
    await state.delete(_override_key(setting))
    return existed


async def get_override(state, setting: Setting) -> str | None:
    return await state.get(_override_key(setting))


async def apply_stored_overrides(cfg, state) -> list[str]:
    """Re-apply every stored override on top of the freshly loaded YAML
    config (called once at startup, right after the DB connects). Returns
    human-readable lines describing what was applied or skipped."""
    lines: list[str] = []
    for setting in SETTINGS:
        raw = await state.get(_override_key(setting))
        if raw is None:
            continue
        try:
            value = parse_value(setting, raw, cfg)
            apply(cfg, setting, value)
            lines.append(f"{setting.path} = {format_value(value)} (override)")
        except SettingError as exc:
            lines.append(f"{setting.path}: stored override {raw!r} invalid ({exc}) — skipped")
    return lines


def post_apply(bot, setting: Setting) -> None:
    """Live side-effects for settings whose consumers cache values at
    startup: the MC pacer and the log level. Everything else reads the
    config tree at use-time and needs nothing extra."""
    if setting.group == "missionchief":
        bot.mc.reconfigure_pacing(bot.cfg.missionchief)
    elif setting.path == "logging.level":
        import logging as _logging

        level = getattr(_logging, bot.cfg.logging.level, _logging.INFO)
        _logging.getLogger().setLevel(level)
        _logging.getLogger("fra_bot").setLevel(level)
