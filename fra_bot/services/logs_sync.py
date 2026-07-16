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

from ..config import Config
from ..db.database import Database
from ..db.repos import LogsRepo, RunsRepo, StateRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError, ParseError
from ..mc.parsers.logs import parse_logs_page
from .backfill_guard import record_page_failure

log = logging.getLogger(__name__)

LOGS_PATH = "/alliance_logfiles"
MAX_INCREMENTAL_PAGES = 10
FIRST_RUN_PAGES = 25
STATE_INITIALIZED = "logs_initialized"

STATE_BACKFILL_DONE = "logs_backfill_done"
STATE_BACKFILL_NEXT_PAGE = "logs_backfill_next_page"
STATE_BACKFILL_PAGE_FAIL = "logs_backfill_page_fail"  # "page:count"
# Skip a page that keeps failing to fetch so one bad page can't wedge the
# whole backfill (transient errors clear well before this).
BACKFILL_SKIP_AFTER = 5


class LogsSyncService:
    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self._cfg = cfg
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
        seen_signatures: set[str] = set()
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
                # A log line landing MID-WALK shifts every row down one, so
                # page N+1 re-shows the tail of page N. Everything goes into
                # ONE insert batch here (unlike the backfill's page-per-batch,
                # which dedupes this via the DB occurrence count), so drop
                # rows already collected from an EARLIER page — without this
                # the re-shown row counts as a second occurrence and the feed
                # posts it twice. Repeats within one page stay: those are
                # genuinely repeated events, not shift artifacts.
                collected.extend(
                    r for r in page.rows if r["signature"] not in seen_signatures
                )
                seen_signatures.update(r["signature"] for r in page.rows)

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

    # ------------------------------------------------------------------
    # Full-history backfill (opt-in deep walk to the alliance's start)
    # ------------------------------------------------------------------

    async def backfill_done(self) -> bool:
        return await self._state.get(STATE_BACKFILL_DONE) == "1"

    async def backfill_step(self) -> bool:
        """Process one chunk of the full log-history backfill.

        Walks pages newest→oldest from a saved cursor, inserting each page's
        rows (deduped by signature) and marking them already-posted so old
        history never floods the feed. Returns True once the walk reaches
        the last page. Resumable across restarts; the global pacer keeps it
        gentle regardless of chunk size. Runs as BULK traffic: its requests
        yield to board work on the shared pacer.
        """
        from ..core.pacing import bulk_traffic

        with bulk_traffic():
            return await self._backfill_step_paced()

    async def _backfill_step_paced(self) -> bool:
        if await self.backfill_done():
            return True

        chunk_size = self._cfg.sync.logs_backfill_pages_per_chunk
        next_page = int(await self._state.get(STATE_BACKFILL_NEXT_PAGE, "1"))
        run_id = await self._runs.start("logs_backfill")
        pages_fetched = 0
        inserted_total = 0

        try:
            for _ in range(chunk_size):
                try:
                    html = await self._client.fetch_page(f"{LOGS_PATH}?page={next_page}")
                except MissionChiefError as exc:
                    fails = await record_page_failure(
                        self._state, STATE_BACKFILL_PAGE_FAIL, next_page
                    )
                    if fails < BACKFILL_SKIP_AFTER:
                        raise  # outer handler fails the run; retry next poll
                    log.error(
                        "Log backfill: skipping page %d after %d consecutive "
                        "failures (%s)", next_page, fails, exc,
                    )
                    next_page += 1
                    await self._state.set(STATE_BACKFILL_NEXT_PAGE, str(next_page))
                    await self._state.delete(STATE_BACKFILL_PAGE_FAIL)
                    continue
                await self._state.delete(STATE_BACKFILL_PAGE_FAIL)
                pages_fetched += 1
                page = parse_logs_page(html)

                if next_page == 1 and not page.has_table:
                    raise ParseError(
                        "Logs page 1 has no table — layout change or not logged in"
                    )
                if not page.rows:
                    # Walked past the last page: history is complete.
                    await self._state.set(STATE_BACKFILL_DONE, "1")
                    await self._state.delete(STATE_BACKFILL_NEXT_PAGE)
                    await self._runs.finish(
                        run_id, status="success", pages=pages_fetched,
                        rows_new=inserted_total, message="log backfill complete",
                    )
                    log.info("Alliance log backfill COMPLETE (%d rows)", inserted_total)
                    return True

                # Oldest-first within the page; mark posted so nothing is
                # re-announced. Cross-page duplicates are deduped by signature.
                inserted_total += await self._logs.insert_batch(
                    list(reversed(page.rows)), mark_posted=True
                )
                next_page += 1
                await self._state.set(STATE_BACKFILL_NEXT_PAGE, str(next_page))
        except MissionChiefError as exc:
            await self._runs.finish(
                run_id, status="failed", pages=pages_fetched,
                rows_new=inserted_total, message=str(exc),
            )
            raise

        await self._runs.finish(
            run_id, status="success", pages=pages_fetched,
            rows_new=inserted_total, message=f"log backfill at page {next_page}",
        )
        log.info(
            "Alliance log backfill progress: next page %d (%d new this chunk)",
            next_page, inserted_total,
        )
        return False
