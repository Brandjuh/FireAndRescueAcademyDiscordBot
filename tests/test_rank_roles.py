"""Credit rank roles: the ladder, promotion detection, baseline
suppression, role reconciliation and departure cleanup."""

import json
from types import SimpleNamespace

import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.services.rank_roles import (
    CREDIT_RANKS,
    DEFAULT_RANK_ROLE_IDS,
    RankRolesService,
    STATE_BASELINE,
    is_promotion,
    promotion_text,
    rank_for_credits,
)
from fra_bot.db.repos import StateRepo


# ---------------------------------------------------------------------------
# Pure ladder logic
# ---------------------------------------------------------------------------

def test_rank_ladder_boundaries():
    assert rank_for_credits(0).key == "probie"
    assert rank_for_credits(199).key == "probie"
    assert rank_for_credits(200).key == "firefighter"
    assert rank_for_credits(9_999).key == "firefighter"
    assert rank_for_credits(10_000).key == "senior_firefighter"
    assert rank_for_credits(999_999).key == "fire_apparatus_operator"
    assert rank_for_credits(1_000_000).key == "lieutenant"
    assert rank_for_credits(10_000_000_000).key == "fire_commissioner"
    assert rank_for_credits(99_999_999_999).key == "fire_commissioner"


def test_promotion_detection():
    assert is_promotion("probie", "firefighter") is True
    assert is_promotion("firefighter", "probie") is False       # demotion
    assert is_promotion("captain", "captain") is False          # unchanged
    assert is_promotion(None, "captain") is False               # no history
    assert is_promotion("bogus", "captain") is False


def test_promotion_text_matches_the_old_bot():
    member = SimpleNamespace(mention="<@1>")
    assert promotion_text(member, CREDIT_RANKS[1]) == (
        "Congratulations to <@1>.\nPromoted to **Firefighter**."
    )


def test_every_rank_has_a_default_role_id():
    assert set(DEFAULT_RANK_ROLE_IDS) == {r.key for r in CREDIT_RANKS}


# ---------------------------------------------------------------------------
# Sync with fakes
# ---------------------------------------------------------------------------

class FakeRole:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


class FakeMember:
    def __init__(self, discord_id, roles=()):
        self.id = discord_id
        self.bot = False
        self.roles = list(roles)
        self.mention = f"<@{discord_id}>"

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(r for r in roles if r not in self.roles)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, roles, members):
        self._roles = {r.id: r for r in roles}
        self._members = {m.id: m for m in members}

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, member_id):
        return self._members.get(member_id)


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class FakeBot:
    def __init__(self, guild, channel):
        self._guild = guild
        self._channel = channel

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return self._channel if channel_id == self._channel.id else None


def _cfg(**overrides):
    auto = SimpleNamespace(
        enabled=True, interval=60, promotion_channel_id=700,
        announce_first_assignment=False, role_ids={},
    )
    for key, value in overrides.items():
        setattr(auto, key, value)
    return SimpleNamespace(
        discord=SimpleNamespace(guild_id=1, verified_role_id=600),
        automation=SimpleNamespace(rank_roles=auto),
    )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "ranks.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def _seed(db, mc_id, name, credits, *, active=1, discord_id=None):
    await db.execute(
        "INSERT INTO members (mc_user_id, name, earned_credits, is_active, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, '2026-01-01', '2026-07-01')",
        (mc_id, name, credits, active),
    )
    if discord_id is not None:
        await db.execute(
            "INSERT INTO member_links (discord_id, mc_user_id, status, "
            "reviewer_id, created_at, updated_at) VALUES (?, ?, 'approved', 0, "
            "'2026-01-01', '2026-01-01')",
            (discord_id, mc_id),
        )


def _rig(db, cfg=None, *, member_roles=()):
    verified = FakeRole(600, "Verified")
    rank_roles = {
        key: FakeRole(role_id, key)
        for key, role_id in DEFAULT_RANK_ROLE_IDS.items()
    }
    member = FakeMember(100, roles=[verified, *member_roles])
    guild = FakeGuild([verified, *rank_roles.values()], [member])
    channel = FakeChannel(700)
    service = RankRolesService(cfg or _cfg(), db, FakeBot(guild, channel))
    service.edit_delay = 0
    return service, member, channel, rank_roles


async def _healthy_roster(db):
    for i in range(120):
        await _seed(db, 5000 + i, f"Filler{i}", 0)


async def test_first_sync_assigns_ranks_without_congratulating(db):
    await _healthy_roster(db)
    await _seed(db, 42, "Alice", 15_000, discord_id=100)
    service, member, channel, ranks = _rig(db)
    summary = await service.sync()
    assert summary["updated"] == 1 and summary["promotions"] == 0
    assert ranks["senior_firefighter"] in member.roles
    assert channel.sent == []  # baseline run: no congratulations spam


async def test_promotion_is_announced_after_baseline(db):
    await _healthy_roster(db)
    await _seed(db, 42, "Alice", 15_000, discord_id=100)
    service, member, channel, ranks = _rig(db)
    await service.sync()  # baseline
    await db.execute(
        "UPDATE members SET earned_credits = ? WHERE mc_user_id = 42",
        (2_000_000,),
    )
    summary = await service.sync()
    assert summary["promotions"] == 1
    assert ranks["lieutenant"] in member.roles
    assert ranks["senior_firefighter"] not in member.roles  # old rank removed
    assert channel.sent == [
        "Congratulations to <@100>.\nPromoted to **Lieutenant**."
    ]


async def test_unverified_member_gets_role_but_no_announcement(db):
    await _healthy_roster(db)
    await _seed(db, 42, "Alice", 300, discord_id=100)
    service, member, channel, ranks = _rig(db)
    member.roles = []  # no verified role
    await service.sync()  # baseline
    await db.execute(
        "UPDATE members SET earned_credits = ? WHERE mc_user_id = 42",
        (20_000,),
    )
    summary = await service.sync()
    assert ranks["senior_firefighter"] in member.roles  # role still follows
    assert summary["promotions"] == 0 and channel.sent == []


async def test_departed_member_loses_rank_roles(db):
    await _healthy_roster(db)
    await _seed(db, 42, "Alice", 15_000, discord_id=100)
    service, member, channel, ranks = _rig(db)
    await service.sync()
    assert ranks["senior_firefighter"] in member.roles
    await db.execute("UPDATE members SET is_active = 0 WHERE mc_user_id = 42")
    summary = await service.sync()
    assert summary["departures"] == 1
    assert all(r not in member.roles for r in ranks.values())


async def test_dry_run_previews_without_touching_roles(db):
    await _healthy_roster(db)
    await _seed(db, 42, "Alice", 15_000, discord_id=100)
    service, member, channel, ranks = _rig(db)
    summary = await service.sync(dry_run=True)
    assert summary["updated"] == 1
    assert all(r not in member.roles for r in ranks.values())
    # Dry-run must not establish the baseline either.
    assert await StateRepo(db).get(STATE_BASELINE) is None


async def test_unhealthy_roster_defers_the_whole_sync(db):
    await _seed(db, 42, "Alice", 15_000, discord_id=100)  # tiny roster
    service, member, channel, ranks = _rig(db)
    summary = await service.sync()
    assert summary["error"] and "unhealthy" in summary["error"]
    assert all(r not in member.roles for r in ranks.values())
