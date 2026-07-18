"""The automation watchdog: the permanent answer to "trainings silently
stopped starting".

Every stall the pipeline has produced so far shared one property: the
request sat in a recoverable state while NOTHING told the operator.
This job runs on the scheduler like any other (always registered, so it
survives updates/restarts and every config state) and each tick:

* checks the board-poll heartbeats (written before the enabled gate, so
  a silent scheduler death is visible),
* finds actionable training/building rows that have sat still too long,
  names WHY (switch off, dry-run, circuit open, login rot, lock busy)
  and — when nothing blocks — re-kicks the queue itself,
* reports each distinct reason to the admin log with a per-reason
  cooldown, so it nags without flooding.

It only reads shared state and calls the same execute path the poller
uses (under the same job lock) — it can never double-start a class.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging

from ..db.repos import AutomationRepo, StateRepo

log = logging.getLogger(__name__)

#: A poll heartbeat older than max(3 * interval, this floor) is "dead".
HEARTBEAT_FLOOR_MINUTES = 15
#: An actionable row untouched for this long counts as stuck.
STUCK_AGE_MINUTES = 30
#: Repeat a given alert at most once per this window.
NOTICE_COOLDOWN_MINUTES = 360
#: Re-check the MC session at most this often (it costs one paced fetch).
LOGIN_CHECK_MINUTES = 180

_KINDS = ("training", "building")


class AutomationWatchdog:
    def __init__(self, cfg, db, bot) -> None:
        self.cfg = cfg
        self.bot = bot
        self.requests = AutomationRepo(db)
        self.state = StateRepo(db)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _age_minutes(raw: str | None, now: dt.datetime) -> float | None:
        if not raw:
            return None
        try:
            then = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=dt.timezone.utc)
        return (now - then).total_seconds() / 60.0

    def _service(self, kind: str):
        return self.bot.trainings if kind == "training" else self.bot.buildings

    def _switch_on(self, kind: str) -> bool:
        auto = self.cfg.automation
        return auto.training.enabled if kind == "training" else auto.building.enabled

    def _interval_minutes(self, kind: str) -> int:
        auto = self.cfg.automation
        return auto.training.interval if kind == "training" else auto.building.interval

    # -- the tick ----------------------------------------------------------

    async def run(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        reasons: list[tuple[str, str]] = []

        for kind in _KINDS:
            reasons += await self._check_kind(kind, now)

        for key, reason in reasons:
            await self._notify(key, reason, now)

    async def _check_kind(
        self, kind: str, now: dt.datetime
    ) -> list[tuple[str, str]]:
        """(stable cooldown key, message) pairs — the key must NOT contain
        ages/counts, or the cooldown resets every tick and the alert spams."""
        reasons: list[tuple[str, str]] = []

        # 1) Is the poll job alive at all? The heartbeat is written before
        #    the enabled gate, so "no heartbeat" means the scheduler job is
        #    dead/wedged — not that the switch is off.
        heartbeat_age = self._age_minutes(
            await self.state.get(f"heartbeat:board-{kind}"), now
        )
        stale_after = max(
            HEARTBEAT_FLOOR_MINUTES, 3 * self._interval_minutes(kind)
        )
        if heartbeat_age is not None and heartbeat_age > stale_after:
            # The fired-stamp (written by _guarded before its lock check)
            # splits the diagnosis: fresh = the scheduler fires but the
            # poll can't run (lock held: a very long poll or a wedged
            # holder); stale/absent = the scheduler job itself is dead.
            fired_age = self._age_minutes(
                await self.state.get(f"heartbeat:fired:board-{kind}s"), now
            )
            if fired_age is not None and fired_age <= stale_after:
                reasons.append((
                    f"heartbeat-lock:{kind}",
                    f"the board-{kind} poll has not completed for "
                    f"{heartbeat_age:.0f} min although its job keeps "
                    "firing — the job lock is held (a very long poll, or "
                    "a wedged holder); if it persists, a restart clears it",
                ))
            else:
                reasons.append((
                    f"heartbeat:{kind}",
                    f"the board-{kind} poll has not run for "
                    f"{heartbeat_age:.0f} min (interval "
                    f"{self._interval_minutes(kind)} min) — the scheduler "
                    "job looks dead; a restart clears it",
                ))

        # 2) Stuck actionable rows: pending/waiting, due, untouched.
        stuck = await self.requests.stuck_actionable(
            kind, older_minutes=STUCK_AGE_MINUTES
        )
        if not stuck:
            return reasons
        oldest = max(
            self._age_minutes(row["updated_at"], now) or 0 for row in stuck
        )
        label = f"{len(stuck)} {kind} request(s) (oldest ~{oldest:.0f} min)"

        if not self._switch_on(kind):
            reasons.append((
                f"switch-off:{kind}",
                f"{label} are queued but `automation.{kind}.enabled` is "
                f"OFF — turn it on with `!fra set {kind}.enabled on`",
            ))
            return reasons
        if getattr(self.bot.pacer, "circuit_open", False):
            reasons.append((
                f"circuit:{kind}",
                f"{label} are waiting while the MissionChief circuit "
                "breaker is open (repeated fetch failures) — it resumes "
                "by itself after the cooldown",
            ))
            return reasons
        if await self._login_rotten(now):
            reasons.append((
                f"login:{kind}",
                f"{label} are waiting but the MissionChief login/session "
                "looks broken — check the credentials / re-login",
            ))
            return reasons

        # Nothing identifiable blocks: kick the queue ourselves, under the
        # same lock as the poller so nothing can double-run.
        kicked = await self._kick(kind)
        if kicked is None:
            reasons.append((
                f"lock:{kind}",
                f"{label} are due but the board-{kind} job lock has been "
                "busy for minutes — a holder may be wedged; a restart "
                "clears it",
            ))
        elif kicked == 0:
            # The queue ran and still didn't take them: attempts/backoff
            # decide — that state is visible via !fra automation.
            log.info(
                "watchdog: %s stuck row(s) for %s, queue kick executed "
                "nothing (backoff/claims apply)", len(stuck), kind,
            )
        else:
            log.warning(
                "watchdog: re-kicked the %s queue (%d stuck row(s), "
                "%d executed)", kind, len(stuck), kicked,
            )
        return reasons

    async def _login_rotten(self, now: dt.datetime) -> bool:
        """A cached MC session check, at most once per LOGIN_CHECK window."""
        last = self._age_minutes(await self.state.get("watchdog:login_check"), now)
        if last is not None and last < LOGIN_CHECK_MINUTES:
            return (await self.state.get("watchdog:login_ok")) == "no"
        ok = True
        try:
            ok = await self.bot.mc.verify_session()
        except Exception:  # noqa: BLE001 — an errored check is not proof
            ok = True
        await self.state.set("watchdog:login_check", now.isoformat())
        await self.state.set("watchdog:login_ok", "yes" if ok else "no")
        return not ok

    async def _kick(self, kind: str) -> int | None:
        """Run the kind's queue under its job lock; None when the lock
        stayed busy (a poll may legitimately hold it for a while)."""
        lock = self.bot.job_lock(f"board-{kind}s")
        try:
            await asyncio.wait_for(lock.acquire(), timeout=180.0)
        except asyncio.TimeoutError:
            return None
        try:
            return await self._service(kind).execute_queue_now()
        except Exception:  # noqa: BLE001 — a failed kick is retried next tick
            log.exception("watchdog: %s queue kick failed", kind)
            return 0
        finally:
            lock.release()

    async def _notify(self, key: str, reason: str, now: dt.datetime) -> None:
        """One admin-log line per distinct reason KEY, cooled down."""
        key = "watchdog:notice:" + hashlib.sha1(
            key.encode("utf-8")
        ).hexdigest()[:10]
        last = self._age_minutes(await self.state.get(key), now)
        if last is not None and last < NOTICE_COOLDOWN_MINUTES:
            return
        await self.state.set(key, now.isoformat())
        log.warning("watchdog: %s", reason)
        channel = self.bot.channel_for("admin_log")
        if channel is None:
            return
        try:
            await channel.send(f"🩺 **Automation watchdog:** {reason}"[:1900])
        except Exception:  # noqa: BLE001 — the alert itself must never crash
            log.exception("watchdog: admin notice failed")
