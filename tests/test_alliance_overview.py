"""`!fra alliance` + the alliance cards that went public with it."""

import datetime as dt
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

from fra_bot.db.database import Database

from fra_bot.services.alliance_overview import (
    overview_embed,
    short_number,
    summarize_roster,
)

NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.timezone.utc)


def _member(credits, rate, first_seen="2026-07-07T15:25:55+00:00"):
    return {"earned_credits": credits, "contribution_rate": rate,
            "first_seen_at": first_seen, "left_at": None}


def test_roster_totals():
    active = [
        _member(1_000, 5.0),
        _member(3_000, None, first_seen="2026-11-01T10:00:00+00:00"),  # new, 0%
        _member(None, 10.0),  # credits not swept yet: left out of the sums
    ]
    former = [
        {"left_at": "2026-10-20T08:00:00+00:00"},   # within 30 days
        {"left_at": "2026-08-01T08:00:00+00:00"},   # too long ago
        {"left_at": None},
    ]
    r = summarize_roster(active, former, now=NOW)
    assert r.members == 3
    assert r.credits_total == 4_000
    assert r.credits_median == 2_000
    assert r.contributing == 2
    assert r.avg_contribution == pytest.approx(5.0)   # (5 + 0 + 10) / 3
    assert (r.joined_30d, r.left_30d) == (1, 1)


def test_empty_roster_does_not_crash():
    r = summarize_roster([], [], now=NOW)
    assert r.members == 0 and r.credits_median is None and r.avg_contribution is None


@pytest.mark.parametrize("value,text", [
    (28_580_945_816, "28.6B"), (4_641, "4.6K"), (999, "999"), (1_500_000, "1.5M"),
])
def test_short_number(value, text):
    assert short_number(value) == text


def _embed(**overrides):
    roster = summarize_roster([_member(28_580_945_816, 5.0)], [], now=NOW)
    kwargs = dict(
        synced_members=5, building_total=4_641, vehicle_total=15_050,
        top_buildings=[("fire station", 1_203), ("hospital", 40)],
        top_vehicles=[("type 0", 900)], date_label="06 Nov 2026",
    )
    kwargs.update(overrides)
    return overview_embed(roster, **kwargs)


def test_overview_says_how_many_members_the_game_data_covers():
    embed = _embed()
    names = [f.name for f in embed.fields]
    assert "🎮 Game data — synced by 5 of 1 members" in names
    text = " ".join(f.value for f in embed.fields)
    assert "4,641" in text and "15,050" in text and "28.6B" in text
    assert "1,203× Fire station" in text
    assert len(embed) < 6000


def test_overview_without_any_sync_points_to_the_userscript():
    embed = _embed(synced_members=0, building_total=0, vehicle_total=0,
                   top_buildings=[], top_vehicles=[])
    game = [f for f in embed.fields if f.name.startswith("🎮")]
    assert len(game) == 1 and "Sync to FRA" in game[0].value


# -- the cards are public now ----------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "overview.sqlite3")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def bot():
    from fra_bot.cogs.game_sync import GameSyncCog

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.db = None
    bot.cfg = SimpleNamespace(discord=SimpleNamespace(admin_role_ids=(999,)))
    await bot.add_cog(GameSyncCog(bot))
    yield bot
    await bot.close()


@pytest.mark.parametrize("name", ["hotspots", "infographic", "fleet"])
async def test_alliance_cards_are_open_to_members(bot, name):
    member = SimpleNamespace(
        bot=bot, command=None, guild=object(),
        author=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False),
                               roles=[]),
    )
    command = bot.get_command(name)
    assert await command.can_run(member) is True
    # ... but rate-limited: one render per user per minute, one at a time
    assert command._buckets._cooldown.per == 60
    assert command._max_concurrency is not None


async def test_overview_end_to_end_from_the_database(db):
    """GameSyncCog.alliance_overview against a real schema (roster + one
    synced member)."""
    import json

    from fra_bot.cogs.game_sync import GameSyncCog

    await db.execute(
        "INSERT INTO members (mc_user_id, name, earned_credits, contribution_rate, "
        "first_seen_at, last_seen_at, is_active) VALUES "
        "(1, 'Alice', 5000, 5, '2026-07-07T00:00:00+00:00', '2026-11-06', 1),"
        "(2, 'Bob', 7000, 0, '2026-07-07T00:00:00+00:00', '2026-11-06', 1)"
    )
    cog = GameSyncCog(SimpleNamespace(db=db))

    async def no_names():
        return {}
    cog._vehicle_names = no_names
    await cog.repo.upsert(
        mc_user_id=1, discord_user_id=None, mc_name="Alice",
        building_count=3, vehicle_count=7,
        buildings_json=json.dumps({"by_type": {"0": 3}, "coords": [[1.0, 2.0]]}),
        vehicles_json=json.dumps({"by_type": {"0": 7}}),
    )
    embed = await cog.alliance_overview()
    text = " ".join(f"{f.name} {f.value}" for f in embed.fields)
    assert "**2** active" in text
    assert "12.0K" in text            # 5000 + 7000 credits
    assert "synced by 1 of 2 members" in text
    assert "**3** buildings" in text and "**7** vehicles" in text
