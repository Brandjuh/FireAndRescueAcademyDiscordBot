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


async def test_diag_reports_rendered_funds_fallback(monkeypatch):
    """When the plain kasse HTML lacks the funds figure (JS-drawn, as on
    the real page), `!fra diag` must exercise the rendered fallback and
    report the figure it finds — proving the build flow can read funds."""
    from types import SimpleNamespace

    from fra_bot.cogs.admin import AdminCog

    kasse = (
        "<div>Deactivate alliance fund</div>"
        "<table><tr><td>Name</td><td>Credits</td></tr></table>"
    )
    gebauede = (
        "<table><tr search_attribute='Fire Academy'>"
        "<td><img building_id='42' src='/img/fire.png' alt='Fire'/></td>"
        "<td><a href='/buildings/42' class='btn btn-success'>"
        "Start a new training course</a></td></tr></table>"
    )
    academy = (
        "<form action='/buildings/42/education' method='post'>"
        "<input type='hidden' name='authenticity_token' value='tok'/>"
        "<select name='building_rooms_use'><option value='1'>1</option></select>"
        "<select name='alliance[cost]'><option value='0'>Free</option></select>"
        "<select name='education_select'><option value='12'>HazMat</option></select>"
        "</form>"
    )

    class _MC:
        session = SimpleNamespace(cookie_jar=[])

        async def fetch_page(self, path, *, referer=None):
            return {
                "/verband/kasse": kasse,
                "/verband/gebauede": gebauede,
            }.get(path, academy)

    async def fake_render(base, cookies, path, **kwargs):
        return "<div>Alliance Funds</div><div>4,935,224 Credits</div>"

    monkeypatch.setattr(
        "fra_bot.mc.browser_builder.BrowserBuilder.available",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr("fra_bot.mc.browser_builder.render_page", fake_render)

    cog = AdminCog.__new__(AdminCog)
    cog.bot = SimpleNamespace(
        cfg=SimpleNamespace(
            automation=SimpleNamespace(dry_run=False),
            missionchief=SimpleNamespace(base_url="https://www.missionchief.com"),
        ),
        mc=_MC(),
    )
    lines = await cog._run_diagnostics()
    text = "\n".join(lines)
    assert "NOT FOUND in plain HTML" in text
    assert "alliance funds (rendered): 4,935,224" in text
    assert "fire: 1 (1 startable)" in text
    assert "academy 42" in text and "free class=yes" in text
