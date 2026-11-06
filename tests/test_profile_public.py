"""``!fra profile``: the one ``!fra`` subcommand every member may run.

Everything else under ``!fra`` must stay admin-only (see
test_admin_auth.py for why the gate lives in ``AdminCog.cog_check``).
These tests go through discord.py's real ``Command.can_run`` so the
cog_check + guild_only wiring is exercised, not just the predicate.
"""

from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from fra_bot.cogs.admin import PUBLIC_FRA_COMMANDS, AdminCog


@pytest.fixture
async def bot():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.db = None
    bot.cfg = SimpleNamespace(discord=SimpleNamespace(admin_role_ids=(999,)))
    await bot.add_cog(AdminCog(bot))
    yield bot
    await bot.close()


def _ctx(bot, *, admin=False, guild=True):
    author = SimpleNamespace(
        id=100, display_name="Alice",
        guild_permissions=SimpleNamespace(administrator=False),
        roles=[SimpleNamespace(id=999)] if admin else [],
    )
    return SimpleNamespace(bot=bot, author=author, command=None,
                           guild=object() if guild else None)


async def test_a_plain_member_may_post_a_profile(bot):
    command = bot.get_command("fra profile")
    assert command is not None
    assert await command.can_run(_ctx(bot)) is True


async def test_the_alias_is_the_same_public_command(bot):
    assert bot.get_command("fra profiel") is bot.get_command("fra profile")


async def test_every_other_fra_command_stays_admin_only(bot):
    fra = bot.get_command("fra")
    member = _ctx(bot)
    checked = 0
    for command in fra.walk_commands():
        if command.qualified_name in PUBLIC_FRA_COMMANDS:
            continue
        try:
            allowed = await command.can_run(member)
        except commands.CheckFailure:
            allowed = False
        assert allowed is False, command.qualified_name
        checked += 1
    assert checked > 30  # the gate really walked the admin commands


async def test_an_admin_still_runs_admin_commands(bot):
    assert await bot.get_command("fra update").can_run(_ctx(bot, admin=True)) is True


async def test_not_in_dms(bot):
    with pytest.raises(commands.NoPrivateMessage):
        await bot.get_command("fra profile").can_run(_ctx(bot, guild=False))


def test_only_the_profile_command_is_public():
    assert PUBLIC_FRA_COMMANDS == frozenset({"fra profile"})


async def test_it_posts_the_profile_card_publicly(bot):
    shown = []

    async def profile_embed(user):
        shown.append(user)
        return discord.Embed(title=f"👤 Profile — {user.display_name}")

    class ProfileCog(commands.Cog):
        pass

    profile_cog = ProfileCog()
    profile_cog.profile_embed = profile_embed
    await bot.add_cog(profile_cog)

    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    ctx = _ctx(bot)
    ctx.send = send
    command = bot.get_command("fra profile")
    cog = bot.get_cog("AdminCog")

    await command.callback(cog, ctx, member=None)
    other = SimpleNamespace(id=200, display_name="Bob")
    await command.callback(cog, ctx, member=other)

    assert shown == [ctx.author, other]
    assert [kw["embed"].title for _, kw in sent] == [
        "👤 Profile — Alice", "👤 Profile — Bob",
    ]
    for _, kwargs in sent:
        assert "ephemeral" not in kwargs  # a plain channel message
        assert kwargs["allowed_mentions"].users is False  # never pings
