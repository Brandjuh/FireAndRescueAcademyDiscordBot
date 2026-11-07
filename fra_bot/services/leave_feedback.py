"""Ask members who LEFT the alliance (not kicked) why they left.

Source of truth is the alliance log: ``left_alliance`` rows are a member's
own decision, ``kicked_from_alliance`` rows (including the tax auto-kick)
are not and never get this PM. The roster diff can't tell the two apart.

Per leave, once the delay has passed (default an hour):

* skipped when the player is back in the roster (left and rejoined) — but
  only by a roster sweep that STARTED after the leave. The sweep runs every
  couple of hours, so right after a leave the stored roster still lists the
  player; until a newer sweep has run the leave just waits;
* skipped when the player turned bot PMs off (``/ingame-pms off``);
* one PM per LEAVE: a player who comes back and leaves again is asked
  again, but never twice about the same leave — a new leave only counts
  when it is logged AFTER the moment we last asked (a later "left" row
  can only exist if they rejoined in between). Two leaves inside one
  delay window therefore get a single PM;
* otherwise one in-game PM (English) with a short multiple-choice question.

Replies arrive as ordinary in-game PMs, so the DM mirror puts them in the
Discord DM forum where staff can read and answer them; the send itself is
mirrored at once through the ``mirror`` hook (outgoing-only conversations
may never show up on the inbox page the mirror scan reads).

Never retroactive: on the first run a watermark is set at the newest log
row, so only leaves from then on are asked, and a leave older than
:data:`MAX_AGE_HOURS` (an outage, a late log sync) is skipped. Honours the
global ``dry_run``. Always scheduled; ``automation.leave_feedback.enabled``
is read live.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from ..config import Config
from ..db.database import Database
from ..db.repos import (
    MemberActionsRepo, MemberPrefsRepo, MembersRepo, RunsRepo, StateRepo,
)
from ..mc.client import MissionChiefClient
from ..mc.messages import UNCONFIRMED_PREFIX

log = logging.getLogger(__name__)

SUBJECT = "Sorry to see you go — quick question from FRA"
BODY = """Hi {name},

We noticed you left Fire & Rescue Academy. No hard feelings — thanks for the time you spent with us!

Could you tell us why you left? Even one line helps us make the alliance better. Just reply to this message, for example with a number:

1. I joined another alliance
2. Not enough missions or events
3. The 5% contribution or the alliance rules
4. I couldn't get the training or buildings I needed
5. I stopped playing or I'm taking a break
6. Something else — tell us!

You're always welcome back — you can re-apply any time.

Best regards,
Fire & Rescue Academy staff"""

#: A leave older than this is never asked about (first enable, outage).
MAX_AGE_HOURS = 48
#: PMs per pass; a rush of leaves paces out over the next passes.
MAX_PER_PASS = 3
#: Hard send failures (the game rejected the PM) are retried this often.
MAX_SEND_ATTEMPTS = 3

WATERMARK_KEY = "leave_feedback:after_log_id"


def _asked_key(mc_user_id: int) -> str:
    return f"leave_feedback:asked:{mc_user_id}"


def _done_key(log_id: int) -> str:
    return f"leave_feedback:done:{log_id}"


def _attempts_key(log_id: int) -> str:
    return f"leave_feedback:attempts:{log_id}"


def _parse(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


class LeaveFeedbackService:
    #: Seconds between two PMs in one pass (on top of the pacer's delays).
    send_spacing = 60.0
    #: Optional hook (set by the bot): mirror_now(conversation_id, username,
    #: subject) — puts the conversation in the DM forum at send time.
    mirror = None

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db
        self.members = MembersRepo(db)
        self.prefs = MemberPrefsRepo(db)
        self.state = StateRepo(db)
        self.actions = MemberActionsRepo(db)
        self.runs = RunsRepo(db)

    @property
    def settings(self):
        return self.cfg.automation.leave_feedback

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    async def run(self) -> list[str]:
        """One pass; returns admin summary lines ([] = nothing done)."""
        if not self.settings.enabled:
            return []
        try:
            return await self._pass(dt.datetime.now(dt.timezone.utc))
        except Exception:  # noqa: BLE001 — a pass must never kill the job loop
            log.exception("leave feedback pass failed")
            return []

    async def _pass(self, now: dt.datetime) -> list[str]:
        watermark = await self.state.get(WATERMARK_KEY)
        if watermark is None:
            # First run: start from the newest log row — never retroactive.
            newest = await self._newest_log_id()
            await self.state.set(WATERMARK_KEY, str(newest))
            log.info("leave feedback: enabled, watching leaves after log #%d", newest)
            return []

        rows = await self._leaves_after(int(watermark))
        if not rows:
            return []
        delay = dt.timedelta(minutes=max(0, int(self.settings.delay_minutes)))
        active = None
        roster_at: dt.datetime | None = None
        lines: list[str] = []
        sent = 0
        oldest_open: int | None = None

        for row in rows:
            log_id = int(row["id"])
            if await self.state.get(_done_key(log_id)) is not None:
                continue
            name = (row["executed_name"] or "").strip()
            mc_id = row["executed_mc_id"]
            left_at = _parse(row["event_at"]) or _parse(row["scraped_at"]) or now
            if not name:
                await self._done(log_id, "no player name in the log row")
                continue
            if now - left_at < delay:
                oldest_open = log_id if oldest_open is None else oldest_open
                continue                       # not yet — next pass
            if now - left_at > dt.timedelta(hours=MAX_AGE_HOURS):
                await self._done(log_id, "too old")
                continue
            if mc_id is not None:
                asked_at = _parse(await self.state.get(_asked_key(int(mc_id))))
                if asked_at is not None and left_at <= asked_at:
                    await self._done(log_id, "already asked about this leave")
                    continue
            if active is None:
                active = await self.members.active_members()
                last = await self.runs.last_success("members")
                roster_at = _parse(last["started_at"]) if last else None
            if mc_id is not None and int(mc_id) in active:
                if roster_at is None or roster_at <= left_at:
                    # The roster predates the leave: "active" there only
                    # means "not swept since" — wait for a newer sweep.
                    oldest_open = log_id if oldest_open is None else oldest_open
                    continue
                await self._done(log_id, "rejoined")
                lines.append(f"↩️ {name} left and is back already — not asked")
                continue
            if not await self.prefs.ingame_pms_enabled(mc_id):
                await self._done(log_id, "bot PMs turned off")
                continue
            if sent >= MAX_PER_PASS:
                oldest_open = log_id if oldest_open is None else oldest_open
                continue                       # pacing — next pass
            if sent and self.send_spacing:
                await asyncio.sleep(self.send_spacing)
            line, retry = await self._ask(log_id, name, mc_id)
            sent += 1
            lines.append(line)
            if retry:
                oldest_open = log_id if oldest_open is None else oldest_open

        # Keep the scan window small: everything up to the oldest row still
        # waiting (delay / pacing / retry) is settled.
        settled = (oldest_open - 1) if oldest_open is not None else int(rows[-1]["id"])
        if settled > int(watermark):
            await self.state.set(WATERMARK_KEY, str(settled))
        return lines

    async def _ask(self, log_id: int, name: str, mc_id) -> tuple[str, bool]:
        """Send (or, in dry run, report) the PM. Returns (summary line,
        should-retry)."""
        if self.dry_run:
            await self._done(log_id, "dry run")
            return f"🧪 dry run — would ask {name} why they left", False
        from ..mc.messages import send_new_message

        try:
            ok, detail, conversation_id = await send_new_message(
                self.client, name, SUBJECT, BODY.format(name=name)
            )
        except Exception as exc:  # noqa: BLE001
            ok, detail, conversation_id = False, str(exc), None
        unconfirmed = not ok and detail.startswith(UNCONFIRMED_PREFIX)
        if ok or unconfirmed:
            # An unconfirmed send probably landed — never resend it.
            if mc_id is not None:
                await self.state.set(_asked_key(int(mc_id)), dt.datetime.now(
                    dt.timezone.utc).isoformat(timespec="seconds"))
            await self._done(log_id, "asked")
            await self._log_action(mc_id, name, unconfirmed)
            if self.mirror is not None and conversation_id:
                try:
                    await self.mirror(conversation_id, name, SUBJECT)
                except Exception:  # noqa: BLE001 — mirroring is best-effort
                    log.exception("leave feedback: could not mirror the PM")
            suffix = " (delivery not confirmed by the game)" if unconfirmed else ""
            return f"📨 asked {name} why they left{suffix}", False

        attempts = int(await self.state.get(_attempts_key(log_id)) or 0) + 1
        await self.state.set(_attempts_key(log_id), str(attempts))
        if attempts >= MAX_SEND_ATTEMPTS:
            await self._done(log_id, f"gave up: {detail[:80]}")
            return f"⚠️ could not PM {name} — gave up ({detail[:80]})", False
        return (f"⚠️ could not PM {name} ({detail[:80]}) — retry "
                f"{attempts}/{MAX_SEND_ATTEMPTS - 1}"), True

    async def _log_action(self, mc_id, name: str, unconfirmed: bool) -> None:
        try:
            await self.actions.log(
                discord_user_id=None,
                mc_user_id=int(mc_id) if mc_id is not None else None,
                actor_name=name,
                action="leave_feedback_asked",
                detail="asked why they left the alliance (in-game PM)"
                       + (" — delivery not confirmed" if unconfirmed else ""),
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not stop sends
            log.exception("leave feedback: member action log failed")

    async def _done(self, log_id: int, why: str) -> None:
        await self.state.set(_done_key(log_id), why)

    async def _newest_log_id(self) -> int:
        async with self.db.conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS n FROM alliance_logs"
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"])

    async def _leaves_after(self, log_id: int):
        async with self.db.conn.execute(
            "SELECT id, executed_name, executed_mc_id, event_at, scraped_at "
            "FROM alliance_logs WHERE action_key = 'left_alliance' AND id > ? "
            "ORDER BY id",
            (log_id,),
        ) as cur:
            return await cur.fetchall()
