"""Discord-panel academy builds at a fixed address (overlap allowed).

A member with the configured role clicks Fire / Police / Rescue / Coastal in
the academy panel; the bot builds that academy type at ONE fixed address
(duplicates allowed — no dedup), naming it ``[AA] <label> #N`` where N is the
next number found by scanning the alliance's LIVE building list. When alliance
funds are below the floor the build is QUEUED (an ``automation_requests`` row,
``kind="academy"``) and retried by the queue poller until funds recover.
Honours the global ``dry_run`` (reports what it would build, spends nothing).

This is a hospital/prison build minus the geocode-from-link, dedup and OSM
machinery: fixed location, fixed type per button, custom name. It reuses the
proven :class:`BuildingsService` browser builder, live-funds read and geocoder
rather than duplicating them.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

import aiosqlite

from ..config import Config
from ..db.database import Database
from ..db.repos import AutomationRepo, StateRepo
from ..geo.geocoder import GeocodeError
from ..mc.browser_builder import BrowserBuilder, BrowserUnavailable
from ..mc.errors import MissionChiefError
from ..mc.parsers.academy import find_next_page_path, parse_alliance_buildings_page

log = logging.getLogger(__name__)

KIND = "academy"
BUILDING_KIND = "building"                  # member hospital/prison requests
ALLIANCE_BUILDINGS_PATH = "/verband/gebauede"
MAX_LIST_PAGES = 12
# Transient build/geocode failures are bounded; a funds-wait does NOT bump
# attempts, so a low-funds build can wait as long as it needs to.
MAX_ATTEMPTS = 8

# --- auto-scale: build a new academy when a discipline runs out of classrooms.
# The free-classroom counts come from the trainings availability walk (cached
# in state, refreshed ~hourly); this key must match trainings.py.
AVAILABILITY_STATE_KEY = "training_availability"
AUTOSCALE_STATE_KEY = "academy_autoscale"
# Anti-runaway: a discipline must read 0 free classrooms on this many
# consecutive checks (~1 per hour) before we build, then wait the cooldown
# before another of the same discipline. At most one auto-build is in flight
# at a time, and member hospital/prison requests take priority for the funds.
AUTOSCALE_DEBOUNCE_CHECKS = 2
AUTOSCALE_COOLDOWN_HOURS = 24
# Don't act on availability data older than this (a stale reading could be
# wrong); skip the run instead.
AUTOSCALE_MAX_AVAILABILITY_AGE_S = 3 * 3600
# Training discipline → academy button key.
_DISCIPLINE_TO_ACADEMY = {
    "fire": "fire", "police": "police", "ems": "rescue", "coastal": "coastal",
}

# Button key → build-type key (must exist in BUILDING_TYPE_IDS), display label
# and emoji. The name is "[AA] <label> #N".
# build_key must match a BUILDING_TYPE_IDS key (the lowercased form label);
# label is the display name used in "[AA] <label> #N".
ACADEMIES: dict[str, dict[str, str]] = {
    "fire":    {"build_key": "fire academy",          "label": "Fire academy",          "emoji": "🚒"},
    "police":  {"build_key": "police academy",        "label": "Police academy",        "emoji": "🚓"},
    "rescue":  {"build_key": "rescue (ems) academy",  "label": "Rescue academy",        "emoji": "🚑"},
    "coastal": {"build_key": "coastal rescue school", "label": "Coastal Rescue School", "emoji": "🌊"},
}

# Matches any academy we built via the panel — "[AA] <label> #NNN" — so the
# extension sweep only touches our own academies.
_OUR_ACADEMY_RE = re.compile(r"^\s*\[AA\]\s.+#\d+\s*$", re.IGNORECASE)


class AcademyService:
    def __init__(self, cfg: Config, db: Database, buildings, upgrader=None) -> None:
        self.cfg = cfg
        # Reuse the building service's browser builder, live-funds read and
        # geocoder (an academy build is a building build minus dedup/geocode).
        self._buildings = buildings
        # The building-upgrade service finishes a fresh academy by buying its
        # extensions (all of them — the "skip the large one" rule is
        # hospital/prison-only). Optional so the service works without it.
        self._upgrader = upgrader
        self.requests = AutomationRepo(db)
        self.state = StateRepo(db)
        self._auto = cfg.automation.academy
        self._coords: tuple[float, float, str] | None = None
        self._geocoded_for: str | None = None

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    @property
    def client(self):
        return self._buildings.client

    # -- enqueue + drain -------------------------------------------------

    async def enqueue(
        self, academy_kind: str, *, requester_name: str | None,
        discord_user_id: int | None, channel_id: int | None,
    ) -> int:
        """Create a pending academy-build request; returns its id."""
        if academy_kind not in ACADEMIES:
            raise ValueError(f"unknown academy kind {academy_kind!r}")
        payload = json.dumps({
            "academy": academy_kind,
            "discord_user_id": discord_user_id,
            "channel_id": channel_id,
        })
        return await self.requests.create(
            kind=KIND, thread_id=0, post_id=int(discord_user_id or 0),
            requester_name=requester_name, requester_mc_id=discord_user_id,
            payload=payload,
        )

    async def run_one(self, request_id: int) -> aiosqlite.Row | None:
        """Execute one just-enqueued request NOW (the button's immediate
        feedback), and return its row afterwards."""
        request = await self.requests.get(request_id)
        if request is not None and request["status"] == "pending":
            await self._claim_and_run(request)
        return await self.requests.get(request_id)

    async def process_queue(self) -> int:
        """Execute every claimable academy build (fresh + due funds-waits).
        Driven by the scheduled poller so a queued low-funds build resumes
        automatically once funds recover."""
        rows = await self.requests.claimable(KIND)
        for request in rows:
            await self._claim_and_run(request)
        return len(rows)

    async def _claim_and_run(self, request: aiosqlite.Row) -> None:
        request_id = request["id"]
        if request["attempts"] >= MAX_ATTEMPTS:
            if await self.requests.claim(request_id):
                await self.requests.set_status(
                    request_id, "failed",
                    f"gave up after {request['attempts']} failed attempts",
                )
            return
        first_attempt = request["status"] == "pending"
        if not await self.requests.claim(request_id):
            return  # another poll / the immediate kick won the claim
        try:
            await self._execute(request, announce=first_attempt)
        except MissionChiefError as exc:
            await self.requests.set_status(
                request_id, "waiting",
                f"MissionChief error ({exc}); will retry",
                bump_attempts=True, announce=False,
            )
        except Exception:  # noqa: BLE001 — one build must not crash the loop
            log.exception("academy build %s crashed", request_id)
            current = await self.requests.get(request_id)
            if current is not None and current["status"] == "processing":
                await self.requests.set_status(
                    request_id, "failed", "internal error while building",
                )

    async def _execute(self, request: aiosqlite.Row, *, announce: bool) -> None:
        request_id = request["id"]
        payload = json.loads(request["payload"] or "{}")
        spec = ACADEMIES.get(payload.get("academy"))
        if spec is None:
            await self.requests.set_status(
                request_id, "failed", f"unknown academy {payload.get('academy')!r}",
            )
            return

        # dry-run / no browser: say what WOULD be built, spend nothing.
        if self.dry_run or not BrowserBuilder.available():
            reason = "dry-run" if self.dry_run else "no browser available on the host"
            name = await self._next_name(spec)
            await self.requests.set_status(
                request_id, "skipped",
                f"[{reason}] would build {name} at {self._auto.address}",
            )
            return

        # Member hospital/prison requests take the funds first: an auto-scale
        # academy waits while any are open (a manual panel build is a deliberate
        # choice and does not defer). Enforced here at build time — not only at
        # enqueue — so a member request arriving after the academy was queued
        # still wins the funds.
        if request["requester_name"] == "autoscale":
            member_open = await self.requests.open_count(BUILDING_KIND)
            if member_open:
                await self.requests.set_status(
                    request_id, "waiting",
                    f"{member_open} member building request(s) have priority; "
                    "waiting",
                    announce=False,
                )
                return

        # Funds gate → queue when low (never spend below the floor).
        funds, funds_error = await self._buildings._live_funds()
        if funds is None:
            await self.requests.set_status(
                request_id, "waiting",
                f"could not read alliance funds ({funds_error}); will retry",
                bump_attempts=True, announce=False,
            )
            return
        if funds < self._auto.min_alliance_funds:
            await self.requests.set_status(
                request_id, "waiting",
                f"alliance funds {funds:,} below floor "
                f"{self._auto.min_alliance_funds:,}; queued until funds recover",
                announce=announce,
            )
            return

        coords = await self._coordinates()
        if coords is None:
            await self.requests.set_status(
                request_id, "waiting",
                f"could not geocode {self._auto.address!r}; will retry",
                bump_attempts=True, announce=False,
            )
            return
        latitude, longitude, address = coords
        name = await self._next_name(spec)

        try:
            result = await self._buildings._builder.build(
                building_type=spec["build_key"],
                latitude=latitude, longitude=longitude,
                name=name, address=address,
            )
        except BrowserUnavailable as exc:
            await self.requests.set_status(
                request_id, "waiting",
                f"browser unavailable ({exc}); will retry",
                bump_attempts=True, announce=False,
            )
            return

        merged = dict(payload)
        merged["name"] = name
        if result.ok:
            if result.building_id:
                merged["building_id"] = result.building_id
            detail = (
                f"built {name} (#{result.building_id})" if result.building_id
                else f"built {name} (submitted — {result.detail})"
            )
            await self.requests.set_status(
                request_id, "done", detail, payload=json.dumps(merged), announce=True,
            )
            # Finish it: buy the extensions that are available now. The rest
            # unlock one at a time (~7 days each) and the periodic sweep buys
            # them as they open.
            await self._finish_extensions(name)
        else:
            await self.requests.set_status(
                request_id, "failed", f"{name}: {result.detail}",
                payload=json.dumps(merged), announce=True,
            )

    # -- helpers ---------------------------------------------------------

    async def _coordinates(self) -> tuple[float, float, str] | None:
        """Geocode the configured address and cache it, keyed on the address
        so a live ``!fra set automation.academy.address`` re-geocodes instead
        of silently building at the old spot (Nominatim is also DB-cached, so
        an unchanged address stays a single lookup)."""
        address = self._auto.address
        if self._coords is not None and self._geocoded_for == address:
            return self._coords
        try:
            location = await self._buildings._geocoder.search(address)
        except GeocodeError as exc:
            log.warning("academy: could not geocode %r (%s)", address, exc)
            return None
        self._coords = (
            location.latitude, location.longitude,
            location.address or address,
        )
        self._geocoded_for = address
        return self._coords

    async def _next_name(self, spec: dict[str, str]) -> str:
        # Zero-pad to three digits: "[AA] Fire academy #007".
        return f"[AA] {spec['label']} #{await self._next_number(spec):03d}"

    async def _next_number(self, spec: dict[str, str]) -> int:
        """Scan the alliance's LIVE building list for existing
        ``[AA] <label> #N`` and return ``max(N) + 1`` (1 when none)."""
        pattern = re.compile(
            r"^\s*\[AA\]\s*" + re.escape(spec["label"]) + r"\s*#(\d+)\s*$",
            re.IGNORECASE,
        )
        highest = 0
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_LIST_PAGES):
            html = await self.client.fetch_page(path)
            for listing in parse_alliance_buildings_page(html):
                match = pattern.match(listing.name or "")
                if match:
                    highest = max(highest, int(match.group(1)))
            nxt = find_next_page_path(html)
            if not nxt:
                break
            path = nxt
        return highest + 1

    # -- extensions ------------------------------------------------------

    async def _finish_extensions(self, name: str) -> None:
        """Right after a build, buy the extension(s) available now. Extensions
        unlock one at a time (each ~7 days), so this typically buys the first;
        :meth:`sweep_extensions` buys the rest as they open. The funds floor is
        NOT enforced here — the build itself was already funds-gated, and a
        fresh building is finished in one go (as with hospital/prison
        post-creation)."""
        if self._upgrader is None or self.dry_run:
            return
        try:
            building_id = await self._find_building_id(name)
        except MissionChiefError as exc:
            log.info("academy: could not list buildings to extend %s (%s)", name, exc)
            return
        if building_id is None:
            log.info("academy: %s not listed yet; the sweep will extend it later", name)
            return
        await self._extend_one(building_id, name)

    async def sweep_extensions(self) -> int:
        """Buy any now-available extension on each of our ``[AA]`` academies.
        Scheduled periodically so the sequential (7-day) extensions max out
        over time. Returns the number of extensions bought this run."""
        if self._upgrader is None or self.dry_run:
            return 0
        try:
            academies = await self._list_our_academies()
        except MissionChiefError as exc:
            log.info("academy sweep: could not list buildings (%s)", exc)
            return 0
        bought = 0
        for building_id, name in academies:
            bought += await self._extend_one(building_id, name)
        if bought:
            log.info("academy sweep: bought %d extension(s) across %d academies",
                     bought, len(academies))
        return bought

    async def _extend_one(self, building_id: int, name: str) -> int:
        try:
            report = await self._upgrader.upgrade_one(
                building_id, kind=KIND, name=name, enforce_floor=False,
            )
        except Exception:  # noqa: BLE001 — extending must never crash the caller
            log.exception("academy: extension buy failed for %s", name)
            return 0
        if report.extensions_bought:
            log.info("academy: %s — bought %d extension(s)",
                     name, report.extensions_bought)
        return report.extensions_bought

    async def _find_building_id(self, name: str) -> int | None:
        target = (name or "").strip().casefold()
        async for listing in self._walk_buildings():
            if (listing.name or "").strip().casefold() == target:
                return listing.building_id
        return None

    async def _list_our_academies(self) -> list[tuple[int, str]]:
        """Every academy we built — ``(building_id, name)`` — identified by the
        ``[AA] … #NNN`` naming scheme and an academy discipline."""
        out: list[tuple[int, str]] = []
        seen: set[int] = set()
        async for listing in self._walk_buildings():
            if listing.building_id in seen or listing.discipline is None:
                continue
            if _OUR_ACADEMY_RE.match(listing.name or ""):
                seen.add(listing.building_id)
                out.append((listing.building_id, listing.name))
        return out

    async def _walk_buildings(self):
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_LIST_PAGES):
            html = await self.client.fetch_page(path)
            for listing in parse_alliance_buildings_page(html):
                yield listing
            nxt = find_next_page_path(html)
            if not nxt:
                break
            path = nxt

    # -- auto-scale ------------------------------------------------------

    async def autoscale(self) -> int:
        """Queue a new academy for any discipline that has run out of free
        classrooms. Gated behind ``automation.academy.autoscale``.

        Anti-runaway: a discipline must read 0 free classrooms on
        ``AUTOSCALE_DEBOUNCE_CHECKS`` consecutive runs before we build (and only
        when that reading is COMPLETE — every academy's page was read, so a
        partial outage that looks like 0 can't trigger a build), then a 24h
        per-discipline cooldown — derived from the durable queued request, not a
        best-effort state blob — at most one auto-build in flight at a time, and
        pending member hospital/prison requests take the funds first. Low funds
        just queue the build (retried by the poller)."""
        if not self._auto.autoscale or self.dry_run:
            return 0
        counts, complete = await self._availability_counts()
        if counts is None:
            return 0  # no fresh, trustworthy signal — do nothing rather than guess

        state = await self._load_autoscale_state()
        streak: dict = state.get("zero_streak", {})

        candidates: list[tuple[str, str]] = []
        for discipline, kind in _DISCIPLINE_TO_ACADEMY.items():
            free = counts.get(discipline)
            # Only a trustworthy, complete reading counts; anything else breaks
            # the consecutive-zeros chain (fails toward NOT building).
            if not isinstance(free, int) or not complete.get(discipline):
                streak[discipline] = 0
                continue
            if free > 0:
                streak[discipline] = 0
                continue
            streak[discipline] = streak.get(discipline, 0) + 1
            if streak[discipline] >= AUTOSCALE_DEBOUNCE_CHECKS:
                candidates.append((discipline, kind))

        built = 0
        if candidates:
            member_open = await self.requests.open_count(BUILDING_KIND)
            academy_open = await self.requests.open_count(KIND)
            if member_open:
                log.info("academy autoscale: %d discipline(s) at 0 classrooms, "
                         "but %d member building request(s) open — deferring for "
                         "priority", len(candidates), member_open)
            elif academy_open:
                log.info("academy autoscale: holding — an academy build is still "
                         "in flight (max one at a time)")
            else:
                for discipline, kind in candidates:
                    if await self._recently_autobuilt(kind):
                        continue  # 24h per-discipline cooldown (durable)
                    await self.enqueue(
                        kind, requester_name="autoscale",
                        discord_user_id=0, channel_id=0,
                    )
                    streak[discipline] = 0
                    built = 1
                    log.info("academy autoscale: %s at 0 free classrooms — queued "
                             "a new %s academy", discipline, kind)
                    break  # one per run

        state["zero_streak"] = streak
        await self.state.set(AUTOSCALE_STATE_KEY, json.dumps(state))
        return built

    async def _availability_counts(self) -> tuple[dict | None, dict]:
        """(counts, complete) from the cached availability walk, or
        ``(None, {})`` when there is no fresh, well-formed snapshot."""
        raw = await self.state.get(AVAILABILITY_STATE_KEY)
        if not raw:
            return None, {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, {}
        if not isinstance(data, dict):
            return None, {}
        at = data.get("at")
        if not isinstance(at, (int, float)):
            return None, {}
        age = datetime.datetime.now(datetime.timezone.utc).timestamp() - at
        if age > AUTOSCALE_MAX_AVAILABILITY_AGE_S:
            log.info("academy autoscale: availability data is %.0f min old — "
                     "skipping", age / 60)
            return None, {}
        counts = data.get("counts")
        if not isinstance(counts, dict):
            return None, {}
        complete = data.get("complete")
        # Older snapshots have no completeness → treat as unknown (not complete),
        # so autoscale waits for a fresh, completeness-tagged reading.
        return counts, (complete if isinstance(complete, dict) else {})

    async def _recently_autobuilt(self, kind: str) -> bool:
        """True if an auto-scale academy of this kind was queued within the
        cooldown window. Derived from the durable request row (not a separate
        state blob), so a crash or state-write failure can't un-gate it."""
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=AUTOSCALE_COOLDOWN_HOURS)
        ).isoformat(timespec="seconds")
        for row in await self.requests.recent(100):
            if row["kind"] != KIND or row["requester_name"] != "autoscale":
                continue
            if (row["created_at"] or "") < cutoff:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            if payload.get("academy") == kind:
                return True
        return False

    async def _load_autoscale_state(self) -> dict:
        raw = await self.state.get(AUTOSCALE_STATE_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
