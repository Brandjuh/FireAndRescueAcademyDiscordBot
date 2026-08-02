"""Application auto-accept + Accept/Deny buttons (reference: newmembernotify).

Auto-accept accepts new applications in-game and announces the result;
failures (and manual mode) fall back to persistent Accept/Deny buttons so
an application can never go unhandled silently.
"""

import re
from types import SimpleNamespace

import pytest
import pytest_asyncio

from fra_bot.cogs.notifications import (
    ApplicationAcceptButton,
    ApplicationDenyButton,
    NotificationsCog,
    _application_view,
)
from fra_bot.db.database import Database
from fra_bot.db.repos import ApplicationsRepo
from fra_bot.mc.errors import FetchError
from fra_bot.services.applications_sync import ApplicationsSyncService

pytestmark = pytest.mark.asyncio


class FakeClient:
    def __init__(self, fail_paths=()):
        self.fetched = []
        self.fail_paths = tuple(fail_paths)

    def url(self, path):
        return "https://www.missionchief.com/" + path.lstrip("/")

    async def fetch_page(self, path, *, referer=None, ajax=False):
        self.fetched.append(path)
        for prefix in self.fail_paths:
            if path.startswith(prefix):
                raise FetchError(path, 500)
        return "<html></html>"


class FakeChannel:
    def __init__(self):
        self.sent = []  # (embed, view)

    async def send(self, embed=None, view=None, **kwargs):
        self.sent.append((embed, view))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "apps.sqlite3")
    await database.connect()
    yield database
    await database.close()


def _bot(db, client, channel, *, auto_accept, dry_run=False):
    return SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(
            automation=SimpleNamespace(
                dry_run=dry_run,
                applications=SimpleNamespace(auto_accept=auto_accept),
            )
        ),
        applications_sync=ApplicationsSyncService(client, db),
        channel_for=lambda key: channel if key == "applications" else None,
    )


def _cog(bot):
    cog = NotificationsCog.__new__(NotificationsCog)
    cog.bot = bot
    cog._apps = ApplicationsRepo(bot.db)
    return cog


async def _seed(db, application_id=41, name="Rookie"):
    apps = ApplicationsRepo(db)
    await apps.upsert_seen([
        {"application_id": application_id, "applicant_name": name, "mc_user_id": 777}
    ])
    return apps


# -- service actions ---------------------------------------------------------

async def test_service_accept_hits_annehmen_and_resolves(db):
    apps = await _seed(db, 41)
    client = FakeClient()
    svc = ApplicationsSyncService(client, db)
    await svc.accept(41)
    assert "/verband/bewerbungen/annehmen/41" in client.fetched
    assert (await apps.get(41))["resolved_at"] is not None


async def test_service_deny_hits_ablehnen_and_resolves(db):
    apps = await _seed(db, 42)
    client = FakeClient()
    svc = ApplicationsSyncService(client, db)
    await svc.deny(42)
    assert "/verband/bewerbungen/ablehnen/42" in client.fetched
    assert (await apps.get(42))["resolved_at"] is not None


# -- publisher flows ---------------------------------------------------------

async def test_publish_auto_accepts_and_announces_green(db):
    apps = await _seed(db, 51, "Newbie")
    client, channel = FakeClient(), FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True))
    await cog._publish_applications()
    assert "/verband/bewerbungen/annehmen/51" in client.fetched
    embed, view = channel.sent[0]
    assert "auto-accepted" in embed.title
    assert view is None  # nothing left to decide
    assert (await apps.get(51))["posted_at"] is not None
    assert (await apps.get(51))["resolved_at"] is not None


async def test_publish_auto_accept_failure_falls_back_to_buttons(db):
    apps = await _seed(db, 52)
    client = FakeClient(fail_paths=("/verband/bewerbungen/annehmen/",))
    channel = FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True))
    await cog._publish_applications()
    embed, view = channel.sent[0]
    assert "Auto-accept failed" in embed.title
    assert view is not None and len(view.children) == 2  # the manual backup
    assert (await apps.get(52))["resolved_at"] is None  # still open


async def test_publish_manual_mode_posts_buttons(db):
    await _seed(db, 53)
    client, channel = FakeClient(), FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=False))
    await cog._publish_applications()
    assert client.fetched == []  # no game action without the switch
    embed, view = channel.sent[0]
    assert "New alliance application" in embed.title
    assert view is not None and len(view.children) == 2


async def test_publish_dry_run_never_auto_accepts(db):
    await _seed(db, 54)
    client, channel = FakeClient(), FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True, dry_run=True))
    await cog._publish_applications()
    assert client.fetched == []
    _, view = channel.sent[0]
    assert view is not None  # falls back to the manual buttons


async def test_publish_already_resolved_never_refires_the_action(db):
    apps = await _seed(db, 55)
    await apps.mark_resolved(55)
    client, channel = FakeClient(), FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True))
    await cog._publish_applications()
    assert client.fetched == []  # the action must not fire twice
    embed, view = channel.sent[0]
    assert "already handled" in embed.title
    assert view is None


# -- persistent buttons ------------------------------------------------------

async def test_button_custom_ids_round_trip():
    view = _application_view(99)
    ids = {item.custom_id for item in view.children}
    assert ids == {"fra:app:accept:99", "fra:app:deny:99"}

    match = re.fullmatch(r"fra:app:accept:(?P<aid>[0-9]+)", "fra:app:accept:99")
    item = await ApplicationAcceptButton.from_custom_id(None, None, match)
    assert item.application_id == 99
    match = re.fullmatch(r"fra:app:deny:(?P<aid>[0-9]+)", "fra:app:deny:99")
    item = await ApplicationDenyButton.from_custom_id(None, None, match)
    assert item.application_id == 99


# -- the CoC 5.3 reapply gate -------------------------------------------------

async def _kick_sanction(db, *, days_ago=10.0, status="active"):
    from fra_bot.db.database import Database  # noqa: F401 (fixture provides db)
    from fra_bot.db.repos import SanctionsRepo
    import datetime as dt

    repo = SanctionsRepo(db)
    sid = await repo.add(
        mc_user_id=777, mc_username="Rookie", discord_user_id=None,
        admin_discord_id=1, admin_name="Boss", sanction_type="Kick",
        reason="CoC 5.3", source="manual",
    )
    backdated = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    ).isoformat(timespec="seconds")
    await db.execute(
        "UPDATE sanctions SET created_at = ?, status = ? WHERE id = ?",
        (backdated, status, sid),
    )
    return sid


async def test_publish_withholds_auto_accept_for_recently_kicked(db):
    await _seed(db, 41)
    sid = await _kick_sanction(db, days_ago=10)
    client = FakeClient()
    channel = FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True))
    await cog._publish_applications()
    embed, view = channel.sent[0]
    assert "decide manually" in embed.title
    assert view is not None                       # Accept/Deny buttons shown
    gate = next(f for f in embed.fields if "Sanction gate" in f.name)
    assert f"#{sid}" in gate.value and "CoC 5.3" in gate.value
    # The game action was NOT fired.
    assert not any("annehmen" in path for path in client.fetched)


async def test_publish_gate_clears_after_the_waiting_period(db):
    await _seed(db, 41)
    await _kick_sanction(db, days_ago=90)         # waited out CoC 5.3
    client = FakeClient()
    channel = FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True))
    await cog._publish_applications()
    embed, _ = channel.sent[0]
    assert "auto-accepted" in embed.title
    assert any("annehmen" in path for path in client.fetched)


async def test_publish_gate_clears_when_the_kick_is_revoked(db):
    await _seed(db, 41)
    await _kick_sanction(db, days_ago=5, status="revoked")
    client = FakeClient()
    channel = FakeChannel()
    cog = _cog(_bot(db, client, channel, auto_accept=True))
    await cog._publish_applications()
    embed, _ = channel.sent[0]
    assert "auto-accepted" in embed.title
