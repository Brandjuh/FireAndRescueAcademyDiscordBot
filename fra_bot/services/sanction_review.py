"""Game-log sanction review (reference bot: sanctionmanager's log scan).

Moderation entries in the scraped alliance log — kicks and chat bans —
import as UNVERIFIED sanctions in the register, and the sanctions cog
posts a "review required" notice with Confirm/Dismiss buttons. That way
a kick an admin performed in game always ends up in the register, even
when nobody remembered to `!sanction add` it.

Ported with one new rule the reference bot lacked: kicks the BOT itself
executed (the tax auto-kick) are skipped — they already carry a fully
documented trail (three warnings + a ``tax_kicked`` dossier action), and
reviewing our own automated kick was pure noise.

Reference behaviours kept:
* first run bootstraps the checkpoint to the current log tail — history
  is never replayed as a flood of stale reviews,
* a same-type sanction recorded for the member in the last 6 hours means
  the admin already logged it manually — skipped as a duplicate,
* many imports in one pass collapse into one bulk notice.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import Config
from ..db.database import Database, utcnow_iso
from ..db.repos import (
    LinksRepo,
    MemberActionsRepo,
    SanctionsRepo,
    StateRepo,
)

log = logging.getLogger(__name__)

#: alliance-log action key -> (sanction type label, reason detail) —
#: the reference bot's mapping, verbatim.
GAME_LOG_SANCTION_ACTIONS: dict[str, tuple[str, str]] = {
    "kicked_from_alliance": ("Kick", "Kicked from the alliance"),
    "chat_ban_set": ("Mute", "Chat ban set"),
}

CHECKPOINT_KEY = "sanction_review_last_log_id"
#: A same-type sanction recorded within this window counts as "already
#: recorded manually" (reference: 6h).
DUPLICATE_LOOKBACK_HOURS = 6.0
#: A ``tax_kicked`` dossier action within this window before the log row
#: proves the kick was the bot's own auto-kick.
TAX_KICK_LOOKBACK_HOURS = 48.0
#: This many imports in one pass collapse into a single bulk notice
#: (reference threshold).
BULK_THRESHOLD = 10


class SanctionReviewService:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self._db = db
        self.sanctions = SanctionsRepo(db)
        self.state = StateRepo(db)
        self.links = LinksRepo(db)
        self.actions = MemberActionsRepo(db)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.automation.sanctions.game_log_review_enabled)

    async def scan(self) -> dict:
        """One pass over new alliance-log rows.

        Returns ``{"created": [review dicts], "skipped_own": n,
        "skipped_recorded": n, "bootstrapped": bool}`` — the cog posts the
        review notices for ``created``."""
        result = {
            "created": [], "skipped_own": 0, "skipped_recorded": 0,
            "bootstrapped": False,
        }
        if not self.enabled:
            return result

        async with self._db.conn.execute(
            "SELECT MAX(id) AS n FROM alliance_logs"
        ) as cur:
            row = await cur.fetchone()
        tail_id = int(row["n"] or 0)

        last_raw = await self.state.get(CHECKPOINT_KEY)
        if last_raw is None:
            # First run: start at the tail, never replay history.
            await self.state.set(CHECKPOINT_KEY, str(tail_id))
            result["bootstrapped"] = True
            return result
        last_id = int(last_raw or 0)
        if tail_id <= last_id:
            return result

        keys = ",".join("?" for _ in GAME_LOG_SANCTION_ACTIONS)
        async with self._db.conn.execute(
            f"SELECT * FROM alliance_logs WHERE id > ? AND action_key IN ({keys}) "
            "ORDER BY id ASC",
            (last_id, *GAME_LOG_SANCTION_ACTIONS),
        ) as cur:
            rows = list(await cur.fetchall())

        for row in rows:
            try:
                review = await self._import_row(row, result)
                if review is not None:
                    result["created"].append(review)
            except Exception:  # noqa: BLE001 — one bad row must not stop the scan
                log.exception("sanction review: log row %s failed", row["id"])
        await self.state.set(CHECKPOINT_KEY, str(tail_id))
        return result

    async def _import_row(self, row, result: dict) -> dict | None:
        sanction_type, reason_detail = GAME_LOG_SANCTION_ACTIONS[row["action_key"]]
        mc_user_id = row["affected_mc_id"]
        name = row["affected_name"]
        event_iso = row["event_at"] or utcnow_iso()

        # The bot's own tax auto-kick: fully documented already — skip.
        # Matched on MC id OR name: the log row of a kicked member often
        # renders their name WITHOUT a profile link (they're no longer in
        # the alliance), so requiring the id let exactly these reviews
        # through.
        if (
            row["action_key"] == "kicked_from_alliance"
            and await self.actions.exists_since(
                action="tax_kicked",
                mc_user_id=int(mc_user_id) if mc_user_id is not None else None,
                actor_name=name,
                since_iso=self._iso_before(event_iso, TAX_KICK_LOOKBACK_HOURS),
            )
        ):
            result["skipped_own"] += 1
            log.info(
                "sanction review: skipping log #%s — own tax auto-kick of %s",
                row["id"], name or mc_user_id,
            )
            return None

        # Already recorded manually within the duplicate window? Skip.
        if await self.sanctions.find_matching(
            mc_user_id=int(mc_user_id) if mc_user_id is not None else None,
            name=name,
            sanction_type=sanction_type,
            created_after_iso=self._iso_before(event_iso, DUPLICATE_LOOKBACK_HOURS),
        ) is not None:
            result["skipped_recorded"] += 1
            return None

        discord_user_id = None
        if mc_user_id is not None:
            link = await self.links.get_by_mc(int(mc_user_id))
            if link is not None and link["status"] == "approved":
                discord_user_id = int(link["discord_id"])

        executor = row["executed_name"] or "MissionChief log"
        notes = (
            f"Imported from alliance log #{row['id']}.\n"
            f"Executor: {executor}"
            + (f" (MC {row['executed_mc_id']})" if row["executed_mc_id"] else "")
            + f".\nLog time: {row['event_at'] or row['raw_timestamp']}."
        )
        if row["description"]:
            notes += f"\nDescription: {row['description'][:300]}"

        sanction_id = await self.sanctions.add(
            mc_user_id=int(mc_user_id) if mc_user_id is not None else None,
            mc_username=name,
            discord_user_id=discord_user_id,
            admin_discord_id=0,
            admin_name=f"MissionChief log: {executor}",
            sanction_type=sanction_type,
            reason=reason_detail,
            notes=notes,
            status="unverified",
        )
        return {
            "sanction_id": sanction_id,
            "sanction_type": sanction_type,
            "log_id": row["id"],
            "name": name,
            "mc_user_id": mc_user_id,
            "discord_user_id": discord_user_id,
            "executor": executor,
            "executed_mc_id": row["executed_mc_id"],
            "event_at": row["event_at"],
            "raw_timestamp": row["raw_timestamp"],
            "description": row["description"],
        }

    @staticmethod
    def _iso_before(iso: str, hours: float) -> str:
        try:
            moment = dt.datetime.fromisoformat(iso)
        except ValueError:
            moment = dt.datetime.now(dt.timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return (moment - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
