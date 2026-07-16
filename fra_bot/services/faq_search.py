"""FAQ fuzzy search (reference bot: faqmanager).

The reference ranking formula is kept:

    score = 0.55 * fuzzy(question, query*)
          + 0.30 * fuzzy(answer,   query*)
          + 0.10 * fuzzy(category, query*)

with a verbatim question match short-circuiting to a perfect score
(the reference's exact boost, made decisive for difflib).

where ``query*`` is the query expanded with the game-terminology
synonym dictionary. Matching uses stdlib difflib (token-set + plain
ratio, best of both) instead of rapidfuzz — same shape, no new
dependency. Thresholds are the reference values: results under
MIN_SCORE are dropped; a best score under SUGGESTION_THRESHOLD is
presented as a suggestion ("did you mean"), not an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .faq_synonyms import DEFAULT_SYNONYMS

TITLE_WEIGHT = 0.55
CONTENT_WEIGHT = 0.30
CATEGORY_WEIGHT = 0.10

MIN_SCORE = 30
SUGGESTION_THRESHOLD = 75


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _ratio(a: str, b: str) -> float:
    """0-100 similarity: the best of the plain ratio, the sorted
    token-set ratio (word order never tanks a match), substring
    containment, and token coverage (rapidfuzz's partial matching
    approximated: how much of the shorter side's tokens appear in the
    longer side — 'coins' inside a long answer must still count)."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    plain = SequenceMatcher(None, a, b).ratio()
    tokens_a = " ".join(sorted(set(a.split())))
    tokens_b = " ".join(sorted(set(b.split())))
    tokenised = SequenceMatcher(None, tokens_a, tokens_b).ratio()
    # Substring containment counts too ("arr" inside "arr setup guide").
    contained = 1.0 if (a in b or b in a) else 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    short_tokens = [t for t in short.split() if len(t) >= 3]
    coverage = 0.0
    if short_tokens:
        matched = sum(len(t) for t in short_tokens if t in long_)
        coverage = 0.85 * matched / sum(len(t) for t in short_tokens)
    return 100.0 * max(plain, tokenised, contained * 0.95, coverage)


def expand_query(query: str) -> list[str]:
    """The query plus every synonym expansion that applies to it."""
    base = _norm(query)
    variants = {base}
    for key, synonyms in DEFAULT_SYNONYMS.items():
        group = {_norm(key), *(_norm(s) for s in synonyms)}
        if any(term and term in base for term in group):
            variants.update(group)
    return list(variants)


@dataclass(frozen=True)
class FaqScore:
    faq_id: int
    question: str
    score: float


def score_faq(
    query: str, *, question: str, answer: str, category: str | None,
    keywords: str | None = None,
) -> float:
    """Rank one FAQ against a query, 0-100 (reference formula)."""
    if _norm(query) == _norm(question):
        # Someone typed the question verbatim: that IS the answer. (The
        # reference's rapidfuzz partial scores carried exact matches over
        # the threshold on their own; difflib's don't, so short-circuit.)
        return 100.0
    variants = expand_query(query)
    haystack_extra = f"{question} {keywords or ''}"
    title = max(_ratio(v, haystack_extra) for v in variants)
    content = max(_ratio(v, answer) for v in variants)
    cat = max(_ratio(v, category or "") for v in variants) if category else 0.0
    return (
        TITLE_WEIGHT * title
        + CONTENT_WEIGHT * content
        + CATEGORY_WEIGHT * cat
    )


def rank_faqs(query: str, rows) -> list[FaqScore]:
    """Rows (question/answer/category/keywords/id) ranked by score,
    already filtered on MIN_SCORE, best first."""
    scored = [
        FaqScore(
            faq_id=row["id"], question=row["question"],
            score=score_faq(
                query, question=row["question"], answer=row["answer"],
                category=row["category"], keywords=row["keywords"],
            ),
        )
        for row in rows
    ]
    return sorted(
        (s for s in scored if s.score >= MIN_SCORE),
        key=lambda s: s.score, reverse=True,
    )
