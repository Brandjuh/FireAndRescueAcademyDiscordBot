"""The event pinger: region resolution + role-ping logic (ported verbatim
from the reference bot's eventpinger, with its test expectations), the
start-ping outbox, and the delivery cog."""

from types import SimpleNamespace

import pytest_asyncio

from fra_bot.cogs.eventpinger import (
    EventPingerCog,
    build_notification_embed,
    discord_timestamp,
    format_notification_mentions,
)
from fra_bot.db.database import Database
from fra_bot.db.repos import EventPingsRepo
from fra_bot.geo.regions import (
    RegionMatch,
    find_region_role,
    region_from_address_details,
    resolve_region,
    state_from_zip,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "pings.sqlite3")
    await database.connect()
    yield database
    await database.close()


class FakeRole:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"


class FakeGuild:
    def __init__(self, roles):
        self.roles = roles

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None


class FakeChannel:
    def __init__(self, guild):
        self.guild = guild
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


# -- region resolution: the reference bot's expectations ---------------------

def test_resolves_us_zip_to_new_york():
    match = resolve_region("71 East 153rd Street, 10451 New York, The Bronx")
    assert match.code == "NY"
    assert match.name == "New York (NY)"
    assert match.source == "us_zip"


def test_zip_prefix_201_resolves_to_virginia_not_dc():
    assert state_from_zip("20101") == "VA"


def test_resolves_bermuda_postal_code_instead_of_florida():
    match = resolve_region("FL 04 Flatts")
    assert match.code == "BM"
    assert match.name == "Bermuda (BM)"


def test_uncertain_address_returns_none():
    assert resolve_region("Main Street near the park") is None


def test_european_postal_code_needs_us_context():
    assert resolve_region("52 Bogenstraße, 46045 Oberhausen, Altstaden") is None


def test_address_details_resolve_bermuda_country():
    match = region_from_address_details({"country": "Bermuda", "country_code": "bm"})
    assert match.code == "BM"
    assert match.source == "geocode_country"


def test_address_details_resolve_us_state():
    match = region_from_address_details(
        {"city": "Los Angeles", "state": "California",
         "country": "United States", "country_code": "us"}
    )
    assert match.code == "CA"
    assert match.source == "geocode_state"


def test_address_details_resolve_global_country():
    match = region_from_address_details(
        {"city": "Oberhausen", "country": "Germany", "country_code": "de"}
    )
    assert match.code == "COUNTRY:DE"
    assert match.name == "Germany (DE)"
    assert "Germany (DE)" in match.role_names


def test_finds_region_role_by_hardcoded_name():
    role = FakeRole(1, "New York (NY)")
    assert find_region_role(FakeGuild([role]), "NY") is role


def test_country_role_does_not_collide_with_state_code():
    california = FakeRole(1, "California (CA)")
    canada = FakeRole(2, "Canada (CA)")
    guild = FakeGuild([california, canada])
    match = RegionMatch("COUNTRY:CA", "Canada (CA)", "geocode_country", ("Canada (CA)", "Canada"))
    assert find_region_role(guild, match) is canada


def test_discord_timestamp_uses_full_style():
    assert discord_timestamp("2026-06-28T19:08:30+00:00") == "<t:1782673710:F>"
    assert discord_timestamp(None) == "Unknown"


def test_notification_embed_uses_reference_layout():
    embed = build_notification_embed(
        "event", "Storm Surge", "FL 04 Flatts",
        RegionMatch("BM", "Bermuda (BM)", "test"),
        {"location": "New York City, NY, USA", "type": "Surprise event",
         "scheduled_at": "2026-06-28T19:08:30+00:00"},
    )
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "MissionChief Alliance Event"
    assert fields["Alliance Event"] == "Storm Surge"
    assert fields["Location"] == "FL 04 Flatts"
    assert fields["Region"] == "Bermuda (BM)"
    assert fields["Next Alliance Event"] == "\n".join([
        "Location: New York City, NY, USA",
        "Type: Surprise event",
        "Scheduled time: <t:1782673710:F>",
    ])


def test_mentions_join_notify_and_region():
    assert format_notification_mentions("<@&1>", "<@&2>") == "<@&1> <@&2>"
    assert format_notification_mentions("<@&1>", None) == "<@&1>"


# -- delivery cog -------------------------------------------------------------

NOTIFY_ROLE_ID = 669496241591418890


def _cog(db, *, guild, geocoder=None, scheduler=None):
    """Assemble the cog without starting its task loop."""
    cog = EventPingerCog.__new__(EventPingerCog)
    cog.bot = SimpleNamespace(
        db=db,
        cfg=SimpleNamespace(discord=SimpleNamespace(
            notify_event_role_id=NOTIFY_ROLE_ID,
        )),
        geocoder=geocoder or SimpleNamespace(),
        missions_service=scheduler,
    )
    cog.repo = EventPingsRepo(db)
    return cog


class FakeGeocoderDetails:
    def __init__(self, details):
        self.details = details

    async def reverse_details(self, lat, lng):
        return self.details


async def test_ping_mentions_notify_and_region_role(db):
    notify = FakeRole(NOTIFY_ROLE_ID, "Notify-Event")
    state = FakeRole(2, "New York (NY)")
    guild = FakeGuild([notify, state])
    channel = FakeChannel(guild)
    geocoder = FakeGeocoderDetails(
        {"state": "New York", "country": "United States", "country_code": "us"}
    )
    cog = _cog(db, guild=guild, geocoder=geocoder)

    repo = EventPingsRepo(db)
    await repo.add(kind="large", name="Test Mission",
                   address="71 East 153rd Street, 10451 New York, The Bronx",
                   latitude=40.82, longitude=-73.93)
    row = (await repo.unposted())[0]
    await cog._send_ping(channel, row)

    content, kwargs = channel.sent[0]
    assert notify.mention in content
    assert state.mention in content
    fields = {f.name: f.value for f in kwargs["embed"].fields}
    assert fields["Region"] == "New York (NY)"
    assert fields["Alliance Mission"] == "Test Mission"
    assert fields["Location"] == "71 East 153rd Street, 10451 New York, The Bronx"
    assert kwargs["allowed_mentions"].roles is True


async def test_unresolved_address_pings_notify_only(db):
    notify = FakeRole(NOTIFY_ROLE_ID, "Notify-Event")
    guild = FakeGuild([notify, FakeRole(2, "Florida (FL)")])
    channel = FakeChannel(guild)

    class NoDetails:
        async def reverse_details(self, lat, lng):
            return None

    cog = _cog(db, guild=guild, geocoder=NoDetails())
    repo = EventPingsRepo(db)
    await repo.add(kind="event", name="Storm Surge", address="Unknown shoreline",
                   latitude=None, longitude=None)
    row = (await repo.unposted())[0]
    await cog._send_ping(channel, row)

    content, kwargs = channel.sent[0]
    assert notify.mention in content
    assert "<@&2>" not in content
    fields = {f.name: f.value for f in kwargs["embed"].fields}
    assert fields["Region"] == "Unresolved, Notify-Event only"


async def test_text_fallback_when_geocode_has_no_details(db):
    """Without geocode details, the reference text heuristics still map a
    ZIP+context address to the state role."""
    notify = FakeRole(NOTIFY_ROLE_ID, "Notify-Event")
    state = FakeRole(3, "New York (NY)")
    guild = FakeGuild([notify, state])
    channel = FakeChannel(guild)

    class NoDetails:
        async def reverse_details(self, lat, lng):
            return None

    cog = _cog(db, guild=guild, geocoder=NoDetails())
    repo = EventPingsRepo(db)
    await repo.add(kind="large", name="Major fire",
                   address="260 Broadway, 10000 New York, Manhattan",
                   latitude=40.7, longitude=-74.0)
    await cog._send_ping(channel, (await repo.unposted())[0])
    content, _ = channel.sent[0]
    assert state.mention in content


async def test_delivery_marks_posted_and_skips_stale(db):
    notify = FakeRole(NOTIFY_ROLE_ID, "Notify-Event")
    guild = FakeGuild([notify])
    channel = FakeChannel(guild)

    class NoDetails:
        async def reverse_details(self, lat, lng):
            return None

    cog = _cog(db, guild=guild, geocoder=NoDetails())
    cog.bot.channel_for = lambda key: channel

    repo = EventPingsRepo(db)
    ping_id = await repo.add(kind="large", name="Test", address="Somewhere",
                             latitude=None, longitude=None)
    # A stale row (older than the cutoff) is dropped without sending.
    stale_id = await repo.add(kind="event", name="Old", address="Elsewhere",
                              latitude=None, longitude=None)
    await db.execute(
        "UPDATE event_pings SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (stale_id,),
    )

    await cog._deliver_pending()

    assert len(channel.sent) == 1          # only the fresh one was sent
    assert await repo.unposted() == []     # both are marked handled
    assert ping_id != stale_id
