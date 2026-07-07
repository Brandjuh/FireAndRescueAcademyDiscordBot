"""Sequence-anchor matching for append-only ledgers.

MissionChief's expense ledger (and logs) can contain rows that look
byte-for-byte identical yet are distinct real events, so single-row
dedup is unsafe. Instead we align *sequences*: given the tail of what
we already stored and a freshly scraped window, find how many leading
scraped rows are already covered.
"""

from __future__ import annotations


def count_overlap(stored_tail: list[str], scraped: list[str]) -> int:
    """Largest j such that ``stored_tail`` ends with ``scraped[:j]``.

    Both lists must be in the same order. ``scraped[j:]`` are the rows
    not yet stored. Returns 0 when there is no overlap at all.
    """
    max_j = min(len(stored_tail), len(scraped))
    for j in range(max_j, 0, -1):
        if stored_tail[-j:] == scraped[:j]:
            return j
    return 0
