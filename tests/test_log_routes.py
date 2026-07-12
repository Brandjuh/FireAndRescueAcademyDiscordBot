"""Per-action-key alliance-log routing: the pure route map + groups, the
repo's two feeds (posted/routed) and the mirror publisher."""

import json
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio

import fra_bot.cogs.notifications as notifications
from fra_bot.cogs.notifications import NotificationsCog, _ROUTE_SEND_BUDGET
from fra_bot.core import log_routes as lr
from fra_bot.db.database import Database
from fra_bot.db.repos import LogsRepo, StateRepo


@pytest.fixture(autouse=True)
def _no_send_pause(monkeypatch):
    # The publisher paces real sends at 1.2s; tests must not sleep.
    monkeypatch.setattr(notifications, "_POST_PAUSE_SECONDS", 0)


# ---------------------------------------------------------------------------
# Pure route map + group logic
# ---------------------------------------------------------------------------

def test_groups_reference_only_known_action_keys():
    # The startup drift guard must have nothing to report.
    assert lr.group_drift() == {}


def test_valid_targets_covers_keys_groups_all_unknown():
    targets = lr.valid_targets()
    assert "building_constructed" in targets   # exact key
    assert "building" in targets               # group alias
    assert "all" in targets and "unknown" in targets


def test_normalize_target_folds_case_dashes_and_hash():
    assert lr.normalize_target("Building_Constructed") == "building_constructed"
    assert lr.normalize_target("BUILDING") == "building"
    assert lr.normalize_target("all") == "all"
    assert lr.normalize_target("nonsense") is None
    assert lr.normalize_target("buildings") is None  # plural typo rejected


def test_channels_for_dedups_by_channel_and_expands_groups():
    routes = {100: ["building", "building_constructed"], 200: ["all"]}
    # 100 subscribes via BOTH the group and the exact key — still once.
    assert sorted(lr.channels_for(routes, "building_constructed")) == [100, 200]
    # A building key not otherwise subscribed still reaches the group channel.
    assert lr.channels_for(routes, "building_destroyed") == [100, 200]
    # A non-building key only reaches the 'all' channel.
    assert lr.channels_for(routes, "left_alliance") == [200]


def test_channels_for_excludes_the_main_channel():
    routes = {100: ["all"], 200: ["building"]}
    assert lr.channels_for(routes, "building_constructed", exclude=100) == [200]


def test_all_catches_unknown_but_groups_do_not():
    routes = {100: ["all"], 200: ["building"]}
    assert lr.channels_for(routes, "unknown") == [100]


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "routes.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_add_remove_clear_round_trip(db):
    state = StateRepo(db)
    await lr.add(state, 100, ["building", "left_alliance"])
    await lr.add(state, 100, ["building"])  # dup ignored
    assert (await lr.load(state))[100] == ["building", "left_alliance"]

    assert await lr.remove(state, 100, "left_alliance") is True
    assert (await lr.load(state))[100] == ["building"]
    # Removing the last target drops the channel key entirely.
    assert await lr.remove(state, 100, "building") is True
    assert 100 not in await lr.load(state)

    await lr.add(state, 200, ["all"])
    assert await lr.remove(state, 200, None) is True  # whole channel
    assert await lr.load(state) == {}


async def test_add_normalizes_and_skips_invalid(db):
    state = StateRepo(db)
    targets = await lr.add(state, 100, ["Building", "bogus", "left-alliance"])
    assert targets == ["building", "left_alliance"]  # normalized, bogus dropped


async def test_load_survives_malformed_state(db):
    state = StateRepo(db)
    await state.set(lr.STATE_KEY, "not json at all")
    assert await lr.load(state) == {}
    await state.set(lr.STATE_KEY, json.dumps({"nan": ["building"], "100": "notalist"}))
    assert await lr.load(state) == {}  # bad channel id + non-list both dropped


# ---------------------------------------------------------------------------
# Repo: the two independent feeds
# ---------------------------------------------------------------------------

def _log(sig, action="building_constructed"):
    return {"raw_timestamp": "t", "event_at": None, "action_key": action,
            "description": "d", "signature": sig}


async def test_history_backfill_marks_both_feeds(db):
    logs = LogsRepo(db)
    await logs.insert_batch([_log("s1")], mark_posted=True)
    # Suppressed from BOTH the main feed and the route feed.
    assert await logs.pending_posts() == []
    assert await logs.pending_routes() == []


async def test_route_feed_gates_on_posted(db):
    logs = LogsRepo(db)
    await logs.insert_batch([_log("s1")], mark_posted=False)
    assert await logs.pending_routes() == []          # not posted yet
    row = (await logs.pending_posts())[0]
    await logs.mark_posted(row["id"])
    assert len(await logs.pending_routes()) == 1        # now eligible
    await logs.mark_routed(row["id"])
    assert await logs.pending_routes() == []


async def test_mark_all_posted_suppresses_route_feed(db):
    logs = LogsRepo(db)
    await logs.insert_batch([_log("s1")], mark_posted=False)
    await logs.mark_all_posted()
    # First-sync suppression must cover the route feed, else configuring a
    # route replays all of history into it.
    assert await logs.pending_routes() == []


# ---------------------------------------------------------------------------
# The mirror publisher
# ---------------------------------------------------------------------------

class FakeChannel:
    def __init__(self, channel_id, fail=None):
        self.id = channel_id
        self.sent = []
        self._fail = fail  # an exception to raise on send

    async def send(self, embed=None, **kwargs):
        if self._fail is not None:
            raise self._fail
        self.sent.append(embed)


class FakeBot:
    def __init__(self, db, channels, main_id):
        self.db = db
        self._channels = {c.id: c for c in channels}
        self._main_id = main_id

    def channel_for(self, key):
        return self._channels.get(self._main_id) if key == "alliance_logs" else None

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


def _cog(bot):
    cog = NotificationsCog.__new__(NotificationsCog)
    cog.bot = bot
    cog._logs = LogsRepo(bot.db)
    cog._state = StateRepo(bot.db)
    return cog


async def _seed_posted(db, sig, action="building_constructed"):
    logs = LogsRepo(db)
    await logs.insert_batch([_log(sig, action)], mark_posted=False)
    row = (await logs.pending_posts())[-1]
    await logs.mark_posted(row["id"])
    return row["id"]


async def test_mirror_sends_to_routed_channel_and_marks_routed(db):
    await _seed_posted(db, "s1", "building_constructed")
    main, route = FakeChannel(1), FakeChannel(2)
    bot = FakeBot(db, [main, route], main_id=1)
    await lr.add(StateRepo(db), 2, ["building"])
    cog = _cog(bot)

    await cog._publish_log_routes()
    assert len(route.sent) == 1                         # mirrored
    assert main.sent == []                              # main feed untouched here
    assert await LogsRepo(db).pending_routes() == []    # marked routed


async def test_mirror_excludes_the_main_channel(db):
    await _seed_posted(db, "s1", "building_constructed")
    main = FakeChannel(1)
    bot = FakeBot(db, [main], main_id=1)
    # Route 'all' at the MAIN channel — must NOT double-post there.
    await lr.add(StateRepo(db), 1, ["all"])
    cog = _cog(bot)
    await cog._publish_log_routes()
    assert main.sent == []
    assert await LogsRepo(db).pending_routes() == []    # still marked routed


async def test_mirror_best_effort_marks_routed_despite_send_error(db):
    await _seed_posted(db, "s1", "building_constructed")
    main = FakeChannel(1)
    boom = FakeChannel(2, fail=discord.HTTPException(
        SimpleNamespace(status=500, reason="err"), "boom"))
    bot = FakeBot(db, [main, boom], main_id=1)
    await lr.add(StateRepo(db), 2, ["building"])
    cog = _cog(bot)
    await cog._publish_log_routes()
    # Transient failure is swallowed; row still marked routed (no retry storm).
    assert await LogsRepo(db).pending_routes() == []


async def test_mirror_unreachable_channel_does_not_wedge_queue(db):
    await _seed_posted(db, "s1", "building_constructed")
    main = FakeChannel(1)
    bot = FakeBot(db, [main], main_id=1)   # channel 2 does not exist
    await lr.add(StateRepo(db), 2, ["building"])
    cog = _cog(bot)
    await cog._publish_log_routes()
    # A deleted route channel resolves to None -> skipped, row marked routed.
    assert await LogsRepo(db).pending_routes() == []


async def test_mirror_fans_one_row_to_several_channels(db):
    await _seed_posted(db, "s1", "building_constructed")
    main, a, b = FakeChannel(1), FakeChannel(2), FakeChannel(3)
    bot = FakeBot(db, [main, a, b], main_id=1)
    state = StateRepo(db)
    await lr.add(state, 2, ["building"])
    await lr.add(state, 3, ["all"])
    cog = _cog(bot)
    await cog._publish_log_routes()
    # Both subscribed channels get exactly one copy; the row is fully drained.
    assert len(a.sent) == 1 and len(b.sent) == 1
    assert await LogsRepo(db).pending_routes() == []


async def test_mirror_send_budget_caps_a_tick(db):
    # A handful of rows each fanned to many channels exceeds the send budget
    # WITHIN the batch fetch, so the budget guard (not the fetch cap) stops it.
    channels = [FakeChannel(cid) for cid in range(2, 2 + _ROUTE_SEND_BUDGET)]
    main = FakeChannel(1)
    bot = FakeBot(db, [main, *channels], main_id=1)
    state = StateRepo(db)
    for c in channels:
        await lr.add(state, c.id, ["all"])          # every channel gets every row
    for i in range(5):
        await _seed_posted(db, f"s{i}", "building_constructed")
    cog = _cog(bot)
    await cog._publish_log_routes()
    total = sum(len(c.sent) for c in channels)
    # Stopped near the budget, not after draining all 5 * N sends.
    assert total <= _ROUTE_SEND_BUDGET + len(channels)
    assert len(await LogsRepo(db).pending_routes()) > 0  # remainder next tick


async def test_mirror_drains_rows_even_without_routes(db):
    # No routes configured: posted rows must still be marked routed, else the
    # backlog piles up and the first route added later floods with history.
    await _seed_posted(db, "s1", "building_constructed")
    main = FakeChannel(1)
    bot = FakeBot(db, [main], main_id=1)
    cog = _cog(bot)
    await cog._publish_log_routes()
    assert main.sent == []                               # nothing mirrored
    assert await LogsRepo(db).pending_routes() == []     # but drained


async def test_mirror_copy_is_the_full_embed(db):
    # The routed copy carries the real embed (title/description), so a content
    # regression in the shared builder is caught.
    logs = LogsRepo(db)
    await logs.insert_batch([{
        "raw_timestamp": "2026-01-01 12:00", "event_at": None,
        "action_key": "contributed_to_alliance", "description": "gave credits",
        "executed_name": "Alice", "executed_mc_id": 42,
        "contribution_amount": 5000, "signature": "c1",
    }], mark_posted=False)
    row = (await logs.pending_posts())[0]
    await logs.mark_posted(row["id"])
    main, route = FakeChannel(1), FakeChannel(2)
    bot = FakeBot(db, [main, route], main_id=1)
    await lr.add(StateRepo(db), 2, ["contributed_to_alliance"])
    cog = _cog(bot)
    await cog._publish_log_routes()
    assert len(route.sent) == 1
    embed = route.sent[0]
    assert "Contributed to the alliance" in embed.title
    assert "Alice" in embed.description
    assert "+5,000" in embed.description
