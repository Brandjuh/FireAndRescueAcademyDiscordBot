"""In-game PM sending, requester DM texts, and the admin requeue path."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.mc.errors import ParseError
from fra_bot.mc.messages import (
    build_message_payload,
    parse_message_form,
    send_ingame_message,
)

pytestmark = pytest.mark.asyncio

COMPOSE_HTML = """
<form action="/messages" method="post">
  <input type="hidden" name="authenticity_token" value="tok"/>
  <input type="text" name="message[recipient]" value=""/>
  <input type="text" name="message[subject]" value=""/>
  <textarea name="message[body]"></textarea>
  <input type="submit" value="Send"/>
</form>
"""


class FakeClient:
    def __init__(self, *, post_status=200):
        self.post_status = post_status
        self.posts = []

    def url(self, path):
        return "https://www.missionchief.com" + path

    async def fetch_page(self, path, *, referer=None):
        return COMPOSE_HTML

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, dict(data)))
        return (self.post_status, {}, "")


def test_parse_message_form_finds_fields():
    form = parse_message_form(COMPOSE_HTML)
    assert form.action == "/messages"
    assert form.recipient_field == "message[recipient]"
    assert form.subject_field == "message[subject]"
    assert form.body_field == "message[body]"
    payload = build_message_payload(form, "Alice", "Hi", "Body text")
    assert payload["message[recipient]"] == "Alice"
    assert payload["message[subject]"] == "Hi"
    assert payload["message[body]"] == "Body text"
    assert payload["authenticity_token"] == "tok"


def test_parse_message_form_rejects_broken_layout():
    with pytest.raises(ParseError):
        parse_message_form("<html><body>no form</body></html>")


async def test_send_ingame_message_posts_and_reports():
    client = FakeClient()
    assert await send_ingame_message(client, "Alice", "Subject", "Body") is True
    path, data = client.posts[0]
    assert path == "/messages"
    assert data["message[recipient]"] == "Alice"

    rejected = FakeClient(post_status=422)
    assert await send_ingame_message(rejected, "Alice", "S", "B") is False


def test_requester_dm_texts_match_reference_bot():
    from fra_bot.cogs.automation import _requester_dm_text

    done = _requester_dm_text("training", "done", "", {
        "results": [{"training": "HazMat", "outcome": "opened", "building_id": 42}],
    })
    assert "started automatically: **HazMat**" in done
    assert "buildings/42" in done and "How to add people to the course" in done

    sent = _requester_dm_text("training", "failed", "no free classroom", {})
    assert "sent to admins for manual start" in sent

    built = _requester_dm_text("building", "done", "", {
        "building_type": "hospital", "address": "St Olavs, Trondheim",
        "latitude": 63.42, "longitude": 10.39, "building_id": 77,
    })
    assert "**APPROVED**" in built and "🏥" in built and "buildings/77" in built

    # No DM for outcomes that don't notify.
    assert _requester_dm_text("training", "done", "", {"results": []}) is None
    assert _requester_dm_text("event", "done", "", {}) is None


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "m.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_requeue_resets_failed_request(db):
    from fra_bot.db.repos import AutomationRepo

    repo = AutomationRepo(db)
    rid = await repo.create(kind="building", thread_id=1, post_id=1,
                            requester_name="A", requester_mc_id=9, payload="{}")
    await repo.claim(rid)
    await repo.set_status(rid, "failed", "build failed", bump_attempts=True)
    assert await repo.requeue(rid, payload='{"clean": true}') is True
    row = await repo.get(rid)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["payload"] == '{"clean": true}'
    assert "re-queued by admin" in row["status_detail"]

    # An open request cannot be re-queued (it is already being worked on).
    other = await repo.create(kind="building", thread_id=1, post_id=2,
                              requester_name="A", requester_mc_id=9, payload="{}")
    assert await repo.requeue(other) is False
