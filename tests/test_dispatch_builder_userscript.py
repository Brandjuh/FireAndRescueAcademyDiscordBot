"""Static guards on the private dispatch-builder userscript.

Like the auto-build script it runs in a browser CI cannot reach, so this
pins the promises that matter: dry run by default, never coins or a
building complex, mutual-aid centers ignored, the two edit-page boxes by
their real field names, and a sane place list.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "fra-dispatch-builder.user.js"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _defaults(source):
    return source.split("const DEFAULTS = {")[1].split("};")[0]


def test_dry_run_and_safe_defaults(source):
    block = _defaults(source)
    assert re.search(r"\bdryRun:\s*true\b", block)
    assert re.search(r"\benabled:\s*false\b", block)
    assert re.search(r"\bmaxPrice:\s*0\b", block)          # a dispatch center is free
    assert re.search(r"\bincludeTerritories:\s*false\b", block)
    assert 'stateTemplate: "Z. USA - {STATE} ({CODE})"' in block
    assert 'countryTemplate: "Z. {COUNTRY}"' in block


def test_it_only_ever_builds_a_dispatch_center_with_credits(source):
    assert 'const DISPATCH_TYPE = "1";' in source
    assert 'params.set("build_with_coins", "");' in source
    assert re.findall(r'"build_with_coins",\s*"(?!")', source) == []
    assert 'params.get("build_with_coins") !== ""' in source
    assert "coins_activate" in source and "alliance_activate" in source
    assert "const COMPLEX_RE = /complex/i;" in source
    assert "building_complex_bases" not in source


def test_mutual_aid_centers_are_ignored(source):
    pattern = re.search(r"const MUTUAL_AID_RE = (/.+/i);", source).group(1)
    regex = re.compile(pattern[1:-2], re.I)
    for name in ("[MUTAL AID] WINNIPEG", "MUTAL AID NORFOLK", "Z. [MUTAL AID] JAKARTA",
                 "z. [MUTAL AID] CLEWISTON", "[MUTUAL AID] Somewhere"):
        assert regex.search(name), name
    assert not regex.search("Z. USA - TEXAS (TX)")


def test_new_centers_get_both_boxes_and_are_read_back(source):
    # Live edit page of a dispatch center (Nov 2026).
    assert 'name="building[active]"' in source
    assert 'name="building[generate_own_missions]"' in source
    assert "Disable" in source and "own coverage area" in source
    assert "const check = await readOptions(id);" in source


def test_place_lists(source):
    states = re.findall(r'\["([A-Z]{2})", "([^"]+)", (-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\],',
                        source.split("const STATES = [")[1].split("];")[0])
    assert len(states) == 50
    assert len({code for code, *_ in states}) == 50
    for code, name, lat, lng in states:
        assert 18 < float(lat) < 72 and -170 < float(lng) < -66, (code, name)
    countries = re.findall(r'\["([A-Z]{2})", "([^"]+)", (-?[\d.]+), (-?[\d.]+), (true|false)\],',
                           source.split("const COUNTRIES = [")[1].split("];")[0])
    sovereign = [c for c in countries if c[4] == "true"]
    assert len(sovereign) >= 190
    assert "US" not in {c[0] for c in countries}          # the US goes per state
    for code, name, lat, lng, _ in countries:
        assert -90 <= float(lat) <= 90 and -180 <= float(lng) <= 180, (code, name)


def test_rules_for_auto_build_are_keyed_on_codes_and_ids(source):
    # One rule per covered place, language-proof and unambiguous:
    #   [cc:cz] = #5792738 Z. Tsjechië      [iso:us-mo] = #274343 Z. USA - ...
    assert "`[iso:${key.toLowerCase()}]`" in source
    assert "`[cc:${key.toLowerCase()}]`" in source
    assert "`${pattern} = #${center.id} ${center.name}`" in source
    # The user's own rules stay above the generated block (and still win),
    # and the block is replaced, never stacked.
    assert "RULES_BEGIN" in source and "RULES_END" in source
    merge = source.split("function mergeRules(")[1].split("\n  }")[0]
    assert "text.slice(0, start) + text.slice(end + RULES_END.length)" in merge
    assert r"`${own}\n\n${block}`" in merge
    # Delivered to Auto-Build on the same page and through its settings key.
    assert '"fra-autobuild-rules"' in source
    assert 'const AUTOBUILD_SETTINGS_KEY = "fra_autobuild_settings";' in source
