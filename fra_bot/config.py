"""Configuration loading.

Non-secret settings live in ``config.yaml`` (see ``config.example.yaml``).
Secrets are read from environment variables / ``.env``:

* ``DISCORD_TOKEN``
* ``MC_EMAIL``
* ``MC_PASSWORD``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class MissionChiefConfig:
    base_url: str
    alliance_id: int
    cookie_path: Path
    min_delay: float
    max_delay: float
    max_requests_per_minute: int
    circuit_breaker_cooldown_minutes: int
    email: str
    password: str


@dataclass(frozen=True)
class SyncConfig:
    members_interval: int
    applications_interval: int
    logs_interval: int
    treasury_interval: int
    expenses_interval: int
    expenses_backfill_pages_per_chunk: int
    expenses_backfill_interval: int
    logs_backfill_pages_per_chunk: int
    logs_backfill_interval: int


@dataclass(frozen=True)
class DiscordChannels:
    admin_log: int
    applications: int
    member_events: int
    alliance_logs: int
    reports: int
    # Approve/deny embeds for requests that need a staff decision.
    admin_approvals: int = 0
    # The member-management panel (dossier button).
    member_panel: int = 0
    # The training/building request panel.
    request_panel: int = 0
    # Role pings for alliance mission/event starts (the event pinger).
    event_pings: int = 1421242306136113254
    # The missions-database forum (one post per einsaetze.json mission);
    # 0 = feature off.
    missions_forum: int = 0
    # Announcements for newly added missions (link to the forum post).
    mission_announce: int = 1524842963316773036
    # The vehicles-database forum (one post per LSSM vehicle); 0 = off.
    vehicles_forum: int = 1525985400521490607
    # Announcements for newly added/changed vehicles (link to the post);
    # 0 = no channel (announce stays off).
    vehicle_announce: int = 0
    # In-game DM mirror forum: every PM conversation ↔ one thread.
    dm_mirror: int = 1517694938501087342
    # The message panel (Send Message / Check Inbox / Reply buttons) —
    # the old bot kept it in the event-pings channel; 0 = off.
    dm_panel: int = 1421242306136113254
    # The class-availability panel: free training classes per agency,
    # refreshed hourly by the keeper; 0 = off.
    class_panel: int = 1421627971831070730
    # The academy-build panel (Fire/Police/Rescue/Coastal buttons); 0 = off.
    academy_panel: int = 1426226521231589507


@dataclass(frozen=True)
class DiscordConfig:
    token: str
    guild_id: int
    channels: DiscordChannels
    admin_role_ids: tuple[int, ...] = field(default_factory=tuple)
    # Role granted by !verify (0 = membersync disabled).
    verified_role_id: int = 0
    # Roles allowed to use the staff console (besides admins).
    staff_role_ids: tuple[int, ...] = ()
    # Always pinged on a mission/event start (the reference bot's
    # Notify-Event role); a region role is added when resolvable.
    notify_event_role_id: int = 669496241591418890
    # Pinged on new/updated-mission announcements in the mission_announce
    # channel (0 = no ping, just the message).
    mission_announce_role_id: int = 0
    # Pinged on new-vehicle announcements in the vehicle_announce channel
    # (0 = no ping, just the message).
    vehicle_announce_role_id: int = 0
    # Eventpinger announcement watcher: the channel where the official
    # MissionChief app announces EVERY alliance mission/event start (also
    # manual ones), and that app's user id. Each announcement gets a reply
    # pinging Notify-Event + the resolved region role. 0 = watcher off.
    event_watch_channel_id: int = 544461383358480385
    event_watch_app_id: int = 743939319122886657


@dataclass(frozen=True)
class TrainingAutomationConfig:
    enabled: bool
    thread_id: int
    interval: int
    min_contribution_rate: float
    preferred_academies: dict[str, int]


@dataclass(frozen=True)
class BuildingAutomationConfig:
    enabled: bool
    thread_id: int
    interval: int
    min_contribution_rate: float
    min_alliance_funds: int
    set_tax_percent: int
    # Daily worldwide auto-build: one hospital + one prison per day at a real
    # OSM location, funds-gated and deduped against existing buildings.
    daily_build_enabled: bool
    daily_build_time: str  # "HH:MM" in reports.timezone


@dataclass(frozen=True)
class AcademyAutomationConfig:
    """Discord-panel academy builds at a fixed address (overlap allowed).

    ``enabled`` gates the funds-gated retry queue; the panel buttons still
    enqueue when it is off, but a low-funds build only retries once the
    queue poller runs. ``role_id`` = 0 means admins only."""
    enabled: bool
    role_id: int
    interval: int
    address: str
    min_alliance_funds: int
    autoscale: bool


@dataclass(frozen=True)
class EventsAutomationConfig:
    enabled: bool
    thread_id: int
    interval: int
    min_contribution_rate: float


@dataclass(frozen=True)
class MissionAutomationConfig:
    """Custom "Own mission" scheduling.

    ``enabled`` gates the queue scheduler (starting missions); the Discord
    panel/slash command can still enqueue requests when it is off, they
    simply wait. ``board_enabled`` gates parsing structured mission posts
    from the board thread. Both are off by default, and starts still honour
    the global ``dry_run`` switch.
    """
    enabled: bool
    board_enabled: bool
    thread_id: int
    interval: int
    panel_channel_id: int
    min_contribution_rate: float


@dataclass(frozen=True)
class MissionsForumConfig:
    """The missions-database forum: every mission from einsaetze.json as a
    tagged forum post, checked daily for new/changed missions. The forum
    channel itself is ``discord.channels.missions_forum``; ``announce_new``
    additionally pings ``discord.channels.mission_announce`` with a link
    whenever a brand-new mission appears (off by default)."""
    enabled: bool
    sync_time: str  # "HH:MM" in reports.timezone
    announce_new: bool
    max_posts_per_run: int


@dataclass(frozen=True)
class VehiclesForumConfig:
    """The vehicles-database forum: every vehicle from the LSSM catalog as a
    tagged forum post, checked daily for new/changed vehicles. The forum
    channel is ``discord.channels.vehicles_forum``; ``announce_new``
    additionally pings ``discord.channels.vehicle_announce`` with a link
    whenever a brand-new vehicle appears (off by default — the catalog rarely
    changes)."""
    enabled: bool
    sync_time: str  # "HH:MM" in reports.timezone
    announce_new: bool
    max_posts_per_run: int


@dataclass(frozen=True)
class RankRolesConfig:
    """Credit rank roles (the old bot's RoleBasedCredits): every linked
    member carries one Discord rank role based on MissionChief earned
    credits; promotions are congratulated in ``promotion_channel_id``.
    ``role_ids`` overrides the built-in rank→role mapping per rank key."""
    enabled: bool
    interval: int  # minutes between syncs
    promotion_channel_id: int
    announce_first_assignment: bool
    role_ids: dict[str, int]


@dataclass(frozen=True)
class DmMirrorConfig:
    """In-game DM mirror: the inbox is scanned every ``interval`` minutes
    and every PM conversation (incoming AND ones the bot account started)
    is mirrored into the ``discord.channels.dm_mirror`` forum — one thread
    per conversation. Staff replies typed in a thread are sent back into
    the game conversation (those honour the global dry_run)."""
    enabled: bool
    interval: int  # minutes between inbox scans


@dataclass(frozen=True)
class TaxWarningsConfig:
    """Automated member tax (5% alliance donation) warnings: in-game PMs
    with escalating severity, a kick flag after three unresolved warnings,
    and an immediate reset once the member fixes their donation."""
    enabled: bool
    min_rate: float
    min_days_between: int
    grace_hours: int
    max_per_run: int
    auto_kick: bool
    interval_hours: int


@dataclass(frozen=True)
class AutomationConfig:
    dry_run: bool
    reply_to_board: bool
    training: TrainingAutomationConfig
    building: BuildingAutomationConfig
    academy: AcademyAutomationConfig
    events: EventsAutomationConfig
    mission: MissionAutomationConfig
    tax_warnings: TaxWarningsConfig
    missions_forum: MissionsForumConfig
    vehicles_forum: VehiclesForumConfig
    dm_mirror: DmMirrorConfig
    rank_roles: RankRolesConfig


@dataclass(frozen=True)
class ScheduledReport:
    report: str          # registered report name
    period: str          # period name (today/week/month/…)
    cadence: str         # daily | weekly | monthly | yearly
    channel_id: int
    weekday: int = 0     # for weekly: 0=Monday
    day: int = 1         # for monthly/yearly: day-of-month
    month: int = 1       # for yearly: month (1=January)


@dataclass(frozen=True)
class ReportsConfig:
    daily_delay_minutes: int
    timezone: str
    scheduled: tuple[ScheduledReport, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GeocodingConfig:
    """Geocoding provider settings.

    Defaults to free OSM Nominatim (no key). Point ``base_url`` at any
    Nominatim-compatible service (e.g. maps.co, LocationIQ) and set the key
    via the ``GEOCODER_API_KEY`` environment variable to use your own quota.
    ``api_key_param`` is the query parameter the provider expects the key in
    (``api_key`` for maps.co / OpenCage, ``key`` for LocationIQ).
    """
    base_url: str
    api_key: str
    api_key_param: str
    contact_email: str
    min_interval: float


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    path: Path
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class Config:
    database: DatabaseConfig
    missionchief: MissionChiefConfig
    sync: SyncConfig
    discord: DiscordConfig
    automation: AutomationConfig
    reports: ReportsConfig
    geocoding: GeocodingConfig
    logging: LoggingConfig


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Environment variable {name} is not set. "
            f"Add it to your .env file (see .env.example)."
        )
    return value


def _get(data: dict, *keys: str, default=None, required: bool = False):
    node = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            if required:
                raise ConfigError(f"Missing config key: {'.'.join(keys)}")
            return default
        node = node[key]
    return node


def _valid_timezone(name: str) -> str:
    """Validate an IANA timezone at LOAD time. A typo'd YAML value used to
    pass through untouched and only blow up later inside the report loops
    and daily jobs — every one of them silently dead. Fail fast instead."""
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError on bad keys
        raise ConfigError(
            f"reports.timezone {name!r} is not a valid IANA timezone "
            f"(e.g. America/New_York): {exc}"
        ) from exc
    return name


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load configuration from YAML + environment variables."""
    load_dotenv()

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(
            f"Config file {config_path} not found. "
            f"Copy config.example.yaml to config.yaml and edit it."
        )
    with config_path.open("r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            hint = ""
            if "'\\t'" in str(exc) or "\t" in str(exc):
                hint = (
                    " — this is almost always a TAB character; YAML only allows "
                    "spaces. Replace tabs with spaces (e.g. in inline comments)."
                )
            raise ConfigError(f"Could not parse {config_path}: {exc}{hint}") from exc

    min_delay = float(_get(raw, "missionchief", "min_delay", default=4.0))
    max_delay = float(_get(raw, "missionchief", "max_delay", default=9.0))
    if min_delay <= 0 or max_delay < min_delay:
        raise ConfigError("missionchief.min_delay/max_delay must satisfy 0 < min <= max")

    channels = _get(raw, "discord", "channels", default={}) or {}

    return Config(
        database=DatabaseConfig(
            path=Path(_get(raw, "database", "path", default="data/fra_bot.sqlite3")),
        ),
        missionchief=MissionChiefConfig(
            base_url=str(
                _get(raw, "missionchief", "base_url", default="https://www.missionchief.com")
            ).rstrip("/"),
            alliance_id=int(_get(raw, "missionchief", "alliance_id", required=True)),
            cookie_path=Path(_get(raw, "missionchief", "cookie_path", default="data/cookies.json")),
            min_delay=min_delay,
            max_delay=max_delay,
            max_requests_per_minute=int(
                _get(raw, "missionchief", "max_requests_per_minute", default=10)
            ),
            circuit_breaker_cooldown_minutes=int(
                _get(raw, "missionchief", "circuit_breaker_cooldown_minutes", default=15)
            ),
            email=_require_env("MC_EMAIL"),
            password=_require_env("MC_PASSWORD"),
        ),
        sync=SyncConfig(
            members_interval=int(_get(raw, "sync", "members_interval", default=60)),
            applications_interval=int(_get(raw, "sync", "applications_interval", default=5)),
            logs_interval=int(_get(raw, "sync", "logs_interval", default=15)),
            treasury_interval=int(_get(raw, "sync", "treasury_interval", default=30)),
            expenses_interval=int(_get(raw, "sync", "expenses_interval", default=60)),
            expenses_backfill_pages_per_chunk=int(
                _get(raw, "sync", "expenses_backfill_pages_per_chunk", default=30)
            ),
            expenses_backfill_interval=int(
                _get(raw, "sync", "expenses_backfill_interval", default=15)
            ),
            logs_backfill_pages_per_chunk=int(
                _get(raw, "sync", "logs_backfill_pages_per_chunk", default=20)
            ),
            logs_backfill_interval=int(
                _get(raw, "sync", "logs_backfill_interval", default=15)
            ),
        ),
        discord=DiscordConfig(
            token=_require_env("DISCORD_TOKEN"),
            guild_id=int(_get(raw, "discord", "guild_id", default=0)),
            channels=DiscordChannels(
                admin_log=int(channels.get("admin_log", 0)),
                applications=int(channels.get("applications", 0)),
                member_events=int(channels.get("member_events", 0)),
                alliance_logs=int(channels.get("alliance_logs", 0)),
                reports=int(channels.get("reports", 0)),
                admin_approvals=int(channels.get("admin_approvals", 0)),
                member_panel=int(channels.get("member_panel", 0)),
                request_panel=int(channels.get("request_panel", 0)),
                event_pings=int(
                    channels.get("event_pings", 1421242306136113254)
                ),
                missions_forum=int(channels.get("missions_forum", 0)),
                vehicles_forum=int(
                    channels.get("vehicles_forum", 1525985400521490607)
                ),
                vehicle_announce=int(channels.get("vehicle_announce", 0)),
                mission_announce=int(
                    channels.get("mission_announce", 1524842963316773036)
                ),
                dm_mirror=int(channels.get("dm_mirror", 1517694938501087342)),
                dm_panel=int(channels.get("dm_panel", 1421242306136113254)),
                class_panel=int(
                    channels.get("class_panel", 1421627971831070730)
                ),
                academy_panel=int(
                    channels.get("academy_panel", 1426226521231589507)
                ),
            ),
            admin_role_ids=tuple(
                int(r) for r in (_get(raw, "discord", "admin_role_ids", default=[]) or [])
            ),
            verified_role_id=int(_get(raw, "discord", "verified_role_id", default=0)),
            staff_role_ids=tuple(
                int(r) for r in (_get(raw, "discord", "staff_role_ids", default=[]) or [])
            ),
            notify_event_role_id=int(
                _get(raw, "discord", "notify_event_role_id", default=669496241591418890)
            ),
            mission_announce_role_id=int(
                _get(raw, "discord", "mission_announce_role_id", default=0)
            ),
            vehicle_announce_role_id=int(
                _get(raw, "discord", "vehicle_announce_role_id", default=0)
            ),
            event_watch_channel_id=int(
                _get(raw, "discord", "event_watch_channel_id",
                     default=544461383358480385)
            ),
            event_watch_app_id=int(
                _get(raw, "discord", "event_watch_app_id",
                     default=743939319122886657)
            ),
        ),
        automation=AutomationConfig(
            dry_run=bool(_get(raw, "automation", "dry_run", default=True)),
            reply_to_board=bool(_get(raw, "automation", "reply_to_board", default=True)),
            training=TrainingAutomationConfig(
                enabled=bool(_get(raw, "automation", "training", "enabled", default=False)),
                thread_id=int(_get(raw, "automation", "training", "thread_id", default=5935)),
                interval=int(_get(raw, "automation", "training", "interval", default=5)),
                min_contribution_rate=float(
                    _get(raw, "automation", "training", "min_contribution_rate", default=5.0)
                ),
                preferred_academies={
                    str(k): int(v)
                    for k, v in (
                        _get(raw, "automation", "training", "preferred_academies", default={})
                        or {}
                    ).items()
                },
            ),
            building=BuildingAutomationConfig(
                enabled=bool(_get(raw, "automation", "building", "enabled", default=False)),
                thread_id=int(_get(raw, "automation", "building", "thread_id", default=6165)),
                interval=int(_get(raw, "automation", "building", "interval", default=5)),
                min_contribution_rate=float(
                    _get(raw, "automation", "building", "min_contribution_rate", default=5.0)
                ),
                min_alliance_funds=int(
                    _get(raw, "automation", "building", "min_alliance_funds", default=2_000_000)
                ),
                set_tax_percent=int(
                    _get(raw, "automation", "building", "set_tax_percent", default=20)
                ),
                daily_build_enabled=bool(
                    _get(raw, "automation", "building", "daily_build_enabled", default=False)
                ),
                daily_build_time=str(
                    _get(raw, "automation", "building", "daily_build_time", default="03:00")
                ),
            ),
            academy=AcademyAutomationConfig(
                enabled=bool(_get(raw, "automation", "academy", "enabled", default=False)),
                role_id=int(_get(raw, "automation", "academy", "role_id", default=0)),
                interval=int(_get(raw, "automation", "academy", "interval", default=10)),
                address=str(
                    _get(raw, "automation", "academy", "address",
                         default="Tepper Avenue, 10454 New York, Manhattan")
                ),
                min_alliance_funds=int(
                    _get(raw, "automation", "academy", "min_alliance_funds", default=2_000_000)
                ),
                autoscale=bool(
                    _get(raw, "automation", "academy", "autoscale", default=False)
                ),
            ),
            events=EventsAutomationConfig(
                enabled=bool(_get(raw, "automation", "events", "enabled", default=False)),
                thread_id=int(_get(raw, "automation", "events", "thread_id", default=15293)),
                interval=int(_get(raw, "automation", "events", "interval", default=5)),
                min_contribution_rate=float(
                    _get(raw, "automation", "events", "min_contribution_rate", default=5.0)
                ),
            ),
            mission=MissionAutomationConfig(
                enabled=bool(_get(raw, "automation", "mission", "enabled", default=False)),
                board_enabled=bool(
                    _get(raw, "automation", "mission", "board_enabled", default=False)
                ),
                thread_id=int(_get(raw, "automation", "mission", "thread_id", default=15310)),
                interval=int(_get(raw, "automation", "mission", "interval", default=5)),
                panel_channel_id=int(
                    _get(raw, "automation", "mission", "panel_channel_id", default=0)
                ),
                min_contribution_rate=float(
                    _get(raw, "automation", "mission", "min_contribution_rate", default=5.0)
                ),
            ),
            missions_forum=MissionsForumConfig(
                enabled=bool(
                    _get(raw, "automation", "missions_forum", "enabled", default=False)
                ),
                sync_time=str(
                    _get(raw, "automation", "missions_forum", "sync_time", default="04:00")
                ),
                announce_new=bool(
                    _get(raw, "automation", "missions_forum", "announce_new", default=False)
                ),
                max_posts_per_run=int(
                    _get(
                        raw, "automation", "missions_forum", "max_posts_per_run",
                        default=100,
                    )
                ),
            ),
            vehicles_forum=VehiclesForumConfig(
                enabled=bool(
                    _get(raw, "automation", "vehicles_forum", "enabled", default=False)
                ),
                sync_time=str(
                    _get(raw, "automation", "vehicles_forum", "sync_time", default="04:30")
                ),
                announce_new=bool(
                    _get(raw, "automation", "vehicles_forum", "announce_new", default=False)
                ),
                max_posts_per_run=int(
                    _get(
                        raw, "automation", "vehicles_forum", "max_posts_per_run",
                        default=100,
                    )
                ),
            ),
            dm_mirror=DmMirrorConfig(
                enabled=bool(
                    _get(raw, "automation", "dm_mirror", "enabled", default=False)
                ),
                interval=int(
                    _get(raw, "automation", "dm_mirror", "interval", default=15)
                ),
            ),
            rank_roles=RankRolesConfig(
                enabled=bool(
                    _get(raw, "automation", "rank_roles", "enabled", default=False)
                ),
                interval=int(
                    _get(raw, "automation", "rank_roles", "interval", default=60)
                ),
                promotion_channel_id=int(
                    _get(
                        raw, "automation", "rank_roles", "promotion_channel_id",
                        default=543935264708362251,
                    )
                ),
                announce_first_assignment=bool(
                    _get(
                        raw, "automation", "rank_roles",
                        "announce_first_assignment", default=False,
                    )
                ),
                role_ids={
                    str(k): int(v)
                    for k, v in (
                        _get(raw, "automation", "rank_roles", "role_ids", default={})
                        or {}
                    ).items()
                },
            ),
            tax_warnings=TaxWarningsConfig(
                enabled=bool(
                    _get(raw, "automation", "tax_warnings", "enabled", default=False)
                ),
                min_rate=float(
                    _get(raw, "automation", "tax_warnings", "min_rate", default=5.0)
                ),
                min_days_between=int(
                    _get(raw, "automation", "tax_warnings", "min_days_between", default=7)
                ),
                grace_hours=int(
                    _get(raw, "automation", "tax_warnings", "grace_hours", default=24)
                ),
                max_per_run=int(
                    _get(raw, "automation", "tax_warnings", "max_per_run", default=5)
                ),
                auto_kick=bool(
                    _get(raw, "automation", "tax_warnings", "auto_kick", default=False)
                ),
                interval_hours=int(
                    _get(raw, "automation", "tax_warnings", "interval_hours", default=6)
                ),
            ),
        ),
        geocoding=GeocodingConfig(
            base_url=str(
                _get(raw, "geocoding", "base_url", default="https://nominatim.openstreetmap.org")
            ).rstrip("/"),
            # Secret: read from the environment, never config.yaml. Optional.
            api_key=os.environ.get("GEOCODER_API_KEY", "").strip(),
            api_key_param=str(_get(raw, "geocoding", "api_key_param", default="api_key")),
            contact_email=str(_get(raw, "geocoding", "contact_email", default="")).strip(),
            min_interval=float(
                _get(raw, "geocoding", "min_interval_seconds", default=1.1)
            ),
        ),
        reports=ReportsConfig(
            daily_delay_minutes=int(_get(raw, "reports", "daily_delay_minutes", default=10)),
            timezone=_valid_timezone(
                str(_get(raw, "reports", "timezone", default="America/New_York"))
            ),
            scheduled=tuple(
                ScheduledReport(
                    report=str(item["report"]),
                    period=str(item.get("period", "today")),
                    cadence=str(item.get("cadence", "daily")).lower(),
                    channel_id=int(item.get("channel_id", 0)),
                    weekday=int(item.get("weekday", 0)),
                    day=int(item.get("day", 1)),
                    month=int(item.get("month", 1)),
                )
                for item in (_get(raw, "reports", "scheduled", default=[]) or [])
                if item.get("report") and item.get("channel_id")
            ),
        ),
        logging=LoggingConfig(
            level=str(_get(raw, "logging", "level", default="INFO")).upper(),
            path=Path(_get(raw, "logging", "path", default="logs/fra_bot.log")),
            max_bytes=int(_get(raw, "logging", "max_bytes", default=5 * 1024 * 1024)),
            backup_count=int(_get(raw, "logging", "backup_count", default=5)),
        ),
    )
