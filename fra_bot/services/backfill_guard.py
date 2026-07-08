"""Shared guard so one persistently-failing page can't wedge a backfill.

Both the expenses and alliance-log backfills walk thousands of pages from
a cursor. If a single page fails to fetch every poll, the cursor never
advances and the whole walk stalls — expenses/logs never finish. This
tracks consecutive failures for the current page so the caller can skip
past a genuinely broken page after enough retries, while still retrying
transient errors (which clear well before the threshold).
"""

from __future__ import annotations


async def record_page_failure(state, key: str, page: int) -> int:
    """Increment the consecutive-failure count for ``page`` (reset on a new
    page). Returns the new count. State value is ``"<page>:<count>"``."""
    raw = await state.get(key) or ""
    prev_page, _, prev_count = raw.partition(":")
    count = int(prev_count) + 1 if prev_page == str(page) and prev_count.isdigit() else 1
    await state.set(key, f"{page}:{count}")
    return count
