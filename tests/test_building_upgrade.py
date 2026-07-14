"""Alliance hospital/prison level + extension upgrades: detail parsing and
the preview / live flow (network mocked)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.mc.parsers.building_detail import (
    parse_alliance_building_kinds,
    parse_csrf_token,
    parse_current_level,
    parse_extension_offers,
)
from fra_bot.services.building_upgrade import BuildingUpgradeService


# -- detail parsing ---------------------------------------------------------

def test_parse_level_and_csrf():
    html = (
        '<meta name="csrf-token" content="abc123">'
        "<dl><dt><strong>Level:</strong></dt><dd> 7 </dd></dl>"
    )
    assert parse_csrf_token(html) == "abc123"
    assert parse_current_level(html) == 7
    assert parse_current_level("<p>no level here</p>") is None


def test_parse_extension_offers_sorted_and_priced():
    html = """
    <a href="/buildings/42/extension/credits/3">Ward (50,000 Credits)</a>
    <a href="/buildings/42/extension/credits/1">ICU (10.000 Credits)</a>
    <a href="/buildings/42/extension/credits/9">Large Hospital (200,000 Credits)</a>
    <a href="/buildings/99/extension/credits/5">other building</a>
    """
    offers = parse_extension_offers(html, 42)
    assert [o.ext_id for o in offers] == [1, 3, 9]     # sorted, other building ignored
    assert offers[0].price == 10000 and offers[1].price == 50000
    assert offers[2].ext_id == 9 and offers[2].price == 200000


def test_parse_price_ignores_leading_counts():
    # A count before the price must never be mistaken FOR the price — that
    # would understate the funds check and bypass the large-prison guard.
    html = '<a href="/buildings/42/extension/credits/4">2 Cells — 100,000 Credits</a>'
    offers = parse_extension_offers(html, 42)
    assert offers[0].price == 100_000


def test_parse_extension_offers_skips_disabled():
    # An academy shows all three "Additional classroom" extensions at once, but
    # the next-in-chain ones are `disabled` until the previous is built. Buying
    # a disabled one would be rejected, so only the enabled offer is returned.
    html = """
    <a class="btn btn-success " href="/buildings/55/extension/credits/0">400,000 Credits</a>
    <a class="btn btn-success disabled" href="/buildings/55/extension/credits/1">400,000 Credits</a>
    <a class="btn btn-success disabled" href="/buildings/55/extension/credits/2">400,000 Credits</a>
    """
    offers = parse_extension_offers(html, 55)
    assert [o.ext_id for o in offers] == [0]


def test_classify_alliance_buildings():
    html = """
    <table>
      <tr search_attribute="Alliance Hospital"><td>
        <a href="/buildings/10">Alliance Hospital</a></td></tr>
      <tr search_attribute="County Prison"><td>
        <img building_id="20" alt="prison"/></td></tr>
      <tr search_attribute="Fire Academy"><td>
        <a href="/buildings/30">Fire Academy</a></td></tr>
    </table>
    """
    rows = parse_alliance_building_kinds(html)
    kinds = {r.building_id: r.kind for r in rows}
    assert kinds == {10: "hospital", 20: "prison", 30: None}


# -- service flow -----------------------------------------------------------

ALLIANCE_LIST = """
<table>
  <tr search_attribute="Alliance Hospital"><td><a href="/buildings/10">Alliance Hospital</a></td></tr>
  <tr search_attribute="County Prison"><td><a href="/buildings/20">County Prison</a></td></tr>
</table>
"""

HOSPITAL_PAGE = (
    '<meta name="csrf-token" content="tok">'
    "<dt><strong>Level:</strong></dt><dd>5</dd>"
    '<a href="/buildings/10/extension/credits/1">Ward (10,000 Credits)</a>'
    '<a href="/buildings/10/extension/credits/9">Large Hospital (200,000 Credits)</a>'
)
HOSPITAL_PAGE_MAXED = (
    '<meta name="csrf-token" content="tok">'
    "<dt><strong>Level:</strong></dt><dd>20</dd>"
    '<a href="/buildings/10/extension/credits/1">Ward (10,000 Credits)</a>'
    '<a href="/buildings/10/extension/credits/9">Large Hospital (200,000 Credits)</a>'
)
HOSPITAL_PAGE_DONE = (
    '<meta name="csrf-token" content="tok">'
    "<dt><strong>Level:</strong></dt><dd>20</dd>"
    '<a href="/buildings/10/extension/credits/9">Large Hospital (200,000 Credits)</a>'
)
PRISON_PAGE = (
    '<meta name="csrf-token" content="tok">'
    '<a href="/buildings/20/extension/credits/2">Cell block (50,000 Credits)</a>'
    '<a href="/buildings/20/extension/credits/30">Large Prison (200,000 Credits)</a>'
)
PRISON_PAGE_DONE = (
    '<meta name="csrf-token" content="tok">'
    '<a href="/buildings/20/extension/credits/30">Large Prison (200,000 Credits)</a>'
)
FUNDS_HTML = "<div>Alliance Funds: 5,000,000 Credits</div>"


class FakeClient:
    """Serves canned pages; records POSTs and GET 'action' calls. For the
    live path, once an extension/level action is 'done' it swaps in the
    follow-up page so the re-fetch shows the chain advancing."""

    def __init__(self, *, funds=FUNDS_HTML, hospital=HOSPITAL_PAGE, prison=PRISON_PAGE):
        self._funds = funds
        self._hospital = hospital
        self._prison = prison
        self.posts: list[str] = []
        self.gets: list[str] = []

    def url(self, path):
        return path

    async def fetch_page(self, path, *, referer=None):
        if path.startswith("/verband/kasse"):
            return self._funds
        if "/verband/gebauede" in path:
            return ALLIANCE_LIST
        if "expand_do/credits" in path:
            self.gets.append(path)
            self._hospital = HOSPITAL_PAGE_MAXED   # level now maxed
            return "<html>ok</html>"
        if path == "/buildings/10":
            return self._hospital
        if path == "/buildings/20":
            return self._prison
        return "<html></html>"

    async def post_form(self, path, data, **kwargs):
        self.posts.append(path)
        # After buying the small prison extension, only the large one remains.
        if path == "/buildings/20/extension/credits/2":
            self._prison = PRISON_PAGE_DONE
        if path == "/buildings/10/extension/credits/1":
            self._hospital = HOSPITAL_PAGE_DONE
        return (200, "", "")


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "up.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _svc(db, client, *, dry_run=True, min_funds=2_000_000):
    from fra_bot.db.repos import RunsRepo
    svc = BuildingUpgradeService.__new__(BuildingUpgradeService)
    svc.cfg = SimpleNamespace(automation=SimpleNamespace(dry_run=dry_run))
    svc.client = client
    svc._auto = SimpleNamespace(min_alliance_funds=min_funds)
    svc.runs = RunsRepo(db)
    svc._max_level = 20
    return svc


async def test_preview_reports_without_writing(db):
    client = FakeClient()
    svc = _svc(db, client)
    report = await svc.upgrade_all(execute=False)
    assert report.mode == "PREVIEW"
    assert report.buildings_seen == 2
    assert report.levels_raised == 1                 # hospital 5 -> 20
    assert report.extensions_bought == 2             # ext 1 (hosp) + ext 2 (prison), skipping large
    assert client.posts == [] and client.gets == []  # preview writes nothing
    # Intended buys are the small extensions (1, 2) — the large ones (9, 30) skipped.
    joined = "\n".join(report.lines)
    assert "[1]" in joined and "[2]" in joined


async def test_live_raises_level_and_buys_except_large(db):
    client = FakeClient()
    svc = _svc(db, client, dry_run=False)  # note: execute drives it regardless
    report = await svc.upgrade_all(execute=True)
    # Hospital: one level GET + one extension POST (ext 1); large (9) skipped.
    assert any("expand_do/credits?level=19" in g for g in client.gets)
    assert "/buildings/10/extension/credits/1" in client.posts
    assert "/buildings/10/extension/credits/9" not in client.posts
    # Prison: buys ext 2, skips large (30).
    assert "/buildings/20/extension/credits/2" in client.posts
    assert "/buildings/20/extension/credits/30" not in client.posts
    assert report.levels_raised == 1
    assert report.extensions_bought == 2
    assert report.errors == 0


async def test_live_stops_when_funds_below_floor(db):
    client = FakeClient(funds="<div>Alliance Funds: 1,000,000 Credits</div>")
    svc = _svc(db, client, dry_run=False, min_funds=2_000_000)
    report = await svc.upgrade_all(execute=True)
    assert report.funds_blocked is True
    assert client.posts == []           # nothing bought below the floor
    assert client.gets == []            # not even the level GET


async def test_action_cap_truncates(db):
    client = FakeClient()
    svc = _svc(db, client, dry_run=False)
    report = await svc.upgrade_all(execute=True, max_actions=1)
    assert report.actions == 1
    assert report.truncated is True


async def test_level_upgrade_verified_against_refetched_page(db):
    # The happy path must report the REAL before/after levels.
    client = FakeClient()
    svc = _svc(db, client, dry_run=False)
    report = await svc.upgrade_all(execute=True)
    assert report.levels_raised == 1
    assert any("raised level 5 → 20" in line for line in report.lines)


async def test_level_upgrade_that_does_not_stick_is_reported(db):
    # MissionChief answers a refused upgrade with a 200 re-render; the level
    # is verified on the re-fetch, so an unchanged level must NOT count.
    client = FakeClient()
    orig_fetch = client.fetch_page

    async def fetch(path, *, referer=None):
        if "expand_do/credits" in path:
            client.gets.append(path)
            return "<html>looks ok but nothing happened</html>"  # no page swap
        return await orig_fetch(path, referer=referer)

    client.fetch_page = fetch
    svc = _svc(db, client, dry_run=False)
    report = await svc.upgrade_all(execute=True)
    assert report.levels_raised == 0                    # did not take
    assert any("did not take" in line for line in report.lines)


async def test_upgrade_one_levels_repeatedly_to_max(db):
    """Post-creation: upgrade_one raises the level step by step until the
    maximum, then buys the eligible extensions (never the large one)."""

    class _SteppingClient(FakeClient):
        """Each level GET advances the page by ONE level (real behaviour),
        instead of jumping straight to max."""

        def __init__(self):
            super().__init__()
            self.level = 17

        async def fetch_page(self, path, *, referer=None):
            if "expand_do/credits" in path:
                self.gets.append(path)
                self.level += 1
                return "<html>ok</html>"
            if path == "/buildings/10":
                page = (
                    '<meta name="csrf-token" content="tok">'
                    f"<dt><strong>Level:</strong></dt><dd>{self.level}</dd>"
                )
                if "/buildings/10/extension/credits/1" not in self.posts:
                    page += '<a href="/buildings/10/extension/credits/1">Ward (10,000 Credits)</a>'
                page += '<a href="/buildings/10/extension/credits/9">Large Hospital (200,000 Credits)</a>'
                return page
            return await super().fetch_page(path, referer=referer)

    client = _SteppingClient()
    svc = _svc(db, client, dry_run=False)
    report = await svc.upgrade_one(10, kind="hospital", name="New Hospital")
    assert report.levels_raised == 3            # 17 -> 18 -> 19 -> 20 (max)
    assert report.extensions_bought == 1        # ward bought, large skipped
    assert not any("credits/9" in p for p in client.posts)
    assert report.errors == 0


async def test_upgrade_one_stops_leveling_after_refused_raise(db):
    """A raise that 'did not take' (funds/refused, 200 re-render) must stop
    further level attempts instead of burning the action budget."""

    class _StuckClient(FakeClient):
        async def fetch_page(self, path, *, referer=None):
            if "expand_do/credits" in path:
                self.gets.append(path)
                return "<html>refused</html>"    # page level never moves
            if path == "/buildings/10":
                return (
                    '<meta name="csrf-token" content="tok">'
                    "<dt><strong>Level:</strong></dt><dd>5</dd>"
                    '<a href="/buildings/10/extension/credits/9">Large Hospital (200,000 Credits)</a>'
                )
            return await super().fetch_page(path, referer=referer)

    client = _StuckClient()
    svc = _svc(db, client, dry_run=False)
    report = await svc.upgrade_one(10, kind="hospital")
    assert len(client.gets) == 1                # exactly one attempt
    assert report.levels_raised == 0 and report.errors == 1


async def test_upgrade_one_ignores_the_funds_floor(db):
    """Explicit policy: a fresh build is completed even below the floor —
    the floor gates new REQUESTS, not the completion of an approved one."""
    client = FakeClient(funds="<div>Alliance Funds: 100,000 Credits</div>")
    svc = _svc(db, client, dry_run=False, min_funds=2_000_000)
    report = await svc.upgrade_one(10, kind="hospital", name="Fresh Hospital")
    assert not report.funds_blocked
    assert report.levels_raised == 1            # 5 -> 20 jump page fixture
    assert report.extensions_bought == 1        # ward bought despite low funds
    assert not any("credits/9" in p for p in client.posts)  # large still skipped
