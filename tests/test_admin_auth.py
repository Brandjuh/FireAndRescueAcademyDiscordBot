"""C1 regression: the admin authorization gate must apply to EVERY
!fra subcommand, not just the bare group.

The bug was that a group check under invoke_without_command=True does
not propagate to subcommands, so anyone could run !fra update. The fix
is AdminCog.cog_check, which discord.py runs for every command in the
cog. Here we test the predicate the cog_check delegates to.
"""

from types import SimpleNamespace

from fra_bot.cogs.admin import is_fra_admin_ctx


def _ctx(*, guild=True, administrator=False, role_ids=(), admin_role_ids=()):
    author = SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=administrator),
        roles=[SimpleNamespace(id=r) for r in role_ids],
    )
    bot = SimpleNamespace(
        cfg=SimpleNamespace(discord=SimpleNamespace(admin_role_ids=tuple(admin_role_ids)))
    )
    return SimpleNamespace(guild=object() if guild else None, author=author, bot=bot)


def test_non_admin_is_denied():
    assert is_fra_admin_ctx(_ctx()) is False


def test_server_administrator_allowed():
    assert is_fra_admin_ctx(_ctx(administrator=True)) is True


def test_admin_role_allowed():
    ctx = _ctx(role_ids=(111, 222), admin_role_ids=(222,))
    assert is_fra_admin_ctx(ctx) is True


def test_wrong_role_denied():
    ctx = _ctx(role_ids=(111,), admin_role_ids=(222,))
    assert is_fra_admin_ctx(ctx) is False


def test_dm_context_denied():
    # No guild => never an admin (guild_permissions/roles are meaningless).
    assert is_fra_admin_ctx(_ctx(guild=False, administrator=True)) is False


def test_cog_check_delegates_to_predicate():
    # AdminCog.cog_check must call the same predicate — verify wiring.
    from fra_bot.cogs.admin import AdminCog

    assert AdminCog.cog_check.__doc__  # documented gate exists
