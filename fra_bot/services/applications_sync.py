"""Applications sync (/verband/bewerbungen), every few minutes.

State lives in the database, so applications that arrived while the bot
was down are still announced exactly once after restart.
"""

from __future__ import annotations

import logging

from ..db.database import Database
from ..db.repos import ApplicationsRepo, RunsRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.applications import parse_applications_page

log = logging.getLogger(__name__)

APPLICATIONS_PATH = "/verband/bewerbungen"
ACCEPT_PATH = "/verband/bewerbungen/annehmen/{application_id}"
DENY_PATH = "/verband/bewerbungen/ablehnen/{application_id}"


class ApplicationsSyncService:
    def __init__(self, client: MissionChiefClient, db: Database) -> None:
        self._client = client
        self._apps = ApplicationsRepo(db)
        self._runs = RunsRepo(db)

    async def run(self) -> list[int]:
        """Returns application ids that are new since the previous check."""
        run_id = await self._runs.start("applications")
        try:
            html = await self._client.fetch_page(APPLICATIONS_PATH)
            applications = parse_applications_page(html)
        except MissionChiefError as exc:
            await self._runs.finish(run_id, status="failed", message=str(exc))
            raise

        new_ids = await self._apps.upsert_seen(applications)
        await self._runs.finish(
            run_id,
            status="success",
            pages=1,
            rows_parsed=len(applications),
            rows_new=len(new_ids),
        )
        if new_ids:
            log.info("Applications sync: %d listed, %d new", len(applications), len(new_ids))
        return new_ids

    # -- accept / deny (reference bot: newmembernotify) -----------------

    async def accept(self, application_id: int) -> None:
        """Accept an application in-game (the same GET the site's own
        Accept button performs). Raises MissionChiefError on failure."""
        await self._act(application_id, ACCEPT_PATH, "accept")

    async def deny(self, application_id: int) -> None:
        """Deny an application in-game. Raises MissionChiefError on failure."""
        await self._act(application_id, DENY_PATH, "deny")

    async def _act(self, application_id: int, path_fmt: str, verb: str) -> None:
        # fetch_page gives us pacing, retries, re-login and the error
        # taxonomy; the action endpoints are plain GET links on the page.
        await self._client.fetch_page(
            path_fmt.format(application_id=application_id),
            referer=self._client.url(APPLICATIONS_PATH),
        )
        await self._apps.mark_resolved(application_id)
        log.info("application %s: %s submitted", application_id, verb)
