"""Static guards on the private auto-build userscript.

The script runs in a browser we cannot reach from CI, so there is nothing
to execute here. What CAN be checked is the handful of promises the script
makes to its user — and those are exactly the ones that cost real money if
they quietly rot: coins are never spent, dry run is the default, the world
pool holds real coordinates, and nothing secret is baked into a file that
lives in a public repository.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "fra-auto-build.user.js"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_userscript_metadata_is_complete(source):
    header = source.split("// ==/UserScript==")[0]
    assert "// ==UserScript==" in header
    for key in ("@name", "@version", "@match", "@updateURL", "@downloadURL"):
        assert key in header, f"missing {key} in the userscript header"
    assert "https://www.missionchief.com/*" in header


def test_it_never_spends_coins(source):
    # Two independent brakes, both from the bot's builder: the form field is
    # pinned to 0, and any button whose label mentions coins is refused.
    assert 'setField(doc, win, "build_with_coins", "0")' in source
    assert 'String(prep.snapshot.coins) !== "0"' in source
    assert '!text.includes("coins")' in source
    # The delivery step filters href AND label together.
    assert 'if (/coin/i.test(haystack)) continue;' in source
    # Nothing may ever set the field to anything else.
    others = re.findall(r'"build_with_coins",\s*"(?!0")', source)
    assert others == []


def test_dry_run_is_the_default(source):
    block = source.split("const DEFAULTS = {")[1].split("};")[0]
    assert re.search(r"\bdryRun:\s*true\b", block)
    assert re.search(r"\benabled:\s*false\b", block)
    # A fire station without its Quint must stop, not build something else.
    assert re.search(r"\bstrictVehicle:\s*true\b", block)
    assert re.search(r'\bstartingVehicle:\s*"Quint"', block)


def test_the_delivery_defaults_are_what_was_asked_for(source):
    block = source.split("const DEFAULTS = {")[1].split("};")[0]
    # Level, staff limit and storage yes; extensions explicitly NOT.
    assert re.search(r"\bmaxLevel:\s*true\b", block)
    assert re.search(r"\bsetStaffLimit:\s*true\b", block)
    assert re.search(r"\bstaffLimit:\s*400\b", block)
    assert re.search(r"\bbuyStorage:\s*true\b", block)
    assert re.search(r"\bbuyExtensions:\s*false\b", block)


def test_every_build_is_a_personal_build(source):
    # There is no alliance mode: the alliance treasury is the bot's job.
    assert "buildAsAlliance" not in source
    assert "alliance: false," in source


def test_a_region_without_a_dispatch_center_is_never_built_in(source):
    # Not "the nearest center": matched per region, and a country without
    # one is skipped and reported rather than guessed at.
    assert "function matchDispatch(" in source
    assert "function parseDispatchRules(" in source
    assert "noteMissingDispatch(" in source
    assert "distanceMeters" not in source.split("function matchDispatch(")[1] \
        .split("function noteMissingDispatch(")[0]


def test_world_pool_holds_real_coordinates(source):
    block = source.split("const WORLD_POINTS = [")[1].split("\n  ];")[0]
    points = re.findall(r'\[\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?),\s*"([^"]+)"\]', block)
    assert len(points) >= 100, "the worldwide pool got thin"
    labels = set()
    for lat, lng, label in points:
        assert -90.0 <= float(lat) <= 90.0, label
        assert -180.0 <= float(lng) <= 180.0, label
        assert label.isascii(), f"non-ASCII place name: {label}"
        assert label not in labels, f"duplicate place: {label}"
        labels.add(label)
    # Every inhabited continent, so "random worldwide" really roams.
    for country in ("USA", "Brazil", "Germany", "Kenya", "Japan", "Australia"):
        assert any(label.endswith(country) for label in labels), country


def test_nothing_secret_is_baked_in(source):
    assert "discord.com/api/webhooks" not in source
    assert "discordapp.com/api/webhooks" not in source
    assert not re.search(r"\b(password|api[_-]?key|secret|token)\s*[:=]\s*['\"]", source)


def test_the_duplicate_and_pacing_rails_are_present(source):
    assert "DUPLICATE_RADIUS_M = 250" in source          # same figure as the bot
    assert "Math.max(20, Number(settings.intervalSeconds)" in source
    assert "FINISH_IDLE_LIMIT" in source                 # the bot's finisher idea
    assert "MAX_DELIVERY_STEPS" in source                # bounded purchases
    assert "maxStorageBuys" in source                    # bounded storage presses
