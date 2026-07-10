"""In-game PM sending, requester DM texts, and the admin requeue path."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.mc.errors import ParseError
from fra_bot.mc.messages import (
    build_message_payload,
    message_was_sent,
    parse_message_form,
    send_ingame_message,
    summarize_response,
)

pytestmark = pytest.mark.asyncio

COMPOSE_HTML = """
<form action="/messages" method="post">
  <input type="hidden" name="authenticity_token" value="tok"/>
  <input type="text" name="message[recipient]" value=""/>
  <input type="text" name="message[subject]" value=""/>
  <textarea name="message[body]"></textarea>
  <input type="submit" name="commit" value="Send"/>
</form>
"""

SENT_HTML = "<html><body><div class='alert'>Message Sent.</div></body></html>"


class FakeClient:
    def __init__(self, *, post_status=200, post_html=SENT_HTML, post_url=""):
        self.post_status = post_status
        self.post_html = post_html
        self.post_url = post_url
        self.posts = []

    def url(self, path):
        return "https://www.missionchief.com" + path

    async def fetch_page(self, path, *, referer=None):
        return COMPOSE_HTML

    async def post_form(self, path, data, **kwargs):
        self.posts.append((path, dict(data)))
        return (self.post_status, self.post_html, self.post_url)


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
    # The submit button rides along, like a real browser POST.
    assert payload["commit"] == "Send"


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


async def test_unconfirmed_send_counts_as_failure():
    """MissionChief re-renders the compose form with HTTP 200 when the
    message was NOT sent — that must never be scored as delivered (it made
    the tax warnings record sends that never happened)."""
    rerendered = FakeClient(post_status=200, post_html=COMPOSE_HTML)
    assert await send_ingame_message(rerendered, "Alice", "S", "B") is False


async def test_conversation_redirect_counts_as_delivered():
    client = FakeClient(
        post_html="<html></html>",
        post_url="https://www.missionchief.com/messages/12345",
    )
    assert await send_ingame_message(client, "Alice", "S", "B") is True


def test_message_was_sent_signals():
    assert message_was_sent(SENT_HTML) is True
    assert message_was_sent("<b>MESSAGE SENT.</b>") is True   # case-proof
    assert message_was_sent(COMPOSE_HTML) is False
    assert message_was_sent("", "https://x/messages/9") is True
    assert message_was_sent("", "https://x/messages/new") is False


def test_summarize_response_redacts_the_csrf_token():
    digest = summarize_response(COMPOSE_HTML + "authenticity_token=secret123")
    assert "secret123" not in digest
    assert "REDACTED" in digest


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


def test_built_detail_becomes_clickable_link():
    """The 'built prison #id' embed line links to the building; the payload
    summary links the id too (embed titles can't carry links, so the linked
    text leads the description)."""
    import re

    from fra_bot.cogs.automation import AutomationCog

    detail = "built prison #5561931"
    built = re.match(r"built (\w+) #(\d+)\b", detail)
    assert built is not None
    linked = (
        f"**[Built {built.group(1)} #{built.group(2)}]"
        f"(https://www.missionchief.com/buildings/{built.group(2)})**"
    )
    assert "buildings/5561931" in linked

    summary = AutomationCog._payload_summary(
        '{"building_type": "prison", "building_id": 5561931}'
    )
    assert "[#5561931](https://www.missionchief.com/buildings/5561931)" in summary
