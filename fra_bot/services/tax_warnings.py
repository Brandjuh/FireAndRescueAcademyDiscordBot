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

Honours the global ``dry_run``: reports what it would send, sends
nothing. The member roster comes from the hourly members sync, so a fix
in game is picked up within the hour.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import MembersRepo, RunsRepo, TaxWarningsRepo
from ..mc.client import MissionChiefClient

log = logging.getLogger(__name__)

MAX_WARNINGS = 3

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

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self.members = MembersRepo(db)
        self.warnings = TaxWarningsRepo(db)
        self.runs = RunsRepo(db)
        self._auto = cfg.automation.tax_warnings

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

        # 2. Warn who is due, worst rate first, capped per run.
        sent = 0
        for member in sorted(
            roster.values(),
            key=lambda m: (m["contribution_rate"] or 0.0, str(m["name"]).casefold()),
        ):
            if sent >= self._auto.max_per_run:
                break
            rate = member["contribution_rate"]
            if rate is None or rate >= self._auto.min_rate:
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
            if not ok:
                lines.append(
                    f"⚠️ {member['name']}: warning {level} could NOT be "
                    f"sent — {detail}"
                )
                continue
            await self.warnings.record_warning(
                member["mc_user_id"], str(member["name"]), count=level,
            )
            # The conversation id makes every "sent" claim verifiable:
            # /messages/<id> in the game must show this exact message.
            proof = f"conv #{conversation_id}" if conversation_id else "no conv id!"
            lines.append(
                f"📨 warning {level}/{MAX_WARNINGS} sent to {member['name']} "
                f"({rate:g}%, {proof})"
            )
            sent += 1
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

        ok, detail = await kick_alliance_member(self.client, member["mc_user_id"])
        if not ok:
            return f"⚠️ {member['name']}: automatic kick failed — {detail}"
        await self.warnings.mark_kicked(member["mc_user_id"])
        return (
            f"👢 {member['name']} kicked after {MAX_WARNINGS} unresolved "
            f"donation warnings ({detail})"
        )

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
