"""Hourly members sync (/verband/mitglieder/<alliance>?page=N).

Safety properties:

* A fetch/parse error mid-run aborts the WHOLE run without touching the
  database — a truncated, credit-sorted list must never be mistaken for
  mass departures.
* A retention guard skips change detection when the scrape looks
  implausibly small compared to the stored roster.
* The very first sync stores the roster silently (no 47 pages of
  "member joined" notifications).
"""

from __future__ import annotations

import logging

from ..config import Config
from ..db.database import Database
from ..db.repos import MembersRepo, RunsRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.members import parse_members_page, validate_members_page

log = logging.getLogger(__name__)

MAX_PAGES = 150
EMPTY_PAGES_STOP = 2
MIN_BASELINE_FOR_GUARD = 100
MIN_RETENTION = 0.5


class MembersSyncService:
    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self._cfg = cfg
        self._client = client
        self._members = MembersRepo(db)
        self._runs = RunsRepo(db)

    async def run(self) -> list[dict]:
        """Scrape the full roster; returns generated member events.

        The sweep is ~47+ pages, so it runs as BULK traffic: its requests
        yield to board work (polls, guides, request execution) on the
        shared pacer instead of starving them for minutes.
        """
        from ..core.pacing import bulk_traffic

        with bulk_traffic():
            return await self._run_paced()

    async def _run_paced(self) -> list[dict]:
        run_id = await self._runs.start("members")
        base_path = f"/verband/mitglieder/{self._cfg.missionchief.alliance_id}"
        roster: list[dict] = []
        seen_ids: set[int] = set()
        pages_fetched = 0
        empty_streak = 0

        try:
            for page_number in range(1, MAX_PAGES + 1):
                html = await self._client.fetch_page(f"{base_path}?page={page_number}")
                pages_fetched += 1
                page = parse_members_page(html)
                validate_members_page(page, page_number)

                fresh = [m for m in page.members if m["mc_user_id"] not in seen_ids]
                if not fresh:
                    # Only repeats (last page echoes) or genuinely empty.
                    empty_streak += 1
                    if empty_streak >= EMPTY_PAGES_STOP:
                        break
                    continue
                empty_streak = 0
                for member in fresh:
                    seen_ids.add(member["mc_user_id"])
                roster.extend(fresh)
        except MissionChiefError as exc:
            await self._runs.finish(
                run_id, status="failed", pages=pages_fetched, message=str(exc)
            )
            log.warning("Members sync aborted, database untouched: %s", exc)
            raise

        previous_count = await self._members.active_count()
        detect_changes = previous_count > 0
        if (
            previous_count >= MIN_BASELINE_FOR_GUARD
            and len(roster) < previous_count * MIN_RETENTION
        ):
            message = (
                f"Retention guard: scraped {len(roster)} members but "
                f"{previous_count} are stored; not applying"
            )
            await self._runs.finish(
                run_id,
                status="failed",
                pages=pages_fetched,
                rows_parsed=len(roster),
                message=message,
            )
            log.warning("%s", message)
            return []

        events = await self._members.apply_roster(
            run_id, roster, detect_changes=detect_changes
        )
        await self._runs.finish(
            run_id,
            status="success",
            pages=pages_fetched,
            rows_parsed=len(roster),
            rows_new=len(events),
            message=None if detect_changes else "initial sync (events suppressed)",
        )
        log.info(
            "Members sync: %d members over %d pages, %d events",
            len(roster), pages_fetched, len(events),
        )
        return events
