"""The "Important Alliance Announcements" line, managed from Discord.

Staff save a TEMPLATE (``!fra announce set …``); the bot renders it and
writes the result to the game at once. Placeholders such as
``{free_classes}`` or ``{tip}`` are filled from data the bot already has
(the hourly class-availability walk, the members table, a tip list staff
maintain), so with auto-refresh on (``automation.announcements.enabled``)
the line keeps itself current: every ``interval_minutes`` the template is
re-rendered — with the next tip — and saved when the result differs from
what the game shows.

The game allows 200 characters. A template is checked against that with
today's numbers and the LONGEST tip before it is accepted; a render that
still overflows later (numbers grew) is not saved and staff are told.

While auto-refresh is on the bot owns the line: an edit made in the game
is overwritten at the next refresh — edit through Discord instead.
Everything is written through :mod:`fra_bot.mc.alliance_text`, which
resends every other alliance text untouched and verifies the save.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import Awaitable, Callable

from ..config import Config
from ..db.database import Database
from ..db.repos import MembersRepo, StateRepo
from ..mc.alliance_text import SaveResult, clean_value, save_alliance_texts

log = logging.getLogger(__name__)

#: The game's limit for this field (the form's maxlength; re-checked live
#: on every save).
MAX_CHARS = 200

TEMPLATE_KEY = "announcements:template"
TIPS_KEY = "announcements:tips"
TIP_INDEX_KEY = "announcements:tip_index"
LAST_REFRESH_KEY = "announcements:last_refresh"
LAST_ERROR_KEY = "announcements:last_error"

#: name → what it becomes (shown in `!fra announce`).
PLACEHOLDERS: dict[str, str] = {
    "free_classes": "free training classes per academy type, e.g. Fire 4 · EMS 32",
    "free_total": "all free training classes added up",
    "free_fire": "free fire station classes",
    "free_police": "free police classes",
    "free_ems": "free EMS / rescue classes",
    "free_water": "free water rescue classes",
    "members": "number of alliance members",
    "tip": "the current tip — the next one at every auto-refresh",
    "updated": "time of the refresh, e.g. 20:15 UTC",
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_SHORT_AGENCY = {
    "fire": "Fire",
    "police": "Police",
    "ems": "EMS",
    "coastal": "Water",
    "other": "Other",
}


def placeholders_in(template: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(template or "")


def unknown_placeholders(template: str) -> list[str]:
    return sorted({p for p in placeholders_in(template) if p not in PLACEHOLDERS})


def render(template: str, values: dict[str, str]) -> str:
    """Fill the known placeholders; anything else is left as typed."""
    text = _PLACEHOLDER_RE.sub(
        lambda m: values.get(m.group(1), m.group(0)), template or ""
    )
    # A placeholder that rendered empty (no tips yet) must not leave a
    # double space behind.
    return re.sub(r" {2,}", " ", clean_value("announcements", text))


def free_class_values(availability: dict | None) -> dict[str, str]:
    counts = (availability or {}).get("counts") or {}
    if not counts:
        unknown = "?"
        return {
            "free_classes": unknown, "free_total": unknown, "free_fire": unknown,
            "free_police": unknown, "free_ems": unknown, "free_water": unknown,
        }
    order = [k for k in ("fire", "police", "ems", "coastal", "other") if k in counts]
    parts = [
        f"{_SHORT_AGENCY[k]} {int(counts[k])}" for k in order if int(counts[k]) > 0
    ]
    return {
        "free_classes": " · ".join(parts) or "none",
        "free_total": str(sum(int(v) for v in counts.values())),
        "free_fire": str(int(counts.get("fire", 0))),
        "free_police": str(int(counts.get("police", 0))),
        "free_ems": str(int(counts.get("ems", 0))),
        "free_water": str(int(counts.get("coastal", 0))),
    }


class TemplateError(ValueError):
    """A template or tip staff typed can't be used (reason in the text)."""


class AnnouncementsService:
    def __init__(
        self,
        cfg: Config,
        client,
        db: Database,
        availability: Callable[[], Awaitable[dict | None]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.state = StateRepo(db)
        self.members = MembersRepo(db)
        self._availability = availability
        self._lock = asyncio.Lock()

    @property
    def settings(self):
        return self.cfg.automation.announcements

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    # -- stored template and tips -----------------------------------------

    async def template(self) -> str:
        return await self.state.get(TEMPLATE_KEY) or ""

    async def tips(self) -> list[str]:
        raw = await self.state.get(TIPS_KEY)
        try:
            tips = json.loads(raw) if raw else []
        except ValueError:
            return []
        return [str(t) for t in tips if str(t).strip()] if isinstance(tips, list) else []

    async def _save_tips(self, tips: list[str]) -> None:
        await self.state.set(TIPS_KEY, json.dumps(tips, ensure_ascii=False))

    async def current_tip(self) -> str:
        tips = await self.tips()
        if not tips:
            return ""
        index = int(await self.state.get(TIP_INDEX_KEY) or 0)
        return tips[index % len(tips)]

    async def _advance_tip(self) -> None:
        tips = await self.tips()
        if tips:
            index = int(await self.state.get(TIP_INDEX_KEY) or 0)
            await self.state.set(TIP_INDEX_KEY, str((index + 1) % len(tips)))

    # -- rendering --------------------------------------------------------

    async def values(
        self, *, tip: str | None = None, now: dt.datetime | None = None
    ) -> dict[str, str]:
        now = now or dt.datetime.now(dt.timezone.utc)
        availability = None
        if self._availability is not None:
            try:
                availability = await self._availability()
            except Exception:  # noqa: BLE001 — a missing number must not block
                log.exception("announcements: class availability unavailable")
        values = free_class_values(availability)
        try:
            values["members"] = str(await self.members.active_count())
        except Exception:  # noqa: BLE001
            log.exception("announcements: member count unavailable")
            values["members"] = "?"
        values["tip"] = await self.current_tip() if tip is None else tip
        values["updated"] = now.strftime("%H:%M UTC")
        return values

    async def preview(self, template: str | None = None) -> str:
        if template is None:
            template = await self.template()
        return render(template, await self.values())

    async def check_template(
        self, template: str, tips: list[str] | None = None
    ) -> str:
        """Validate a template; returns today's render. Raises TemplateError.

        With ``{tip}`` in it, every tip is tried and the longest result is
        what has to fit."""
        template = clean_value("announcements", template)
        if not template:
            raise TemplateError("the text is empty")
        unknown = unknown_placeholders(template)
        if unknown:
            raise TemplateError(
                "unknown placeholder(s): "
                + ", ".join("{" + p + "}" for p in unknown)
                + " — see `!fra announce` for the list"
            )
        values = await self.values()
        if tips is None:
            tips = await self.tips()
        candidates = [values["tip"]]
        if "tip" in placeholders_in(template):
            candidates += tips
        longest = max(
            (render(template, {**values, "tip": tip}) for tip in candidates),
            key=len,
        )
        if len(longest) > MAX_CHARS:
            which = " (with the longest tip)" if len(candidates) > 1 else ""
            raise TemplateError(
                f"too long{which}: {len(longest)}/{MAX_CHARS} characters"
            )
        return render(template, values)

    # -- writing ----------------------------------------------------------

    async def live_text(self) -> str | None:
        from ..mc.alliance_text import EDIT_PATH, parse_alliance_text_form

        return parse_alliance_text_form(
            await self.client.fetch_page(EDIT_PATH)
        ).get("announcements")

    async def push(self, text: str, *, restart_clock: bool = True) -> SaveResult:
        if restart_clock:
            # A manual write restarts the auto-refresh clock, so the next
            # automatic one is a full interval away.
            await self.state.set(
                LAST_REFRESH_KEY, dt.datetime.now(dt.timezone.utc).isoformat()
            )
        async with self._lock:
            return await save_alliance_texts(
                self.client, {"announcements": text}, dry_run=self.dry_run
            )

    async def set_template(self, template: str) -> tuple[str, SaveResult]:
        """Store a new template and write its render to the game now."""
        rendered = await self.check_template(template)
        await self.state.set(TEMPLATE_KEY, clean_value("announcements", template))
        return rendered, await self.push(rendered)

    async def refresh_now(self) -> tuple[str, SaveResult]:
        template = await self.template()
        if not template:
            raise TemplateError("no text saved yet — use `!fra announce set <text>`")
        rendered = await self.preview(template)
        return rendered, await self.push(rendered)

    async def add_tip(self, tip: str) -> int:
        tip = clean_value("announcements", tip)
        if not tip:
            raise TemplateError("the tip is empty")
        if "{" in tip or "}" in tip:
            raise TemplateError("a tip can't contain { } placeholders")
        tips = await self.tips()
        template = await self.template()
        if template and "tip" in placeholders_in(template):
            await self.check_template(template, tips=[*tips, tip])
        tips.append(tip)
        await self._save_tips(tips)
        return len(tips)

    async def remove_tip(self, number: int) -> str:
        tips = await self.tips()
        if not 1 <= number <= len(tips):
            raise TemplateError(f"there is no tip #{number} (there are {len(tips)})")
        removed = tips.pop(number - 1)
        await self._save_tips(tips)
        return removed

    # -- the scheduled refresh -------------------------------------------

    async def next_refresh_at(self) -> dt.datetime | None:
        last = await self.state.get(LAST_REFRESH_KEY)
        if not last:
            return None
        try:
            at = dt.datetime.fromisoformat(last)
        except ValueError:
            return None
        return at + dt.timedelta(minutes=int(self.settings.interval_minutes))

    async def run(self, now: dt.datetime | None = None) -> list[str]:
        """One scheduler tick; returns admin lines ([] = nothing to say).

        Quiet when things work — only a failure, and the recovery after
        one, are reported (each once)."""
        if not self.settings.enabled:
            return []
        now = now or dt.datetime.now(dt.timezone.utc)
        template = await self.template()
        if not template:
            return []
        due = await self.next_refresh_at()
        if due is not None and now < due:
            return []
        await self.state.set(LAST_REFRESH_KEY, now.isoformat())
        if due is not None and "tip" in placeholders_in(template):
            await self._advance_tip()

        rendered = render(template, await self.values(now=now))
        if len(rendered) > MAX_CHARS:
            result = SaveResult(
                False, False, f"the text is {len(rendered)}/{MAX_CHARS} characters "
                f"right now, so it was not saved: {rendered}",
            )
        else:
            try:
                result = await self.push(rendered, restart_clock=False)
            except Exception as exc:  # noqa: BLE001 — never kill the job loop
                log.exception("announcements refresh failed")
                result = SaveResult(False, False, f"error: {exc}")
        return await self._report(result)

    async def _report(self, result: SaveResult) -> list[str]:
        lines: list[str] = []
        previous = await self.state.get(LAST_ERROR_KEY)
        if result.ok:
            if previous:
                await self.state.delete(LAST_ERROR_KEY)
                lines.append("✅ auto-refresh works again.")
        elif result.detail != previous:
            await self.state.set(LAST_ERROR_KEY, result.detail)
            lines.append(f"⚠️ auto-refresh failed: {result.detail}")
        if result.collateral:
            lines.append(
                "🚨 the save also changed: " + ", ".join(result.collateral)
                + " — check the alliance texts in the game!"
            )
        return lines
