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
