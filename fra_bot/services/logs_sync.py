"""Alliance logs sync (/alliance_logfiles?page=N), incremental.

Walks pages newest-first until it hits a page whose rows are all
already known, then inserts everything in CHRONOLOGICAL order so
ascending database ids match event order. Identical rows (same second,
same text) are kept via occurrence indexes — see LogsRepo.

On the very first run a deeper backfill is done and all rows are marked
as posted, so history never floods the Discord feed.
"""

from __future__ import annotations

import logging

from ..db.database import Database
from ..db.repos import LogsRepo, RunsRepo, StateRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError, ParseError
from ..mc.parsers.logs import parse_logs_page

log = logging.getLogger(__name__)

LOGS_PATH = "/alliance_logfiles"
MAX_INCREMENTAL_PAGES = 10
FIRST_RUN_PAGES = 25
STATE_INITIALIZED = "logs_initialized"


class LogsSyncService:
    def __init__(self, client: MissionChiefClient, db: Database) -> None:
        self._client = client
        self._logs = LogsRepo(db)
        self._runs = RunsRepo(db)
        self._state = StateRepo(db)

    async def run(self) -> int:
        """Returns the number of newly stored log rows."""
        run_id = await self._runs.start("logs")
        first_run = await self._state.get(STATE_INITIALIZED) is None
        max_pages = FIRST_RUN_PAGES if first_run else MAX_INCREMENTAL_PAGES

        collected: list[dict] = []  # newest first, across pages
        pages_fetched = 0
        try:
            for page_number in range(1, max_pages + 1):
                html = await self._client.fetch_page(f"{LOGS_PATH}?page={page_number}")
                pages_fetched += 1
                page = parse_logs_page(html)
                if page_number == 1 and not page.has_table:
                    raise ParseError(
                        "Logs page 1 has no table — layout change or not logged in"
                    )
                if not page.rows:
                    break
                collected.extend(page.rows)

                if not first_run:
                    signatures = [row["signature"] for row in page.rows]
                    known = await self._logs.known_signatures(signatures)
                    if all(sig in known for sig in signatures):
                        break  # deeper pages are older than what we have
        except MissionChiefError as exc:
            await self._runs.finish(
                run_id, status="failed", pages=pages_fetched, message=str(exc)
            )
            raise

        # Chronological order (oldest first) before inserting.
        collected.reverse()
        inserted = await self._logs.insert_batch(collected)

        if first_run:
            suppressed = await self._logs.mark_all_posted()
            await self._state.set(STATE_INITIALIZED, "1")
            log.info("Logs first run: stored %d rows, %d suppressed from feed",
                     inserted, suppressed)

        await self._runs.finish(
            run_id,
            status="success",
            pages=pages_fetched,
            rows_parsed=len(collected),
            rows_new=inserted,
        )
        if inserted:
            log.info("Logs sync: %d new rows over %d pages", inserted, pages_fetched)
        return inserted
