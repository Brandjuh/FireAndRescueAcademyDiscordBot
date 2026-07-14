"""Runtime settings: key resolution, natural value parsing, apply on the
frozen config tree, and override persistence across a (simulated) restart."""

import pytest
import pytest_asyncio

from fra_bot.core.settings import (
    SETTINGS,
    SettingError,
    apply,
    apply_stored_overrides,
    clear_override,
    current,
    format_value,
    parse_value,
    resolve,
    store_override,
)
from fra_bot.db.database import Database
from fra_bot.db.repos import StateRepo

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from fra_bot.config import load_config

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("MC_EMAIL", "x@example.com")
    monkeypatch.setenv("MC_PASSWORD", "x")
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return load_config(path)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "settings.sqlite3")
    await database.connect()
    yield database
    await database.close()


# -- key resolution -----------------------------------------------------------

def test_resolve_by_unique_suffix_and_alias():
    assert resolve("dry_run").path == "automation.dry_run"
    assert resolve("max_delay").path == "missionchief.max_delay"
    assert resolve("training.enabled").path == "automation.training.enabled"
    assert resolve("automation.dry_run").path == "automation.dry_run"
    # YAML spelling maps via alias.
    assert resolve("min_interval_seconds").path == "geocoding.min_interval"


def test_resolve_ambiguous_lists_options():
    with pytest.raises(SettingError) as err:
        resolve("enabled")
    assert "training.enabled" in str(err.value)
    assert "building.enabled" in str(err.value)


def test_resolve_unknown_suggests():
    with pytest.raises(SettingError) as err:
        resolve("dryrun")
    assert "dry_run" in str(err.value)


# -- value parsing -------------------------------------------------------------

def test_parse_natural_booleans(cfg):
    setting = resolve("dry_run")
    for word in ("on", "aan", "ja", "yes", "true", "1"):
        assert parse_value(setting, word, cfg) is True
    for word in ("off", "uit", "nee", "no", "false", "0"):
        assert parse_value(setting, word, cfg) is False
    with pytest.raises(SettingError):
        parse_value(setting, "misschien", cfg)


def test_parse_numbers_bounds_and_choices(cfg):
    with pytest.raises(SettingError):
        parse_value(resolve("max_requests_per_minute"), "99", cfg)   # > max 30
    assert parse_value(resolve("set_tax_percent"), "20", cfg) == 20
    with pytest.raises(SettingError):
        parse_value(resolve("set_tax_percent"), "25", cfg)           # not a choice


def test_parse_delay_pair_stays_ordered(cfg):
    # min_delay above the current max_delay (9.0) must be refused.
    with pytest.raises(SettingError):
        parse_value(resolve("min_delay"), "30", cfg)
    with pytest.raises(SettingError):
        parse_value(resolve("max_delay"), "2", cfg)                  # < min 4.0
    assert parse_value(resolve("max_delay"), "12", cfg) == 12.0


def test_parse_time_channel_and_rolelist(cfg):
    assert parse_value(resolve("daily_build_time"), "3:30", cfg) == "03:30"
    with pytest.raises(SettingError):
        parse_value(resolve("daily_build_time"), "25:00", cfg)
    # Channel mention -> id.
    assert parse_value(resolve("channels.reports"), "<#123456>", cfg) == 123456
    # Role mentions + commas -> tuple of ids; 'none' clears.
    assert parse_value(resolve("admin_role_ids"), "<@&11>, 22", cfg) == (11, 22)
    assert parse_value(resolve("admin_role_ids"), "none", cfg) == ()
    # Timezones are validated.
    assert parse_value(resolve("timezone"), "Europe/Amsterdam", cfg) == "Europe/Amsterdam"
    with pytest.raises(SettingError):
        parse_value(resolve("timezone"), "Mars/OlympusMons", cfg)


# -- apply + persistence --------------------------------------------------------

def test_apply_mutates_nested_frozen_config(cfg):
    setting = resolve("dry_run")
    assert current(cfg, setting) is True
    apply(cfg, setting, False)
    assert cfg.automation.dry_run is False
    # "min_alliance_funds" alone is now ambiguous (building + academy), so
    # the suffix must be qualified — exactly the resolver's intended behaviour.
    nested = resolve("building.min_alliance_funds")
    apply(cfg, nested, 5_000_000)
    assert cfg.automation.building.min_alliance_funds == 5_000_000


async def test_overrides_survive_restart(cfg, db):
    state = StateRepo(db)
    await store_override(state, resolve("dry_run"), False)
    await store_override(state, resolve("max_delay"), 12.0)
    await store_override(state, resolve("admin_role_ids"), (11, 22))

    # Simulated restart: fresh cfg from yaml, overrides re-applied on top.
    lines = await apply_stored_overrides(cfg, state)
    assert cfg.automation.dry_run is False
    assert cfg.missionchief.max_delay == 12.0
    assert cfg.discord.admin_role_ids == (11, 22)
    assert any("dry_run" in line for line in lines)

    # Clearing one removes only that override.
    assert await clear_override(state, resolve("dry_run")) is True
    assert await clear_override(state, resolve("dry_run")) is False


async def test_invalid_stored_override_is_skipped(cfg, db):
    state = StateRepo(db)
    await state.set("config_override:automation.dry_run", "misschien")
    lines = await apply_stored_overrides(cfg, state)
    assert cfg.automation.dry_run is True                # unchanged
    assert any("skipped" in line for line in lines)


def test_every_setting_resolves_on_real_config(cfg):
    # The registry must stay in sync with the dataclasses: every path reads.
    for setting in SETTINGS:
        value = current(cfg, setting)
        assert format_value(value) is not None
