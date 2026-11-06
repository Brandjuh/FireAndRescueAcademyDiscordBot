"""Static guards on the shared FRA menu in the userscripts.

Every FRA userscript puts its entries under ONE "FRA" navbar dropdown. A
userscript cannot import another, so the menu code is a block copied
verbatim into each script. These tests keep the copies identical and pin
the bits that made the profile sync vanish for players on the bare
missionchief.com host. (Behaviour was checked in headless Chromium against
a mock navbar; there is no browser in CI.)
"""

import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
SCRIPTS = sorted(TOOLS.glob("fra-*.user.js"))
START = "// ------------------------------------------------ FRA menu (shared block)"
END = "// ----------------------------------------------- /FRA menu (shared block)"


def _block(source: str) -> str:
    assert source.count(START) == 1 and source.count(END) == 1
    return source[source.index(START):source.index(END) + len(END)]


def test_there_are_fra_userscripts():
    names = {p.name for p in SCRIPTS}
    assert {"fra-profile-sync.user.js", "fra-auto-build.user.js"} <= names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_carries_the_identical_menu_block(script):
    reference = _block(SCRIPTS[0].read_text(encoding="utf-8"))
    assert _block(script.read_text(encoding="utf-8")) == reference


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_uses_the_menu_and_no_own_navbar_button(script):
    source = script.read_text(encoding="utf-8")
    assert re.search(r"fraMenuAdd\(\{", source.replace(_block(source), ""))
    # the old profile-sync button appended an <a> straight into the navbar
    assert "nav.appendChild(button)" not in source


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_runs_on_both_game_hosts(script):
    source = script.read_text(encoding="utf-8")
    header = source.split("// ==/UserScript==")[0]
    assert "https://www.missionchief.com/*" in header
    assert "https://missionchief.com/*" in header
    # and fetches stay on the host the player is on (cross-host = no session)
    assert "window.location.origin" in source


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_menu_inside_the_games_frames(script):
    source = script.read_text(encoding="utf-8")
    assert "if (window.top !== window.self) return;" in source


def test_public_entries_come_before_private_ones():
    sync = (TOOLS / "fra-profile-sync.user.js").read_text(encoding="utf-8")
    build = (TOOLS / "fra-auto-build.user.js").read_text(encoding="utf-8")
    sync_orders = [int(n) for n in re.findall(r"order: (\d+),", sync)]
    build_orders = [int(n) for n in re.findall(r"order: (\d+),", build)]
    assert sync_orders and max(sync_orders) < 50
    assert build_orders and min(build_orders) >= 50
