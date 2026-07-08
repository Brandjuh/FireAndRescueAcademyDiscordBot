"""Level up + extend alliance hospitals and prisons — on command.

For every alliance hospital and prison the bot:

* raises the hospital **level** toward the maximum, and
* buys every available **extension EXCEPT the last "large" one** (the
  "Large Hospital" / "Large Prison" final expansion is left out).

Prisons reveal their next extension only after the previous is built, so
each purchase re-fetches the page and takes the next offer — the chain
walks itself.

Real endpoints (from the reference bot):
* extensions: ``POST /buildings/<id>/extension/credits/<extId>`` (CSRF token),
* hospital level: ``GET /buildings/<id>/expand_do/credits?level=<max-1>``.

Safety: spends alliance credits, so it is gated on the live funds floor
(``min_alliance_funds``) before every purchase and previews (no writes)
unless the caller explicitly executes. Bounded by an action cap per run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Config
from ..db.database import Database
from ..db.repos import RunsRepo
from ..mc.client import MissionChiefClient
from ..mc.errors import MissionChiefError
from ..mc.parsers.academy import find_next_page_path
from ..mc.parsers.building_detail import (
    parse_alliance_building_kinds,
    parse_csrf_token,
    parse_current_level,
    parse_extension_offers,
)
from ..mc.parsers.funds import parse_total_funds

log = logging.getLogger(__name__)

ALLIANCE_BUILDINGS_PATH = "/verband/gebauede"
KASSE_PATH = "/verband/kasse"
MAX_LIST_PAGES = 15

# The "large" final extension to skip, per type (from the reference bot).
HOSPITAL_LARGE_EXT_ID = 9
PRISON_LARGE_EXT_ID = 30
PRISON_LARGE_PRICE = 200_000

DEFAULT_MAX_HOSPITAL_LEVEL = 20
DEFAULT_MAX_ACTIONS = 25          # bounded chunk per invocation; re-run for more
_PER_BUILDING_GUARD = 60          # hard stop against a stuck re-fetch loop


@dataclass
class UpgradeReport:
    mode: str                      # "PREVIEW" | "LIVE"
    funds: int | None = None
    buildings_seen: int = 0
    levels_raised: int = 0
    extensions_bought: int = 0     # bought (LIVE) or would-buy (PREVIEW)
    errors: int = 0
    actions: int = 0
    funds_blocked: bool = False
    truncated: bool = False
    lines: list[str] = field(default_factory=list)

    def summary(self, *, floor: int) -> str:
        head = [
            f"🏗️ Building upgrade — **{self.mode}**",
            f"Alliance hospitals/prisons seen: {self.buildings_seen} · "
            f"levels: {self.levels_raised} · extensions: {self.extensions_bought}"
            + (f" · errors: {self.errors}" if self.errors else ""),
        ]
        if self.funds is not None:
            head.append(f"Alliance funds: {self.funds:,} (floor {floor:,})")
        if self.funds_blocked:
            head.append("⏳ Stopped early — funds hit the floor.")
        if self.truncated:
            head.append(f"✂️ Reached the {DEFAULT_MAX_ACTIONS}-action cap — run again to continue.")
        body = self.lines[:40]
        if len(self.lines) > 40:
            body.append(f"…and {len(self.lines) - 40} more.")
        return "\n".join(head + [""] + body)


class BuildingUpgradeService:
    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self._auto = cfg.automation.building
        self.runs = RunsRepo(db)
        self._max_level = DEFAULT_MAX_HOSPITAL_LEVEL

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    async def upgrade_all(
        self, *, execute: bool, max_actions: int = DEFAULT_MAX_ACTIONS
    ) -> UpgradeReport:
        """Preview (``execute=False``) or carry out the upgrades. Preview never
        writes; execute spends credits and honours the funds floor + cap."""
        report = UpgradeReport(mode="LIVE" if execute else "PREVIEW")
        run_id = await self.runs.start("building_upgrade")
        try:
            report.funds = await self._live_funds()
            targets = await self._alliance_targets()
            report.buildings_seen = len(targets)
            for b in targets:
                if execute and report.actions >= max_actions:
                    report.truncated = True
                    break
                try:
                    if execute:
                        await self._upgrade_live(b, report, max_actions)
                    else:
                        await self._preview(b, report)
                except MissionChiefError as exc:
                    report.errors += 1
                    report.lines.append(f"❌ {b.name} (#{b.building_id}): {exc}")
                if report.funds_blocked:
                    break
            await self.runs.finish(
                run_id, status="success",
                rows_parsed=report.buildings_seen, rows_new=report.actions,
            )
        except MissionChiefError as exc:
            report.lines.append(f"❌ could not list alliance buildings: {exc}")
            await self.runs.finish(run_id, status="failed", message=str(exc))
        return report

    # -- targets ---------------------------------------------------------

    async def _alliance_targets(self) -> list:
        seen: set[int] = set()
        targets: list = []
        path = ALLIANCE_BUILDINGS_PATH
        for _ in range(MAX_LIST_PAGES):
            html = await self.client.fetch_page(path)
            for b in parse_alliance_building_kinds(html):
                if b.kind in ("hospital", "prison") and b.building_id not in seen:
                    seen.add(b.building_id)
                    targets.append(b)
            nxt = find_next_page_path(html)
            if not nxt:
                break
            path = nxt
        return targets

    # -- preview ---------------------------------------------------------

    async def _preview(self, b, report: UpgradeReport) -> None:
        html = await self.client.fetch_page(f"/buildings/{b.building_id}")
        intents: list[str] = []
        if b.kind == "hospital":
            level = parse_current_level(html)
            if level is not None and level < self._max_level:
                intents.append(f"raise level {level}→{self._max_level}")
                report.levels_raised += 1
        offers = self._eligible(parse_extension_offers(html, b.building_id), b.kind)
        if offers:
            ids = ", ".join(str(o.ext_id) for o in offers)
            intents.append(f"buy extension(s) [{ids}]")
            report.extensions_bought += len(offers)
        if intents:
            report.lines.append(
                f"📝 {b.name} (#{b.building_id}): would " + "; ".join(intents)
            )
        else:
            report.lines.append(f"✅ {b.name} (#{b.building_id}): up to date")

    # -- live ------------------------------------------------------------

    async def _upgrade_live(self, b, report: UpgradeReport, max_actions: int) -> None:
        attempted: set[int] = set()
        raised_level = False
        level_before: int | None = None
        guard = 0
        while report.actions < max_actions and guard < _PER_BUILDING_GUARD:
            guard += 1
            html = await self.client.fetch_page(f"/buildings/{b.building_id}")
            token = parse_csrf_token(html)

            if level_before is not None:
                # Verify the level GET actually landed: MissionChief answers a
                # refused upgrade (funds, already max) with a 200 re-render,
                # so only a level that really moved counts as raised.
                level_after = parse_current_level(html)
                if level_after is not None and level_after > level_before:
                    report.levels_raised += 1
                    report.lines.append(
                        f"✅ {b.name}: raised level {level_before} → {level_after}"
                    )
                else:
                    report.errors += 1
                    report.lines.append(
                        f"⚠️ {b.name}: level upgrade did not take "
                        f"(still {level_after if level_after is not None else '?'}) "
                        "— check alliance funds"
                    )
                level_before = None

            if b.kind == "hospital" and not raised_level:
                level = parse_current_level(html)
                if level is not None and level < self._max_level:
                    if not await self._funds_ok(report):
                        report.funds_blocked = True
                        report.lines.append(f"⏳ {b.name}: funds below floor — stopped")
                        return
                    if await self._raise_level(b.building_id):
                        report.actions += 1
                        level_before = level  # verified on the next re-fetch
                    else:
                        report.errors += 1
                        report.lines.append(f"❌ {b.name}: level upgrade rejected")
                        return
                    raised_level = True
                    continue  # re-fetch with the new level

            offers = self._eligible(parse_extension_offers(html, b.building_id), b.kind)
            offers = [o for o in offers if o.ext_id not in attempted]
            if not offers:
                return  # nothing eligible left on this building
            nxt = offers[0]
            if not await self._funds_ok(report, price=nxt.price):
                report.funds_blocked = True
                report.lines.append(f"⏳ {b.name}: funds below floor — stopped")
                return
            attempted.add(nxt.ext_id)
            if await self._buy_extension(b.building_id, nxt.href, token):
                report.actions += 1
                report.extensions_bought += 1
                price = f" ({nxt.price:,})" if nxt.price else ""
                report.lines.append(f"✅ {b.name}: bought extension {nxt.ext_id}{price}")
            else:
                report.errors += 1
                report.lines.append(f"❌ {b.name}: extension {nxt.ext_id} rejected")
                return

    # -- eligibility -----------------------------------------------------

    def _eligible(self, offers: list, kind: str) -> list:
        """Drop the "large" final extension per the alliance's policy."""
        if kind == "hospital":
            return [o for o in offers if o.ext_id != HOSPITAL_LARGE_EXT_ID]
        if kind == "prison":
            return [
                o for o in offers
                if o.ext_id != PRISON_LARGE_EXT_ID and o.price != PRISON_LARGE_PRICE
            ]
        return list(offers)

    # -- MissionChief actions -------------------------------------------

    async def _raise_level(self, building_id: int) -> bool:
        target = max(0, self._max_level - 1)
        try:
            await self.client.fetch_page(
                f"/buildings/{building_id}/expand_do/credits?level={target}",
                referer=self.client.url(f"/buildings/{building_id}"),
            )
            return True
        except MissionChiefError as exc:
            log.warning("level upgrade for %s failed: %s", building_id, exc)
            return False

    async def _buy_extension(self, building_id: int, href: str, token: str | None) -> bool:
        data = {"authenticity_token": token} if token else {}
        status, _, _ = await self.client.post_form(
            href, data, ajax=True, csrf_token=token,
            referer=self.client.url(f"/buildings/{building_id}"),
        )
        if status >= 400:
            log.warning("extension purchase %s failed with HTTP %s", href, status)
            return False
        return True

    async def _funds_ok(self, report: UpgradeReport, *, price: int | None = None) -> bool:
        """True when funds stay at/above the floor after a spend of ``price``."""
        funds = await self._live_funds()
        report.funds = funds
        if funds is None:
            return False  # can't confirm — refuse to spend
        return funds - (price or 0) >= self._auto.min_alliance_funds

    async def _live_funds(self) -> int | None:
        try:
            html = await self.client.fetch_page(KASSE_PATH)
        except MissionChiefError as exc:
            log.warning("building upgrade funds check failed: %s", exc)
            return None
        return parse_total_funds(html)
