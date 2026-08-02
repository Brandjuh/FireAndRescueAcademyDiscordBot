"""Sanction wizard building blocks, panel wiring, review button set, and
the register statistics that feed the Statistics button."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.panels import PanelKeeperCog
from fra_bot.cogs.sanctions import (
    SANCTION_TYPE_KEYS,
    _review_view,
    repeat_banner,
    type_options,
)
from fra_bot.db.database import Database
from fra_bot.db.repos import SanctionsRepo

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "wizard.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_type_options_put_the_coc_advice_first():
    options = type_options("1.6")
    assert options[0].value == "Warning - Official 1st warning"
    assert options[0].label.endswith("(advised)")
    assert len(options) == len(SANCTION_TYPE_KEYS)
    # Free-text reason: plain catalogue order, nothing marked.
    plain = type_options(None)
    assert len(plain) == len(SANCTION_TYPE_KEYS)
    assert not any(o.label.endswith("(advised)") for o in plain)


async def test_repeat_banner_fires_on_three_same_rule_offenses():
    def row(status="active", category="1.4"):
        return {"status": status, "reason_category": category}

    rows = [row(), row(), row()]
    assert "1.4" in repeat_banner(rows, "1.4")
    assert repeat_banner(rows[:2], "1.4") is None
    # Settled records and other rules don't count; free text never fires.
    assert repeat_banner(
        [row("revoked"), row(), row(category="2.1")], "1.4"
    ) is None
    assert repeat_banner(rows, None) is None


async def test_review_view_carries_the_full_button_set():
    view = _review_view(7)
    ids = [item.custom_id for item in view.children]
    assert ids == [
        "fra:sreview:confirm:7",
        "fra:sreview:edit:type:7",
        "fra:sreview:edit:reason:7",
        "fra:sreview:edit:notes:7",
        "fra:sreview:dismiss:7",
    ]
    # Settled records get edit-only (no Approve/Dismiss).
    edit_only = _review_view(7, include_resolution=False)
    assert [item.custom_id for item in edit_only.children] == [
        "fra:sreview:edit:type:7",
        "fra:sreview:edit:reason:7",
        "fra:sreview:edit:notes:7",
    ]


async def test_panel_keeper_registry_includes_the_sanction_panel():
    keeper = PanelKeeperCog.__new__(PanelKeeperCog)
    keeper.bot = SimpleNamespace(cfg=SimpleNamespace(
        automation=SimpleNamespace(
            mission=SimpleNamespace(panel_channel_id=0),
        ),
        discord=SimpleNamespace(
            channels=SimpleNamespace(sanction_panel=777),
        ),
    ))
    specs = {spec.key: spec for spec in PanelKeeperCog._specs(keeper)}
    assert "sanctions" in specs
    assert specs["sanctions"].cog_name == "SanctionsCog"
    assert specs["sanctions"].channel_id() == 777


async def test_register_statistics(db):
    repo = SanctionsRepo(db)
    for name, admin in (("Alice", "Boss"), ("Alice", "Boss"), ("Bob", "Chief")):
        await repo.add(
            mc_user_id=hash(name) % 1000, mc_username=name,
            discord_user_id=None, admin_discord_id=1, admin_name=admin,
            sanction_type="Warning - Official 1st warning", reason="t",
        )
    await repo.add(
        mc_user_id=1, mc_username="Alice", discord_user_id=None,
        admin_discord_id=0, admin_name="FRA Bot (tax warnings)",
        sanction_type="Kick", reason="t", source="tax",
    )
    summary = await repo.status_summary()
    assert summary == {"active": 4}
    admins = await repo.admin_leaderboard()
    # The tax bot is automation — humans only on the leaderboard.
    assert [(r["admin_name"], r["n"]) for r in admins] == [
        ("Boss", 2), ("Chief", 1),
    ]
    members = await repo.member_leaderboard()
    assert members[0]["name"] == "Alice" and members[0]["n"] == 3
