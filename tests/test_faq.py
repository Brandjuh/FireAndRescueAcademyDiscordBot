"""FAQ search + repo (reference: faqmanager, ranking formula preserved)."""

import pytest
import pytest_asyncio

from fra_bot.db.database import Database
from fra_bot.db.repos import FaqRepo
from fra_bot.services.faq_search import (
    MIN_SCORE,
    SUGGESTION_THRESHOLD,
    expand_query,
    rank_faqs,
    score_faq,
)
from fra_bot.services.faq_synonyms import DEFAULT_SYNONYMS

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "faq.sqlite3")
    await database.connect()
    yield database
    await database.close()


async def test_synonyms_ported_verbatim():
    # The reference dictionary: 37 game terms, 212 phrasings.
    assert len(DEFAULT_SYNONYMS) == 37
    assert sum(len(v) for v in DEFAULT_SYNONYMS.values()) == 212
    assert "arr" in DEFAULT_SYNONYMS


async def test_expand_query_pulls_in_synonym_group():
    variants = expand_query("how do I set up arr")
    assert "alarm and response regulation" in variants


async def test_exact_question_scores_above_suggestion_threshold():
    score = score_faq(
        "How do coins work?",
        question="How do coins work?",
        answer="Coins are premium currency earned via achievements.",
        category=None,
    )
    assert score >= SUGGESTION_THRESHOLD


async def test_unrelated_query_falls_below_min_score():
    score = score_faq(
        "swimming pool maintenance",
        question="How do coins work?",
        answer="Coins are premium currency.",
        category=None,
    )
    assert score < MIN_SCORE


async def test_rank_faqs_synonym_beats_unrelated(db):
    repo = FaqRepo(db)
    arr = await repo.add(
        question="How do I configure Alarm and Response Regulations?",
        answer="Open Settings → ARR and add a rule per vehicle set.",
        created_by="Admin",
    )
    await repo.add(
        question="How do coins work?",
        answer="Coins are premium currency.",
        created_by="Admin",
    )
    ranked = rank_faqs("arr setup", await repo.all_active())
    assert ranked and ranked[0].faq_id == arr


async def test_keywords_make_an_unfindable_entry_findable(db):
    # A query that shares nothing with question/answer text (and hits no
    # synonym group) only matches once an admin adds it as a keyword.
    repo = FaqRepo(db)
    faq_id = await repo.add(
        question="Vehicle purchase advice",
        answer="Start with engines, then ladders.",
        created_by="Admin",
    )
    query = "xk9 protocol"
    assert rank_faqs(query, await repo.all_active()) == []
    await repo.update(faq_id, keywords="xk9 protocol")
    after = rank_faqs(query, await repo.all_active())
    assert after and after[0].faq_id == faq_id


async def test_repo_soft_delete_hides_entry(db):
    repo = FaqRepo(db)
    faq_id = await repo.add(question="Q", answer="A", created_by="Admin")
    assert await repo.remove(faq_id) is True
    assert await repo.get(faq_id) is None
    assert await repo.all_active() == []
    assert await repo.remove(faq_id) is False  # already gone
