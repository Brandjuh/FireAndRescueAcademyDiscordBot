"""Treasury sync (/verband/kasse): balance, income top lists, expenses.

Three jobs live here:

* :meth:`sync_balance_and_income` — total funds + daily/monthly income
  top lists, stored as snapshots keyed by the New York game day/month
  (MissionChief resets these lists at midnight America/New_York). The
  scheduler also fires this shortly before midnight NY so the final
  pre-reset standings are always captured.
* :meth:`backfill_step` — resumable initial ingestion of the full
  expense ledger (3150+ pages). Pages are walked newest→oldest in small
  chunks; rows go to a staging table; when the walk reaches the end the
  staging content is committed to ``expenses`` oldest-first in one
  transaction. Progress survives restarts.
* :meth:`sync_expenses_incremental` — after backfill, keeps the ledger
  current by scanning from page 1 until the sequence anchor (the newest
  stored rows) is found. Identical-looking rows are real events, so
  alignment is done on row *sequences*, never on single rows.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..db.database import Database
from ..db.repos import RunsRepo, StateRepo, TreasuryRepo, ny_period_keys
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError, ParseError
from ..mc.parsers.treasury import (
    parse_expenses_page,
    parse_income_table,
    parse_total_funds,
)
from .anchor import count_overlap
from .backfill_guard import record_page_failure

log = logging.getLogger(__name__)

KASSE_PATH = "/verband/kasse"

STATE_BACKFILL_DONE = "expenses_backfill_done"
STATE_BACKFILL_NEXT_PAGE = "expenses_backfill_next_page"
STATE_BACKFILL_PAGE_FAIL = "expenses_backfill_page_fail"  # "page:count"

# Skip a single page that keeps failing to fetch so one bad page can't
# wedge the whole multi-thousand-page backfill forever. Transient errors
# clear well before this; a page that fails this many polls in a row is
# almost certainly a deterministic problem on that page.
BACKFILL_SKIP_AFTER = 5

ANCHOR_TAIL_SIZE = 60
MAX_INCREMENTAL_PAGES = 40


class TreasurySyncService:
    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self._cfg = cfg
        self._client = client
        self._treasury = TreasuryRepo(db)
        self._runs = RunsRepo(db)
        self._state = StateRepo(db)

    # ------------------------------------------------------------------
    # Balance + income top lists
    # ------------------------------------------------------------------

    async def sync_balance_and_income(self) -> None:
        run_id = await self._runs.start("treasury")
        # Period keys BEFORE the fetches: the paced page loads can take
        # minutes, and a sync that started before the NY midnight reset
        # used to store the finished day's standings under the NEW day's
        # key — the finished day then had no final capture at all.
        day_key, month_key = ny_period_keys()
        try:
            daily_html = await self._client.fetch_page(KASSE_PATH)
            monthly_html = await self._client.fetch_page(f"{KASSE_PATH}?type=monthly")
        except MissionChiefError as exc:
            await self._runs.finish(run_id, status="failed", message=str(exc))
            raise
        rows = 0

        total_funds = parse_total_funds(daily_html)
        if total_funds is not None:
            await self._treasury.record_balance(total_funds)
        else:
            log.warning("Could not parse total alliance funds from kasse page")

        # …and re-read them AFTER. Taking the keys up front fixed one race
        # (a pre-reset sync filing under the new day) but created its
        # mirror image: a sync that STARTS just before the NY reset and
        # finishes just after it fetches the NEW day's near-empty list and
        # would file it under the FINISHED day's key — overwriting that
        # day's final standings with a couple of early birds. When the
        # period rolled over mid-sync we cannot tell which day the page
        # showed, so we store nothing and let the next run (minutes later,
        # unambiguously in the new period) capture it.
        day_key_after, month_key_after = ny_period_keys()

        # A skipped capture used to finish as a plain success with rows=0,
        # so the day whose FINAL standings were lost looked exactly like a
        # healthy run. The morning then presented that day's last interval
        # snapshot (up to ~35 min before the reset) as final, or nothing
        # at all. Say so instead.
        skipped: list[str] = []

        daily_entries = parse_income_table(daily_html)
        if daily_entries and day_key_after != day_key:
            log.warning(
                "treasury: NY day rolled over mid-sync (%s -> %s); daily "
                "income snapshot skipped rather than filed under the wrong day",
                day_key, day_key_after,
            )
            skipped.append(f"daily {day_key} (rolled into {day_key_after})")
        elif daily_entries:
            if await self._treasury.store_income_snapshot(
                "daily", day_key, daily_entries
            ):
                rows += len(daily_entries)
            else:
                skipped.append(f"daily {day_key} (smaller than stored)")

        monthly_entries = parse_income_table(monthly_html)
        if monthly_entries and month_key_after != month_key:
            log.warning(
                "treasury: NY month rolled over mid-sync (%s -> %s); monthly "
                "income snapshot skipped", month_key, month_key_after,
            )
            skipped.append(f"monthly {month_key} (rolled into {month_key_after})")
        elif monthly_entries:
            if await self._treasury.store_income_snapshot(
                "monthly", month_key, monthly_entries
            ):
                rows += len(monthly_entries)
            else:
                skipped.append(f"monthly {month_key} (smaller than stored)")

        if skipped:
            note = "income snapshot not stored: " + "; ".join(skipped)
            await self._runs.finish(
                run_id, status="success", pages=2,
                rows_parsed=rows, rows_new=rows, message=note,
            )
            notify = getattr(self, "notify_admin", None)
            if notify is not None:
                await notify(
                    "⚠️ **Treasury** — " + note
                    + ". That period's final standings may be missing or "
                    "stale in the reports."
                )
            return

        await self._runs.finish(
            run_id, status="success", pages=2, rows_parsed=rows, rows_new=rows
        )
        log.info(
            "Treasury sync: funds=%s, daily rows=%d, monthly rows=%d",
            total_funds, len(daily_entries), len(monthly_entries),
        )

    # ------------------------------------------------------------------
    # Expenses: initial backfill
    # ------------------------------------------------------------------

    async def backfill_done(self) -> bool:
        return await self._state.get(STATE_BACKFILL_DONE) == "1"

    async def backfill_step(self) -> bool:
        """Process one chunk of the initial backfill.

        Returns True when the backfill is (now) complete. Runs as BULK
        traffic: its requests yield to board work on the shared pacer.
        """
        from ..core.pacing import bulk_traffic

        with bulk_traffic():
            return await self._backfill_step_paced()

    async def _backfill_step_paced(self) -> bool:
        if await self.backfill_done():
            return True

        chunk_size = self._cfg.sync.expenses_backfill_pages_per_chunk
        next_page = int(await self._state.get(STATE_BACKFILL_NEXT_PAGE, "1"))
        run_id = await self._runs.start("expenses_backfill")
        pages_fetched = 0
        rows_added = 0

        try:
            for _ in range(chunk_size):
                path = KASSE_PATH if next_page == 1 else f"{KASSE_PATH}?page={next_page}"
                try:
                    html = await self._client.fetch_page(path)
                except MissionChiefError as exc:
                    fails = await record_page_failure(
                        self._state, STATE_BACKFILL_PAGE_FAIL, next_page
                    )
                    if fails < BACKFILL_SKIP_AFTER:
                        raise  # outer handler fails the run; retry next poll
                    log.error(
                        "Expenses backfill: skipping page %d after %d consecutive "
                        "failures (%s)", next_page, fails, exc,
                    )
                    next_page += 1
                    await self._state.set(STATE_BACKFILL_NEXT_PAGE, str(next_page))
                    await self._state.delete(STATE_BACKFILL_PAGE_FAIL)
                    continue
                await self._state.delete(STATE_BACKFILL_PAGE_FAIL)
                pages_fetched += 1
                page = parse_expenses_page(html)

                if next_page == 1 and not page.has_table:
                    raise ParseError(
                        "Expense table missing on kasse page 1 — layout change?"
                    )
                if not page.rows:
                    # Walked past the last page: commit the whole ledger
                    # and flip the done flag atomically.
                    copied = await self._treasury.staging_finalize(
                        STATE_BACKFILL_DONE, STATE_BACKFILL_NEXT_PAGE,
                        current_year=int(ny_period_keys()[0][:4]),
                    )
                    await self._runs.finish(
                        run_id,
                        status="success",
                        pages=pages_fetched,
                        rows_new=rows_added,
                        message=f"backfill complete, {copied} expenses committed",
                    )
                    log.info("Expenses backfill COMPLETE: %d rows committed", copied)
                    return True

                # New rows may have shifted the ledger down between fetches;
                # drop the leading rows we already staged.
                tail = await self._treasury.staging_tail_signatures(ANCHOR_TAIL_SIZE)
                signatures = [row["signature"] for row in page.rows]
                overlap = count_overlap(tail, signatures) if tail else 0
                rows_added += await self._treasury.staging_append(page.rows[overlap:])

                next_page += 1
                await self._state.set(STATE_BACKFILL_NEXT_PAGE, str(next_page))
        except MissionChiefError as exc:
            await self._runs.finish(
                run_id,
                status="failed",
                pages=pages_fetched,
                rows_new=rows_added,
                message=str(exc),
            )
            raise

        staged = await self._treasury.staging_count()
        await self._runs.finish(
            run_id,
            status="success",
            pages=pages_fetched,
            rows_new=rows_added,
            message=f"backfill at page {next_page}, {staged} rows staged",
        )
        log.info(
            "Expenses backfill progress: next page %d, %d rows staged",
            next_page, staged,
        )
        return False

    # ------------------------------------------------------------------
    # Expenses: incremental top-up
    # ------------------------------------------------------------------

    async def sync_expenses_incremental(self) -> int:
        """Fetch new expenses from the top of the ledger.

        Returns the number of rows added. No-op until backfill is done.
        """
        if not await self.backfill_done():
            return 0

        run_id = await self._runs.start("expenses")
        stored_tail = await self._treasury.newest_signatures(ANCHOR_TAIL_SIZE)
        window: list[dict] = []  # display order (newest first)
        pages_fetched = 0

        try:
            for page_number in range(1, MAX_INCREMENTAL_PAGES + 1):
                path = KASSE_PATH if page_number == 1 else f"{KASSE_PATH}?page={page_number}"
                html = await self._client.fetch_page(path)
                pages_fetched += 1
                page = parse_expenses_page(html)
                if page_number == 1 and not page.has_table:
                    raise ParseError(
                        "Expense table missing on kasse page 1 — layout change?"
                    )
                if not page.rows:
                    break
                window.extend(page.rows)

                if not stored_tail:
                    break  # empty ledger locally; take what page 1 offers

                chrono = [row["signature"] for row in reversed(window)]
                if count_overlap(stored_tail, chrono) > 0:
                    break  # anchor found — window now covers all new rows
            else:
                message = (
                    f"No anchor within {MAX_INCREMENTAL_PAGES} pages; "
                    "skipping to avoid duplicates"
                )
                await self._runs.finish(
                    run_id, status="failed", pages=pages_fetched, message=message
                )
                log.warning("Expenses incremental: %s", message)
                return 0
        except MissionChiefError as exc:
            await self._runs.finish(
                run_id, status="failed", pages=pages_fetched, message=str(exc)
            )
            raise

        window_chrono = list(reversed(window))
        overlap = count_overlap(
            stored_tail, [row["signature"] for row in window_chrono]
        )
        new_rows = window_chrono[overlap:]
        added = await self._treasury.insert_expenses_chronological(new_rows)
        await self._runs.finish(
            run_id,
            status="success",
            pages=pages_fetched,
            rows_parsed=len(window),
            rows_new=added,
        )
        if added:
            log.info("Expenses incremental: %d new rows", added)
        return added
