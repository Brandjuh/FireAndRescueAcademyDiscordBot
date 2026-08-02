"""Sanction issuing, real mute execution, and the CoC-5 escalation engine.

What the reference bot faked, this service does for real:

* a Mute sanction actually SETS the in-game chat ban (behind the
  ``mute_execution_enabled`` switch until the route is verified live —
  see ``fra_bot/mc/chat_ban.py``), with the duration from the type;
* timed mutes carry a real ``expires_at``; the GAME lifts a timed chat
  ban itself, so the 5-minute sweep only books the stored transition to
  'expired' — revoking a running mute lifts the ban early via the game;
* every set/removed chat ban is VERIFIED against the alliance log
  (``chat_ban_set`` / ``chat_ban_removed`` rows) — the moderation routes
  answer 200 even when the game silently refuses, exactly like the kick
  route, so an absent log row raises an admin alert;
* the escalation engine follows CoC section 5: 1st offense = warning
  (30 days 'under warning'), 2nd = warning + temporary mute or kick,
  3rd = removal (temp-ban ≥ 60 days). Modes: ``advisory`` (text only),
  ``button`` (admin embed with action buttons — the cog posts it) and
  ``auto`` (acts by itself once the newest offense is
  ``escalation_gap_hours`` old, giving admins a revoke window).

Honours the global ``dry_run`` everywhere: reports, never acts.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import (
    LinksRepo,
    MemberActionsRepo,
    MembersRepo,
    SanctionsRepo,
    StateRepo,
)
from ..mc.chat_ban import remove_chat_ban, set_chat_ban
from ..mc.client import MissionChiefClient
from ..mc.kick import kick_alliance_member
from .sanction_rules import (
    ladder_advice,
    ladder_step,
    mute_duration,
    mute_expiry,
)

log = logging.getLogger(__name__)

#: A set/removed chat ban must show up in the scraped alliance log within
#: this long, or the admin is alerted that the action did not stick.
MUTE_VERIFY_HOURS = 2.0
_VERIFY_KEY = "sanction_mute_verify:"

#: Auto-escalation retries a failing kick/mute this many times, then
#: alerts once and goes quiet (mirrors the tax sender's give-up cap).
ESCALATION_MAX_ATTEMPTS = 3


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


class SanctionService:
    #: Hook set by the bot: async (discord_user_id, name, sanction_type,
    #: reason) — the sanctions cog's member notification (Discord DM with
    #: in-game PM fallback). Best-effort; never blocks an action.
    notify_member = None

    def __init__(self, cfg: Config, client: MissionChiefClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self._db = db
        self.repo = SanctionsRepo(db)
        self.links = LinksRepo(db)
        self.actions = MemberActionsRepo(db)
        self.state = StateRepo(db)
        self.members = MembersRepo(db)

    @property
    def _auto(self):
        return self.cfg.automation.sanctions

    @property
    def dry_run(self) -> bool:
        return self.cfg.automation.dry_run

    # -- issuing ----------------------------------------------------------

    async def issue(
        self, *, mc_user_id: int | None, mc_username: str | None,
        discord_user_id: int | None, admin_discord_id: int, admin_name: str,
        sanction_type: str, reason: str, reason_category: str | None = None,
        notes: str | None = None, source: str = "manual",
    ) -> dict:
        """Record a sanction; for mutes also set the real in-game chat
        ban. Returns ``{"sanction_id", "expires_at", "mute_note",
        "offense_count", "escalation"}`` — the caller (cog/web) owns the
        announcements."""
        expires_at = mute_expiry(sanction_type)
        sanction_id = await self.repo.add(
            mc_user_id=mc_user_id, mc_username=mc_username,
            discord_user_id=discord_user_id,
            admin_discord_id=admin_discord_id, admin_name=admin_name,
            sanction_type=sanction_type, reason=reason, notes=notes,
            reason_category=reason_category, source=source,
            expires_at=expires_at,
        )
        mute_note = None
        if sanction_type.startswith("Mute"):
            mute_note = await self._execute_mute(
                sanction_id, mc_user_id, mc_username, sanction_type,
            )

        offenses = 0
        escalation = None
        if (
            source not in ("tax", "escalation")
            and sanction_type.startswith(("Warning", "Mute"))
        ):
            offenses = await self.repo.offense_count(
                mc_user_id=mc_user_id, discord_user_id=discord_user_id,
                name=mc_username,
            )
            threshold = self._auto.escalation_offense_threshold
            if offenses >= 2:
                escalation = {
                    "count": offenses,
                    "step": ladder_step(offenses, threshold),
                    "advice": ladder_advice(offenses, threshold),
                    "mode": self._auto.escalation_mode,
                }
        return {
            "sanction_id": sanction_id,
            "expires_at": expires_at,
            "mute_note": mute_note,
            "offense_count": offenses,
            "escalation": escalation,
        }

    async def _execute_mute(
        self, sanction_id: int, mc_user_id: int | None,
        name: str | None, sanction_type: str,
    ) -> str:
        """Set the in-game chat ban for a Mute sanction; returns a short
        note for the announcement/summary."""
        if self.dry_run:
            note = "in-game chat ban not set (dry-run)"
        elif not self._auto.mute_execution_enabled:
            note = (
                "in-game chat ban NOT set — mute execution is off until "
                "the chat-ban route is verified "
                "(`!fra set sanctions.mute_execution_enabled true`)"
            )
        elif mc_user_id is None:
            note = "in-game chat ban NOT set — member has no known MC id"
        else:
            duration = mute_duration(sanction_type)
            minutes = int(duration.total_seconds() // 60) if duration else None
            ok, detail = await set_chat_ban(
                self.client, int(mc_user_id), duration_minutes=minutes,
            )
            if ok:
                note = f"in-game chat ban set ({detail})"
                await self._arm_verify(sanction_id, "chat_ban_set")
            else:
                note = f"⚠️ in-game chat ban FAILED: {detail}"
        await self.repo.add_history(
            sanction_id, action="mute_execution", actor="FRA Bot", detail=note,
        )
        return note

    async def revoke(
        self, sanction_id: int, *, revoked_by: str,
    ) -> tuple[bool, str]:
        """Revoke a sanction; a still-running mute is lifted in the game
        FIRST — if that fails the sanction stays active, so register and
        game never disagree. Returns (ok, note)."""
        row = await self.repo.get(sanction_id)
        if row is None:
            return False, "sanction does not exist"
        if row["status"] != "active":
            return False, f"sanction is already {row['status']}"
        note = ""
        if (
            str(row["sanction_type"]).startswith("Mute")
            and not self.dry_run
            and self._auto.mute_execution_enabled
            and row["mc_user_id"] is not None
            and self._mute_still_running(row)
        ):
            ok, detail = await remove_chat_ban(self.client, int(row["mc_user_id"]))
            if not ok:
                await self.repo.add_history(
                    sanction_id, action="unmute_failed", actor=revoked_by,
                    detail=detail,
                )
                return False, (
                    f"chat ban removal failed ({detail}) — the sanction "
                    "stays active so register and game don't diverge"
                )
            note = " · in-game chat ban lifted"
            await self._arm_verify(sanction_id, "chat_ban_removed")
        if not await self.repo.revoke(
            sanction_id, revoked_by=revoked_by,
        ):
            return False, "sanction was already revoked"
        return True, note

    @staticmethod
    def _mute_still_running(row) -> bool:
        expires = row["expires_at"]
        if not expires:
            return True  # untimed mute — always lift on revoke
        try:
            expiry = dt.datetime.fromisoformat(expires)
        except ValueError:
            return True
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.timezone.utc)
        return expiry > dt.datetime.now(dt.timezone.utc)

    # -- the 5-minute sweep ----------------------------------------------

    async def sweep(self) -> list[str]:
        """Expiry bookkeeping + chat-ban log verification + the auto
        escalation pass. Returns admin-summary lines."""
        now = dt.datetime.now(dt.timezone.utc)
        lines: list[str] = []
        try:
            for row in await self.repo.expire_due_mutes(
                now.isoformat(timespec="seconds")
            ):
                lines.append(
                    f"⌛ mute #{row['id']} for "
                    f"**{row['mc_username'] or row['mc_user_id']}** expired "
                    "(the game lifts timed chat bans itself)"
                )
        except Exception:  # noqa: BLE001 — one stage must not kill the sweep
            log.exception("mute expiry sweep failed")
        try:
            lines.extend(await self._verify_pass(now))
        except Exception:  # noqa: BLE001
            log.exception("mute verification pass failed")
        try:
            lines.extend(await self._auto_escalation(now))
        except Exception:  # noqa: BLE001
            log.exception("auto escalation pass failed")
        return lines

    async def _arm_verify(self, sanction_id: int, action_key: str) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        deadline = now + dt.timedelta(hours=MUTE_VERIFY_HOURS)
        await self.state.set(
            f"{_VERIFY_KEY}{sanction_id}",
            f"{action_key}|{now.isoformat(timespec='seconds')}"
            f"|{deadline.isoformat(timespec='seconds')}",
        )

    async def _verify_pass(self, now: dt.datetime) -> list[str]:
        """Prove armed chat-ban actions against the alliance log; alert
        when the log never showed them (same philosophy as the tax kick's
        roster verification — a 200 from the route proves nothing)."""
        async with self._db.conn.execute(
            "SELECT key, value FROM scraper_state WHERE key LIKE ?",
            (f"{_VERIFY_KEY}%",),
        ) as cur:
            pending = list(await cur.fetchall())
        lines: list[str] = []
        for entry in pending:
            key = entry["key"]
            try:
                sanction_id = int(key.removeprefix(_VERIFY_KEY))
                action_key, issued_iso, deadline_iso = entry["value"].split("|")
            except ValueError:
                await self.state.delete(key)
                continue
            row = await self.repo.get(sanction_id)
            if row is None:
                await self.state.delete(key)
                continue
            if await self._log_row_exists(
                action_key, issued_iso,
                mc_user_id=row["mc_user_id"], name=row["mc_username"],
            ):
                await self.state.delete(key)
                await self.repo.add_history(
                    sanction_id, action="verified", actor="FRA Bot",
                    detail=f"{action_key} confirmed by the alliance log",
                )
                continue
            deadline = dt.datetime.fromisoformat(deadline_iso)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=dt.timezone.utc)
            if now < deadline:
                continue  # the log sync may simply not have run yet
            await self.state.delete(key)
            await self.repo.add_history(
                sanction_id, action="verify_failed", actor="FRA Bot",
                detail=f"no {action_key} log row within "
                       f"{MUTE_VERIFY_HOURS:g}h",
            )
            verb = (
                "mute" if action_key == "chat_ban_set" else "chat-ban removal"
            )
            lines.append(
                f"⚠️ the {verb} for "
                f"**{row['mc_username'] or row['mc_user_id']}** (sanction "
                f"#{sanction_id}) never appeared in the game log — it "
                "probably did not stick. Check the bot account's "
                "Moderator action rights (and the chat-ban route)."
            )
        return lines

    async def _log_row_exists(
        self, action_key: str, since_iso: str, *,
        mc_user_id: int | None, name: str | None,
    ) -> bool:
        clauses, params = [], []
        if mc_user_id is not None:
            clauses.append("affected_mc_id = ?")
            params.append(int(mc_user_id))
        if name:
            clauses.append("affected_name = ? COLLATE NOCASE")
            params.append(name)
        if not clauses:
            return False
        async with self._db.conn.execute(
            f"SELECT 1 FROM alliance_logs WHERE action_key = ? "
            f"AND scraped_at >= ? AND ({' OR '.join(clauses)}) LIMIT 1",
            (action_key, since_iso, *params),
        ) as cur:
            return await cur.fetchone() is not None

    # -- escalation (CoC section 5) ---------------------------------------

    async def _auto_escalation(self, now: dt.datetime) -> list[str]:
        """``auto`` mode only: act on members at/over an escalation step
        once their newest offense is ``escalation_gap_hours`` old (the
        revoke window for admins)."""
        if self._auto.escalation_mode != "auto":
            return []
        gap_hours = float(self._auto.escalation_gap_hours)
        rows = await self.repo.all_countable()
        groups: dict[object, list] = {}
        for row in rows:
            key = (
                int(row["mc_user_id"]) if row["mc_user_id"] is not None
                else str(row["mc_username"] or "").casefold()
            )
            if key == "":
                continue
            groups.setdefault(key, []).append(row)
        lines: list[str] = []
        for items in groups.values():
            try:
                line = await self._auto_escalate_member(items, now, gap_hours)
            except Exception:  # noqa: BLE001 — one member must not stop the pass
                log.exception("auto escalation for a member failed")
                continue
            if line:
                lines.append(line)
        return lines

    async def _auto_escalate_member(
        self, items: list, now: dt.datetime, gap_hours: float,
    ) -> str | None:
        count = len(items)
        if count < 2:
            return None
        latest = max(items, key=lambda r: r["id"])
        age = _hours_since(latest["created_at"], now)
        if age is None or age < gap_hours:
            return None
        mc_user_id = next(
            (int(r["mc_user_id"]) for r in items if r["mc_user_id"] is not None),
            None,
        )
        discord_id = next(
            (int(r["discord_user_id"]) for r in items
             if r["discord_user_id"] is not None),
            None,
        )
        name = latest["mc_username"] or next(
            (r["mc_username"] for r in items if r["mc_username"]), None,
        )
        member_rows = await self.repo.for_member(
            mc_user_id=mc_user_id, discord_user_id=discord_id, name=name,
            limit=500,
        )
        if self._already_acted(member_rows, latest["id"]):
            return None
        threshold = self._auto.escalation_offense_threshold
        if ladder_step(count, threshold) == "final":
            return await self.execute_escalation_kick(
                mc_user_id=mc_user_id, name=name, discord_user_id=discord_id,
                count=count, by="FRA Bot (auto escalation)",
                latest_offense_id=latest["id"],
            )
        return await self.execute_escalation_mute(
            mc_user_id=mc_user_id, name=name, discord_user_id=discord_id,
            count=count, by="FRA Bot (auto escalation)",
            latest_offense_id=latest["id"],
        )

    @staticmethod
    def _already_acted(member_rows: list, latest_offense_id: int) -> bool:
        """An escalation consequence (or any kick/ban) recorded AFTER the
        newest offense means this position was already handled — also
        when an admin revoked our consequence (that's an intervention,
        not an invitation to redo it)."""
        for row in member_rows:
            if row["id"] <= latest_offense_id:
                continue
            if row["source"] == "escalation":
                return True
            if (
                row["sanction_type"] in ("Kick", "Ban")
                and row["status"] != "dismissed"
            ):
                return True
        return False

    async def _attempts(self, latest_offense_id: int) -> int:
        raw = await self.state.get(f"escalation_attempts:{latest_offense_id}")
        return int(raw or 0)

    async def _bump_attempts(self, latest_offense_id: int) -> int:
        n = await self._attempts(latest_offense_id) + 1
        await self.state.set(f"escalation_attempts:{latest_offense_id}", str(n))
        return n

    async def execute_escalation_mute(
        self, *, mc_user_id: int | None, name: str | None,
        discord_user_id: int | None, count: int, by: str,
        latest_offense_id: int | None = None,
    ) -> str | None:
        """CoC 5.2 consequence: a real temporary mute (type from
        ``escalation_mute_type``). Shared by auto mode and the admin
        buttons."""
        mute_type = self._auto.escalation_mute_type
        reason = (
            f"CoC 5.2 — {count} offenses on record: temporary mute "
            f"({mute_type.removeprefix('Mute').strip() or 'untimed'})."
        )
        if self.dry_run:
            return (
                f"📝 [dry-run] would escalation-mute **{name}** "
                f"({count} offenses, {mute_type})"
            )
        if latest_offense_id is not None:
            attempts = await self._attempts(latest_offense_id)
            if attempts >= ESCALATION_MAX_ATTEMPTS:
                return None  # gave up earlier; alert already sent
        await self._notify(
            discord_user_id, name, mute_type,
            f"{reason} Contact an admin if you believe this is a mistake.",
        )
        result = await self.issue(
            mc_user_id=mc_user_id, mc_username=name,
            discord_user_id=discord_user_id, admin_discord_id=0,
            admin_name=by, sanction_type=mute_type, reason=reason,
            source="escalation",
        )
        await self._log_action(
            "escalation_muted", discord_user_id, mc_user_id, name,
            f"#{result['sanction_id']} {mute_type} after {count} offenses "
            f"(by {by})",
        )
        note = result["mute_note"] or ""
        return (
            f"🔇 **{name}** escalation-muted ({mute_type}, {count} CoC "
            f"offenses) — sanction #{result['sanction_id']}. {note}"
        )

    async def execute_escalation_kick(
        self, *, mc_user_id: int | None, name: str | None,
        discord_user_id: int | None, count: int, by: str,
        latest_offense_id: int | None = None,
    ) -> str | None:
        """CoC 5.3 consequence: removal from the alliance (temp-ban ≥60
        days). Shared by auto mode and the admin buttons."""
        if self.dry_run:
            return (
                f"📝 [dry-run] would escalation-kick **{name}** "
                f"({count} offenses)"
            )
        roster = await self.members.active_members()
        if mc_user_id is None or mc_user_id not in roster:
            # Nobody to kick (unknown id, or already gone). A button
            # click always gets the answer; the auto pass alerts ONCE and
            # then stays quiet — without a recorded consequence this
            # member would otherwise re-alert every 5-minute pass.
            line = (
                f"⚠️ **{name}** reached the CoC 5.3 removal step "
                f"({count} offenses) but cannot be kicked "
                + ("(no MissionChief id on record)" if mc_user_id is None
                   else "(no longer on the roster)")
                + " — handle manually if needed."
            )
            if latest_offense_id is None:
                return line
            if await self._once(f"escalation_noid:{latest_offense_id}"):
                return line
            return None
        if latest_offense_id is not None:
            attempts = await self._attempts(latest_offense_id)
            if attempts >= ESCALATION_MAX_ATTEMPTS:
                return None
        reason = (
            f"CoC 5.3 — {count} offenses on record: removal from the "
            "alliance (temp-ban, return after 60 days via an admin at "
            "the earliest)."
        )
        if self._auto.escalation_notice:
            # Tell the member BEFORE the kick — afterwards a PM may no
            # longer reach them (same order as the tax auto-kick).
            await self._notify(
                discord_user_id, name, "Kick",
                f"{reason} You may re-apply after 60 days via an alliance "
                "admin (CoC 5.3).",
            )
        ok, detail = await kick_alliance_member(self.client, int(mc_user_id))
        if not ok:
            if latest_offense_id is not None:
                attempts = await self._bump_attempts(latest_offense_id)
                if attempts >= ESCALATION_MAX_ATTEMPTS:
                    return (
                        f"🚫 escalation kick for **{name}** failed "
                        f"{attempts}× — giving up ({detail}). Check the "
                        "bot account's alliance rights and kick manually."
                    )
            return f"⚠️ escalation kick for **{name}** failed — {detail}"
        result = await self.issue(
            mc_user_id=mc_user_id, mc_username=name,
            discord_user_id=discord_user_id, admin_discord_id=0,
            admin_name=by, sanction_type="Kick", reason=reason,
            notes=f"Kick executed by the bot ({detail}).",
            source="escalation",
        )
        await self._log_action(
            "escalation_kicked", discord_user_id, mc_user_id, name,
            f"#{result['sanction_id']} kicked after {count} offenses "
            f"(by {by}; {detail})",
        )
        return (
            f"👢 **{name}** removed from the alliance ({count} CoC "
            f"offenses, CoC 5.3) — sanction #{result['sanction_id']}; the "
            "members sync confirms the departure"
        )

    async def _once(self, key: str) -> bool:
        """True the first time this key is seen (one-shot admin alerts)."""
        full = f"sanction_once:{key}"
        if await self.state.get(full) is not None:
            return False
        await self.state.set(full, utcnow_iso())
        return True

    async def _notify(
        self, discord_user_id: int | None, name: str | None,
        sanction_type: str, reason: str,
    ) -> None:
        if self.notify_member is None:
            return
        try:
            await self.notify_member(discord_user_id, name, sanction_type, reason)
        except Exception:  # noqa: BLE001 — notification must never block action
            log.exception("sanction member notification failed")

    async def _log_action(
        self, action: str, discord_user_id: int | None,
        mc_user_id: int | None, name: str | None, detail: str,
    ) -> None:
        try:
            await self.actions.log(
                discord_user_id=discord_user_id,
                mc_user_id=int(mc_user_id) if mc_user_id is not None else None,
                actor_name=name, action=action, detail=detail,
            )
        except Exception:  # noqa: BLE001 — bookkeeping must not stop actions
            log.exception("sanction action log failed")
