"""Automated member tax (alliance donation) warnings — the reference
bot's MessageManager system, ported.

Every scan (6-hourly) walks the freshly synced member roster:

* a member below the minimum donation (default 5%) gets an in-game PM —
  a friendly reminder first, then two official warnings, at least 7 days
  apart, capped at three;
* brand-new members get a 24h grace period before the first reminder;
* after the third unresolved warning (plus the same gap) the member is
  flagged kick-due in the admin channel — the actual kick only happens
  automatically when ``auto_kick`` is switched on;
* **a member who fixes their donation is reset immediately**: warnings
  stop the moment the roster shows the rate at/above the minimum, and a
  later dip starts over at warning 1. (The old bot never reset, which is
  why it kept warning members who had already fixed their tax.)

Correctness guards (each one exists because its absence sent wrong PMs):

* **stale-roster gate** — rates come from the hourly members sync; when
  that sync has been failing, the stored rates are frozen and a member
  who fixed their donation days ago still looks below-minimum. No
  warning or kick fires while the last successful sync is too old.
* **unconfirmed sends advance the ladder** — the game regularly answers
  a delivered PM without its success flash; resending "until confirmed"
  delivered the same warning multiple times to the same member. Only a
  send the game explicitly REJECTED (error text / HTTP failure) is
  retried.
* **kicks are verified against the roster** — the kick route answers 200
  even when the game silently refuses (missing alliance rights). A
  member still being seen by the members sync well after their "kick"
  gets the trail reopened instead of being considered gone forever.

Honours the global ``dry_run``: reports what it would send, sends
nothing. The member roster comes from the hourly members sync, so a fix
in game is picked up within the hour.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import MembersRepo, RunsRepo, StateRepo, TaxWarningsRepo
from ..mc.client import MissionChiefClient
from ..mc.messages import UNCONFIRMED_PREFIX

log = logging.getLogger(__name__)

MAX_WARNINGS = 3
# Hard send failures (the game rejected the PM, or HTTP/network errors) are
# retried a few times, then abandoned so the bot doesn't retry them every
# pass forever and re-spam the admin channel. Unconfirmed-but-probably-
# delivered sends are NOT retried at all — see the module docstring.
MAX_SEND_ATTEMPTS = 3

# Skip the warning/kick passes when the last successful members sync is
# older than max(2 × sync interval, this floor) — acting on a frozen roster
# warns members who already fixed their donation.
ROSTER_STALE_MINUTES_FLOOR = 120.0

# A kick only counts once the roster has had time to observe it: the member
# must still be seen this long after the kick (plus a margin for a sync that
# was mid-flight during the kick) before the trail is reopened.
KICK_VERIFY_MIN_HOURS = 1.0
KICK_VERIFY_MARGIN_HOURS = 0.5

# The reference bot's in-game PM presets, verbatim.
WARNING_PRESETS: dict[int, tuple[str, str]] = {
    1: (
        "Reminder: Please set your alliance donation to 5%",
        "Hello {username},\n\n"
        "This is a friendly reminder that your alliance donation is currently not set to the required minimum of 5%.\n\n"
        "According to our Code of Conduct, rule 4.1, every member must set their alliance donation to at least 5%. "
        "These funds are used to build hospitals, prisons, and academies that benefit all alliance members.\n\n"
        "It is possible that you simply forgot to set this or were not sure where to find it. No problem, but please "
        "update it as soon as possible.\n\n"
        "How to update your alliance donation:\n\n"
        "1. Open the menu.\n"
        "2. Click on Show Alliance.\n"
        "3. Go to Alliance Funds.\n"
        "4. Set your donation percentage to at least 5%.\n\n"
        "A higher percentage is always appreciated, but 5% is the minimum requirement.\n\n"
        "Thank you for taking care of this.",
    ),
    2: (
        "Warning: Alliance donation below required minimum",
        "Hello {username},\n\n"
        "This is an official warning regarding your alliance donation.\n\n"
        "Your alliance donation is still not set to the required minimum of 5%, even though this is mandatory under "
        "our Code of Conduct, rule 4.1.\n\n"
        "All members are required to contribute at least 5% to the alliance. These contributions are important because "
        "they allow the alliance to build hospitals, prisons, and academies that support every member.\n\n"
        "Please update your alliance donation to at least 5% as soon as possible.\n\n"
        "How to update your alliance donation:\n\n"
        "1. Open the menu.\n"
        "2. Click on Show Alliance.\n"
        "3. Go to Alliance Funds.\n"
        "4. Set your donation percentage to at least 5%.\n\n"
        "Failure to correct this may result in further action.\n\n"
        "Please make sure this is fixed.",
    ),
    3: (
        "Final warning: Alliance donation requirement not met",
        "Hello {username},\n\n"
        "This is a final warning regarding your alliance donation.\n\n"
        "Your alliance donation is still not set to the required minimum of 5%, despite previous reminders and "
        "warnings. This is a direct violation of our Code of Conduct, rule 4.1.\n\n"
        "All members are required to set their alliance donation to at least 5%. This rule exists to make sure everyone "
        "contributes fairly to the growth and support of the alliance.\n\n"
        "You must update your alliance donation to at least 5% immediately.\n\n"
        "How to update your alliance donation:\n\n"
        "1. Open the menu.\n"
        "2. Click on Show Alliance.\n"
        "3. Go to Alliance Funds.\n"
        "4. Set your donation percentage to at least 5%.\n\n"
        "If this is not corrected, sanctions will follow in accordance with the alliance rules.\n\n"
        "This is your final opportunity to fix the issue before action is taken.",
    ),
}


#: Sent to the member RIGHT BEFORE the auto-kick (best-effort — a failed
#: PM never blocks the kick; three warnings were already delivered).
#: Same tone and structure as the warning presets above.
KICK_NOTICE_PRESET = (
    "Final notice: Removed from the alliance",
    "Hello {username},\n\n"
    "Despite three warnings, your alliance donation is still not set to the "
    "required minimum of 5% (Code of Conduct, rule 4.1).\n\n"
    "You are therefore being removed from the Fire & Rescue Academy "
    "alliance.\n\n"
    "You are welcome to reapply once you are willing to meet the donation "
    "requirement. If you believe this is a mistake, please contact an admin "
    "on our Discord server.",
)


def _hours_since(iso: str | None, now: dt.datetime) -> float | None:
    if not iso:
        return None
    try:
        then = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    return (now - then).total_seconds() / 3600.0


class TaxWarningService:
    # Seconds between two warning PMs in one scan (the reference bot's
    # spacing; on top of the pacer's per-request delays).
    send_spacing = 90.0
    # Optional hook (set by the bot): mirror_now(conversation_id, username,
    # subject) — mirrors each sent warning into the DM forum at send time,
    # like the reference bot's _send_message_and_link. Outgoing-only
    # conversations may never appear on the inbox page the scheduled
    # mirror scan reads, so without this hook they stay invisible.
    mirror = None

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self.members = MembersRepo(db)
        self.warnings = TaxWarningsRepo(db)
        from ..db.repos import MemberActionsRepo

        self.actions = MemberActionsRepo(db)
        self.runs = RunsRepo(db)
        self.state = StateRepo(db)
        self._auto = cfg.automation.tax_warnings

    @staticmethod
    def _attempts_key(mc_user_id: int) -> str:
        return f"taxwarn_send_attempts:{mc_user_id}"

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    async def scan(self, *, force: bool = False) -> list[str]:
        """One warning pass. Returns human summary lines (also logged);
        an empty list means nothing needed doing. ``force`` (the manual
        command) runs even when the schedule switch is off."""
        if not force and not self._auto.enabled:
            return []
        run_id = await self.runs.start("tax_warnings")
        lines: list[str] = []
        try:
            lines = await self._scan(force=force)
            await self.runs.finish(
                run_id, status="success", rows_parsed=len(lines),
                message=" | ".join(lines)[:500] or None,
            )
        except Exception as exc:  # noqa: BLE001 — a scan must not crash the loop
            log.exception("tax warning scan failed: %s", exc)
            await self.runs.finish(run_id, status="failed", message=str(exc))
        for line in lines:
            log.info("tax warnings: %s", line)
        return lines

    async def _scan(self, *, force: bool) -> list[str]:
        now = dt.datetime.now(dt.timezone.utc)
        roster = await self.members.active_members()
        lines: list[str] = []

        # 1. Resolve first: anyone with an open warning trail whose CURRENT
        #    rate meets the minimum is reset — warnings stop immediately.
        for row in await self.warnings.all_open():
            member = roster.get(row["mc_user_id"])
            if member is None:
                await self.warnings.clear(row["mc_user_id"])
                lines.append(
                    f"🚪 {row['username'] or row['mc_user_id']}: left the "
                    "alliance — warning trail cleared"
                )
                continue
            rate = member["contribution_rate"]
            if rate is not None and rate >= self._auto.min_rate:
                await self.warnings.mark_resolved(row["mc_user_id"])
                lines.append(
                    f"✅ {member['name']}: donation fixed ({rate:g}%) after "
                    f"warning {row['warning_count']} — warnings reset"
                )

        # 1.5 Stale-roster gate: resolving above is safe on old data (it
        #     only STOPS warnings), but warning or kicking on frozen rates
        #     hits members who already fixed their donation. Skip the
        #     acting passes until the members sync succeeds again.
        age_minutes = await self._roster_age_minutes(now)
        stale_after = max(
            2.0 * float(getattr(getattr(self.cfg, "sync", None),
                                "members_interval", 60) or 60),
            ROSTER_STALE_MINUTES_FLOOR,
        )
        if age_minutes is not None and age_minutes > stale_after:
            lines.append(
                f"⏭️ warning pass skipped: the member roster is "
                f"{age_minutes / 60.0:.1f}h old (is the members sync "
                "failing?) — nobody is warned or kicked on outdated "
                "donation rates"
            )
            return lines

        # 1.6 Verify that earlier kicks actually stuck.
        lines.extend(await self._verify_kicks(roster, now))

        # 2. Warn who is due, worst rate first, capped per run.
        sent = 0
        for member in sorted(
            roster.values(),
            key=lambda m: (m["contribution_rate"] or 0.0, str(m["name"]).casefold()),
        ):
            if sent >= self._auto.max_per_run:
                break
            rate = member["contribution_rate"]
            attempts_key = self._attempts_key(member["mc_user_id"])
            if rate is not None and rate >= self._auto.min_rate:
                # Donation is OK now — clear any earlier give-up so a later dip
                # gets a fresh set of delivery attempts.
                await self.state.delete(attempts_key)
                continue
            if rate is None:
                continue
            hours_member = _hours_since(member["first_seen_at"], now)
            if hours_member is not None and hours_member < self._auto.grace_hours:
                continue  # too new for automated warnings
            state = await self.warnings.get(member["mc_user_id"])
            count = state["warning_count"] if state is not None else 0
            if count >= MAX_WARNINGS:
                line = await self._handle_kick_due(member, state, now)
                if line:
                    lines.append(line)
                continue
            gap = _hours_since(state["last_warning_at"] if state else None, now)
            if gap is not None and gap < self._auto.min_days_between * 24:
                continue  # don't rush the member
            attempts = int(await self.state.get(attempts_key) or 0)
            if attempts >= MAX_SEND_ATTEMPTS:
                # Gave up delivering to this member (reported when the cap was
                # hit). Stay silent until they fix their rate (clears above).
                continue
            level = count + 1
            subject, body = WARNING_PRESETS[level]
            if self.dry_run:
                lines.append(
                    f"📝 [dry-run] would send warning {level} to "
                    f"{member['name']} ({rate:g}%)"
                )
                sent += 1
                continue
            if sent and self.send_spacing:
                # The reference bot spaced warning PMs ~90s apart — a burst
                # of near-identical messages is exactly what bot detection
                # looks for. Keep that behaviour.
                import asyncio

                await asyncio.sleep(self.send_spacing)
            from ..mc.messages import send_new_message

            ok, detail, conversation_id = await send_new_message(
                self.client, str(member["name"]), subject,
                body.format(username=member["name"]),
            )
            # "Unconfirmed" = the POST landed and the game showed NO error,
            # it just didn't prove the delivery. Those PMs almost always DID
            # arrive — resending "until confirmed" is exactly how members got
            # the same "no contribution set" warning several times over.
            # Record it and move on; only explicit rejections are retried.
            unconfirmed = not ok and detail.startswith(UNCONFIRMED_PREFIX)
            if not ok and not unconfirmed:
                # The game rejected the PM (recipient not found, blocked) or
                # the request itself failed — nothing arrived. Count the try;
                # after MAX_SEND_ATTEMPTS, give up instead of retrying forever.
                attempts += 1
                await self.state.set(attempts_key, str(attempts))
                if attempts >= MAX_SEND_ATTEMPTS:
                    lines.append(
                        f"🚫 {member['name']}: warning {level} could not be "
                        f"delivered after {MAX_SEND_ATTEMPTS} attempts — giving "
                        "up (the recipient likely blocked the bot or is a ghost "
                        f"account). Last reason: {detail}"
                    )
                else:
                    lines.append(
                        f"⚠️ {member['name']}: warning {level} rejected "
                        f"(attempt {attempts}/{MAX_SEND_ATTEMPTS}, will retry) — "
                        f"{detail}"
                    )
                continue
            await self.state.delete(attempts_key)  # delivered — reset the counter
            await self.warnings.record_warning(
                member["mc_user_id"], str(member["name"]), count=level,
            )
            # Also into the central member-action log, so the warning shows
            # up in the member's dossier/timeline — the tax_warnings table
            # alone is invisible there.
            try:
                await self.actions.log(
                    discord_user_id=None,
                    mc_user_id=int(member["mc_user_id"]),
                    actor_name=str(member["name"]),
                    action="tax_warning_sent",
                    detail=f"tax warning {level} (contribution below the "
                           f"{self._auto.min_rate:g}% minimum)"
                           + (" — delivery not confirmed by the game"
                              if unconfirmed else ""),
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not stop sends
                log.exception("tax warning action log failed")
            if self.mirror is not None and conversation_id:
                try:
                    await self.mirror(
                        conversation_id, str(member["name"]), subject
                    )
                except Exception:  # noqa: BLE001 — mirroring is best-effort
                    log.exception("Could not mirror tax warning conversation")
            # The conversation id makes every "sent" claim verifiable:
            # /messages/<id> in the game must show this exact message.
            if unconfirmed:
                lines.append(
                    f"📨 warning {level}/{MAX_WARNINGS} to {member['name']} "
                    f"({rate:g}%) — the game did not confirm delivery; "
                    "recorded anyway (a resend would duplicate the PM)"
                )
            else:
                proof = (
                    f"conv #{conversation_id}" if conversation_id
                    else "no conv id!"
                )
                lines.append(
                    f"📨 warning {level}/{MAX_WARNINGS} sent to "
                    f"{member['name']} ({rate:g}%, {proof})"
                )
            sent += 1
        return lines

    async def _roster_age_minutes(self, now: dt.datetime) -> float | None:
        """Minutes since the last SUCCESSFUL members sync; None when none is
        recorded (fresh install — the members table is empty then anyway,
        so there is nobody to warn either)."""
        row = await self.runs.last_success("members")
        if row is None:
            return None
        hours = _hours_since(row["finished_at"] or row["started_at"], now)
        return None if hours is None else hours * 60.0

    async def _verify_kicks(self, roster, now: dt.datetime) -> list[str]:
        """Reopen trails whose kick did not stick.

        The kick route answers 200 even when the game silently refuses
        (missing alliance rights, protected role), so ``kicked_at`` is only
        a claim. The roster is the proof: a member the members sync still
        sees well AFTER the kick was accepted was not kicked — reopen the
        trail so the kick-due stage retries (or re-flags) instead of
        considering them gone forever."""
        lines: list[str] = []
        for row in await self.warnings.kicked_rows():
            member = roster.get(row["mc_user_id"])
            if member is None:
                continue  # gone from the roster — the kick stuck
            kicked_h = _hours_since(row["kicked_at"], now)
            seen_h = _hours_since(member["last_seen_at"], now)
            if kicked_h is None or kicked_h < KICK_VERIFY_MIN_HOURS:
                continue  # the roster hasn't had time to observe the kick
            if seen_h is None or seen_h + KICK_VERIFY_MARGIN_HOURS >= kicked_h:
                continue  # not seen since the kick — no verdict yet
            await self.warnings.mark_kick_failed(row["mc_user_id"])
            try:
                await self.actions.log(
                    discord_user_id=None,
                    mc_user_id=int(row["mc_user_id"]),
                    actor_name=str(member["name"]),
                    action="tax_kick_failed",
                    detail=f"kick did not stick — still on the roster "
                           f"{kicked_h:.1f}h later (check the bot account's "
                           "alliance rights)",
                )
            except Exception:  # noqa: BLE001 — bookkeeping must not stop the scan
                log.exception("kick verification action log failed")
            lines.append(
                f"⚠️ {member['name']}: the earlier kick did NOT stick — they "
                f"are still on the roster {kicked_h:.1f}h later. Check the "
                "bot account's alliance rights; the kick stage will retry."
            )
        return lines

    async def _handle_kick_due(self, member, state, now: dt.datetime) -> str | None:
        """Third warning stayed unresolved past the gap: flag (or kick)."""
        gap = _hours_since(state["last_warning_at"], now)
        if gap is None or gap < self._auto.min_days_between * 24:
            return None
        if state["kicked_at"]:
            return None
        rate = member["contribution_rate"] or 0.0
        if not self._auto.auto_kick or self.dry_run:
            # Flag once per gap window, so the admin channel isn't spammed.
            flagged = _hours_since(state["kick_flagged_at"], now)
            if flagged is not None and flagged < self._auto.min_days_between * 24:
                return None
            await self.warnings.mark_kick_flagged(member["mc_user_id"])
            return (
                f"⛔ {member['name']} ({rate:g}%) has {MAX_WARNINGS} unresolved "
                "donation warnings — kick is due (auto_kick is off, handle "
                "manually or `!fra set tax_warnings.auto_kick true`)"
            )
        from ..mc.kick import kick_alliance_member

        # Tell the member FIRST — after the kick they are outside the
        # alliance and a PM may no longer reach them. Best-effort: a failed
        # notice never blocks the kick (three warnings were delivered).
        notice = await self._send_kick_notice(member)
        ok, detail = await kick_alliance_member(self.client, member["mc_user_id"])
        if not ok:
            return f"⚠️ {member['name']}: automatic kick failed — {detail}{notice}"
        await self.warnings.mark_kicked(member["mc_user_id"])
        # Into the dossier/timeline too — and note that the claim still gets
        # verified against the roster (see _verify_kicks).
        try:
            await self.actions.log(
                discord_user_id=None,
                mc_user_id=int(member["mc_user_id"]),
                actor_name=str(member["name"]),
                action="tax_kicked",
                detail=f"auto-kicked after {MAX_WARNINGS} unresolved donation "
                       f"warnings ({detail}); the roster sync verifies the "
                       "departure",
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not stop the scan
            log.exception("tax kick action log failed")
        return (
            f"👢 {member['name']} kicked after {MAX_WARNINGS} unresolved "
            f"donation warnings ({detail}){notice} — the roster sync will "
            "confirm the departure; if they are still listed in a few hours "
            "I will flag it and retry"
        )

    async def _send_kick_notice(self, member) -> str:
        """The removal PM, sent right before the auto-kick. Returns a short
        suffix for the admin summary line ('' only when nothing applies)."""
        from ..mc.messages import send_new_message

        subject, body = KICK_NOTICE_PRESET
        try:
            ok, detail, conversation_id = await send_new_message(
                self.client, str(member["name"]), subject,
                body.format(username=member["name"]),
            )
        except Exception as exc:  # noqa: BLE001 — the notice must never block the kick
            log.exception("kick notice to %s errored", member["name"])
            return f" · kick notice errored ({exc})"
        if ok or detail.startswith(UNCONFIRMED_PREFIX):
            if self.mirror is not None and conversation_id:
                try:
                    await self.mirror(
                        conversation_id, str(member["name"]), subject
                    )
                except Exception:  # noqa: BLE001 — mirroring is best-effort
                    log.exception("Could not mirror the kick notice")
            return " · removal notice sent"
        log.warning("kick notice to %s not delivered: %s", member["name"], detail)
        return f" · removal notice failed ({detail[:80]})"

    async def overview(self) -> list[str]:
        """Current standing for the admin command: who is below the rate
        and where they are in the warning ladder."""
        now = dt.datetime.now(dt.timezone.utc)
        roster = await self.members.active_members()
        lines: list[str] = []
        for member in sorted(
            roster.values(),
            key=lambda m: (m["contribution_rate"] or 0.0, str(m["name"]).casefold()),
        ):
            rate = member["contribution_rate"]
            if rate is None or rate >= self._auto.min_rate:
                continue
            state = await self.warnings.get(member["mc_user_id"])
            count = state["warning_count"] if state is not None else 0
            hours_member = _hours_since(member["first_seen_at"], now)
            grace = (
                " · in new-member grace"
                if hours_member is not None and hours_member < self._auto.grace_hours
                else ""
            )
            kick = " · KICK DUE" if count >= MAX_WARNINGS else ""
            lines.append(
                f"{member['name']}: {rate:g}% · warnings {count}/{MAX_WARNINGS}"
                f"{kick}{grace}"
            )
        return lines
