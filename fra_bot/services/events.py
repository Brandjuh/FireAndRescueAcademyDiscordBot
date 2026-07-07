"""Alliance event / large scale mission starting from board requests
(thread 15293).

Members post a location; we geocode it and start a large scale mission
there "as soon as it can" — i.e. respecting MissionChief's free-start
cooldown. A request that can't start yet becomes ``waiting`` with a
``next_attempt_at`` set to the next free window, and the poller retries.

The HTTP form path is used (simpler and more reliable than the browser),
with a hard free-only guard so the bot can never spend coins.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..db.database import utcnow_iso
from ..geo.geocoder import GeocodeError, Geocoder
from ..geo.maps_links import find_maps_links
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.board import BoardPost
from ..mc.parsers.events import (
    EVENT_KINDS,
    build_event_payload,
    is_free_submit,
    next_free_at,
    parse_event_form,
)
from .board_requests import BoardRequestService

log = logging.getLogger(__name__)

# Board requests start a large scale alliance mission at the location.
REQUEST_KIND = "large"


class EventsService(BoardRequestService):
    kind = "event"

    def __init__(
        self,
        cfg: Config,
        client: MissionChiefClient,
        db: Database,
        geocoder: Geocoder,
    ) -> None:
        super().__init__(cfg, client, db)
        self._auto = cfg.automation.events
        self._geocoder = geocoder

    @property
    def thread_id(self) -> int:
        return self._auto.thread_id

    async def handle_post(self, post: BoardPost) -> None:
        location_text = self._extract_location(post.content)
        if not location_text:
            return

        request_id = await self.create_request(post, payload={"location": location_text})

        rate = await self.contribution_rate(post.author_mc_id)
        if rate is not None and rate < self._auto.min_contribution_rate:
            await self.requests.set_status(
                request_id, "skipped",
                f"contribution {rate:g}% below {self._auto.min_contribution_rate:g}%",
            )
            await self.reply(
                f"@{post.author_name}: event request not accepted — your alliance "
                f"contribution ({rate:g}%) is below {self._auto.min_contribution_rate:g}%."
            )
            return

        try:
            resolved = await self._resolve(location_text)
        except GeocodeError as exc:
            await self.requests.set_status(request_id, "failed", f"geocoding failed: {exc}")
            await self.reply(
                f"@{post.author_name}: I couldn't locate \"{location_text}\" ({exc})."
            )
            return

        payload = {
            "location": location_text,
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "address": resolved.address,
        }
        await self._attempt_start(request_id, post.author_name, payload)

    async def retry_waiting(self, request: aiosqlite.Row) -> None:
        payload = json.loads(request["payload"] or "{}")
        if not payload.get("latitude"):
            return
        await self._attempt_start(
            request["id"], request["requester_name"], payload, announce=False
        )

    # ------------------------------------------------------------------

    _PREFIX_RE = re.compile(
        r"^\s*(event|events|mission|missions|alliance event|"
        r"large scale mission|location|request)\s*[:\-]\s*(.+)",
        re.IGNORECASE,
    )

    def _extract_location(self, content: str) -> str | None:
        """Only treat a post as a request when it clearly is one.

        A maps link, or an explicit ``event:``/``location:`` prefix — NOT
        arbitrary chatter, so we never geocode "thanks everyone" and
        accidentally start a mission at some incidental place name.
        """
        links = find_maps_links(content)
        if links:
            return links[0]
        text = re.sub(r"\s+", " ", content).strip()
        match = self._PREFIX_RE.match(text)
        if not match:
            return None
        located = match.group(2).strip()
        return located[:180] or None

    async def _resolve(self, location_text: str):
        if find_maps_links(location_text):
            return await self._geocoder.resolve_maps_link(location_text)
        return await self._geocoder.search(location_text)

    async def _attempt_start(
        self, request_id: int, requester: str | None, payload: dict, *, announce: bool = True
    ) -> None:
        new_path = EVENT_KINDS[REQUEST_KIND]["new_path"]
        lat, lng = payload["latitude"], payload["longitude"]

        try:
            form = parse_event_form(
                await self.client.fetch_page(f"{new_path}?tlat={lat}&tlng={lng}")
            )
        except MissionChiefError as exc:
            await self.requests.set_status(
                request_id, "waiting", f"could not load event form ({exc}); will retry",
                payload=json.dumps(payload), bump_attempts=True, announce=False,
            )
            return

        # Cooldown: is a free start available yet?
        eligible_at = next_free_at(REQUEST_KIND, form.last_free_at)
        if eligible_at and eligible_at > utcnow_iso():
            await self.requests.set_status(
                request_id, "waiting",
                f"next free mission at {eligible_at}; queued",
                payload=json.dumps(payload),
                next_attempt_at=eligible_at,
                announce=announce,
            )
            if announce:
                await self.reply(
                    f"@{requester}: your event at {payload.get('address') or 'the location'} "
                    f"is queued — the next free alliance mission is available at "
                    f"{eligible_at} UTC. I'll start it then."
                )
            return

        if form.action is None or form.authenticity_token is None:
            await self.requests.set_status(
                request_id, "waiting", "event form incomplete; will retry",
                payload=json.dumps(payload), bump_attempts=True, announce=False,
            )
            return

        address = payload.get("address") or ""
        if not is_free_submit(form):
            await self.requests.set_status(
                request_id, "failed",
                "refusing to start: form would spend coins",
                payload=json.dumps(payload),
            )
            return

        if self.dry_run:
            await self.requests.set_status(
                request_id, "skipped",
                f"dry-run: would start mission at {lat:.5f},{lng:.5f}",
                payload=json.dumps(payload),
            )
            await self.reply(
                f"@{requester}: event resolved to {address or 'the location'} "
                f"({lat:.5f}, {lng:.5f}). [dry-run — not started]"
            )
            return

        body = build_event_payload(
            form, kind=REQUEST_KIND, latitude=lat, longitude=lng, address=address
        )
        try:
            status, _, _ = await self.client.post_form(
                form.action,
                body,
                referer=self.client.url(new_path),
                ajax=True,
                csrf_token=form.authenticity_token,
                allow_redirects=False,
            )
        except MissionChiefError as exc:
            await self.requests.set_status(
                request_id, "waiting", f"start request failed ({exc}); will retry",
                payload=json.dumps(payload), bump_attempts=True, announce=False,
            )
            return

        if status >= 400:
            await self.requests.set_status(
                request_id, "failed",
                f"MissionChief rejected the start (HTTP {status})",
                payload=json.dumps(payload),
            )
            await self.reply(
                f"@{requester}: I couldn't start the event automatically "
                f"(HTTP {status}). An admin will handle it."
            )
            return

        await self.requests.set_status(
            request_id, "done",
            f"large scale mission started at {lat:.5f},{lng:.5f}",
            payload=json.dumps(payload),
        )
        await self.reply(
            f"🚨 Large scale alliance mission started for {requester} at "
            f"{address or 'the requested location'}!"
        )
