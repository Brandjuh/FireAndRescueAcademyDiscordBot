"""Web console FAQ page: listing, the search preview that mirrors the
`!faq` command's three outcomes (answer / did-you-mean / no match), and
add/edit/soft-delete through FaqRepo. All offline via aiohttp's test
client against a real migrated database. The Discord FAQ commands never
write the member-action log, so the web mutations must not either."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fra_bot.db.database import Database
from fra_bot.db.repos import FaqRepo
from fra_bot.web.server import build_app

pytestmark = pytest.mark.asyncio

MINIMAL_YAML = """
missionchief:
  alliance_id: 1621
discord:
  guild_id: 1
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from fra_bot.config import load_config

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("MC_EMAIL", "x@example.com")
    monkeypatch.setenv("MC_PASSWORD", "x")
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    return load_config(path)


class FakeBot:
    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self.actions = []

    async def log_member_action(self, **kwargs) -> None:
        self.actions.append(kwargs)

    def get_cog(self, name):
        return None


@pytest_asyncio.fixture
async def client(tmp_path, cfg):
    db = Database(tmp_path / "web_faq.sqlite3")
    await db.connect()
    repo = FaqRepo(db)
    await repo.add(
        question="How do coins work?",
        answer="Coins are earned by completing daily missions and "
               "alliance events.",
        created_by="Alice", keywords="premium, currency",
    )
    await repo.add(
        question="How do I join an event?",
        answer="Ask an admin in the events channel to sign you up.",
        created_by="Alice", category="events",
    )
    await repo.add(
        question="What are the ARR rules?",
        answer="Use the shared ARR setups and do not edit them without "
               "approval.",
        created_by="Bob", keywords="alarm rules",
    )
    bot = FakeBot(db, cfg)
    test_client = TestClient(TestServer(build_app(bot)))
    await test_client.start_server()
    test_client.bot = bot
    yield test_client
    await test_client.close()
    await db.close()


async def test_faq_page_lists_active_entries(client):
    response = await client.get("/faq")
    text = await response.text()
    assert response.status == 200
    assert ">FAQ<" in text  # joined the top nav
    assert "How do coins work?" in text
    assert "What are the ARR rules?" in text
    assert "premium, currency" in text  # keywords column
    assert "events" in text  # category column
    assert "Entries (3)" in text


async def test_preview_answers_like_the_command_when_confident(client):
    # A verbatim question scores 100 — the bot answers, and so must the
    # preview: the answer body, not a suggestion list.
    text = await (await client.get("/faq?q=how+do+coins+work%3F")).text()
    assert ">would answer<" in text  # the exact outcome badge
    assert "Coins are earned by completing daily missions" in text
    assert ">did you mean<" not in text


async def test_preview_suggests_below_the_threshold(client):
    # "premium currency" only hits entry #1 through its keywords and
    # scores between MIN_SCORE and SUGGESTION_THRESHOLD (~54): the bot
    # sends "did you mean" options, so the preview must too.
    text = await (await client.get("/faq?q=premium+currency")).text()
    assert ">did you mean<" in text
    assert "#1 How do coins work?" in text
    assert ">would answer<" not in text


async def test_preview_reports_no_match(client):
    text = await (await client.get("/faq?q=zzzqqq+nonsense")).text()
    assert "nothing in the FAQ matches" in text
    assert ">would answer<" not in text and ">did you mean<" not in text


async def test_detail_page_shows_answer_and_edit_form(client):
    text = await (await client.get("/faq/1")).text()
    assert "How do coins work?" in text
    assert "Coins are earned by completing daily missions" in text
    assert "Added by Alice" in text
    assert "action='/faq/1/edit'" in text
    assert (await client.get("/faq/999")).status == 404


async def test_add_goes_through_the_repo_without_logging(client):
    response = await client.post(
        "/faq/add",
        data={"question": "What is AFK?", "answer": "Away from keyboard.",
              "keywords": "idle, inactive"},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    row = await FaqRepo(client.bot.db).get(4)
    assert row["question"] == "What is AFK?"
    assert row["keywords"] == "idle, inactive"
    assert row["created_by"] == "Web console"
    # The Discord `!faq add` path never logs a member action; neither may we.
    assert client.bot.actions == []


async def test_add_requires_question_and_answer(client):
    response = await client.post(
        "/faq/add", data={"question": "Only a question"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    assert len(await FaqRepo(client.bot.db).all_active()) == 3


async def test_edit_updates_all_fields_and_can_clear_keywords(client):
    response = await client.post(
        "/faq/1/edit",
        data={"question": "How do Coins work?",
              "answer": "Coins come from daily missions.",
              "category": "economy", "keywords": ""},
        allow_redirects=False,
    )
    assert response.status == 302 and "ok=" in response.headers["Location"]
    row = await FaqRepo(client.bot.db).get(1)
    assert row["question"] == "How do Coins work?"
    assert row["answer"] == "Coins come from daily missions."
    assert row["category"] == "economy"
    assert row["keywords"] == ""  # empty submit really clears the column
    assert client.bot.actions == []
    # Unknown id and empty answer are rejected with an error flash.
    response = await client.post(
        "/faq/999/edit", data={"question": "x", "answer": "y"},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    response = await client.post(
        "/faq/1/edit", data={"question": "x", "answer": ""},
        allow_redirects=False,
    )
    assert "err=" in response.headers["Location"]
    row = await FaqRepo(client.bot.db).get(1)
    assert row["answer"] == "Coins come from daily missions."


async def test_delete_is_soft_and_hides_the_entry_everywhere(client):
    response = await client.post("/faq/3/delete", allow_redirects=False)
    assert response.status == 302 and "ok=" in response.headers["Location"]
    repo = FaqRepo(client.bot.db)
    assert await repo.get(3) is None  # repo semantics: gone from reads
    # ...but the row itself survives for history (soft delete).
    async with client.bot.db.conn.execute(
        "SELECT is_deleted FROM faq_entries WHERE id = 3"
    ) as cur:
        assert (await cur.fetchone())["is_deleted"] == 1
    text = await (await client.get("/faq")).text()
    assert "ARR rules" not in text and "Entries (2)" in text
    # A deleted entry can no longer win the search preview either.
    text = await (await client.get("/faq?q=what+are+the+arr+rules%3F")).text()
    assert ">would answer<" not in text
    # Deleting again reports an error instead of pretending success.
    response = await client.post("/faq/3/delete", allow_redirects=False)
    assert "err=" in response.headers["Location"]
    assert client.bot.actions == []
