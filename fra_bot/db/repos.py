"""Repositories: all SQL lives here, grouped per domain.

Every repository takes the shared :class:`~fra_bot.db.database.Database`.
Timestamps in and out are UTC ISO-8601 strings.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import aiosqlite

from ..mc.parsers.common import infer_expense_event_ats
from .database import Database, utcnow_iso


class StateRepo:
    """Small key/value store for sync cursors and backfill progress."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str, default: str | None = None) -> str | None:
        async with self._db.conn.execute(
            "SELECT value FROM scraper_state WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO scraper_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def delete(self, key: str) -> None:
        await self._db.execute("DELETE FROM scraper_state WHERE key = ?", (key,))


class RunsRepo:
    """Audit log of scrape runs, also used to find the latest snapshot."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, scraper: str) -> int:
        return await self._db.execute_returning_id(
            "INSERT INTO scrape_runs (scraper, started_at) VALUES (?, ?)",
            (scraper, utcnow_iso()),
        )

    async def finish(
        self,
        run_id: int,
        *,
        status: str,
        pages: int = 0,
        rows_parsed: int = 0,
        rows_new: int = 0,
        message: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE scrape_runs SET finished_at = ?, status = ?, pages = ?, "
            "rows_parsed = ?, rows_new = ?, message = ? WHERE id = ?",
            (utcnow_iso(), status, pages, rows_parsed, rows_new, message, run_id),
        )

    async def last_success(self, scraper: str) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM scrape_runs WHERE scraper = ? AND status = 'success' "
            "ORDER BY id DESC LIMIT 1",
            (scraper,),
        ) as cur:
            return await cur.fetchone()

    async def recent(self, limit: int = 10) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())

    async def close_orphans(self) -> int:
        """Mark runs still 'running' as failed — they were interrupted by
        a crash, restart, or an unhandled circuit-breaker pause. Call once
        at startup so stale rows don't linger in `!fra status`."""
        cur = await self._db.execute(
            "UPDATE scrape_runs SET status = 'failed', finished_at = ?, "
            "message = COALESCE(message, 'interrupted') "
            "WHERE status = 'running'",
            (utcnow_iso(),),
        )
        return cur


class MembersRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def active_members(self) -> dict[int, aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM members WHERE is_active = 1"
        ) as cur:
            return {row["mc_user_id"]: row for row in await cur.fetchall()}

    async def active_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM members WHERE is_active = 1"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def apply_roster(
        self, run_id: int, roster: list[dict[str, Any]], *, detect_changes: bool
    ) -> list[dict[str, Any]]:
        """Apply a full roster scrape in ONE transaction.

        Returns the member events generated (also written to member_events).
        ``detect_changes`` is False on the very first sync so 47 pages of
        members don't get announced as "joined".
        """
        now = utcnow_iso()
        events: list[dict[str, Any]] = []
        previous = await self.active_members()
        seen_ids: set[int] = set()

        async with self._db.transaction() as conn:
            for entry in roster:
                mc_id = entry["mc_user_id"]
                seen_ids.add(mc_id)
                old = previous.get(mc_id)
                if old is None and detect_changes:
                    events.append(
                        {
                            "mc_user_id": mc_id,
                            "name": entry["name"],
                            "event_type": "joined",
                            "old_value": None,
                            "new_value": entry.get("role"),
                        }
                    )
                elif old is not None and detect_changes:
                    if entry["name"] != old["name"]:
                        events.append(
                            {
                                "mc_user_id": mc_id,
                                "name": entry["name"],
                                "event_type": "name_changed",
                                "old_value": old["name"],
                                "new_value": entry["name"],
                            }
                        )
                    if (entry.get("role") or "") != (old["role"] or ""):
                        events.append(
                            {
                                "mc_user_id": mc_id,
                                "name": entry["name"],
                                "event_type": "role_changed",
                                "old_value": old["role"],
                                "new_value": entry.get("role"),
                            }
                        )
                    old_rate = old["contribution_rate"]
                    new_rate = entry.get("contribution_rate")
                    if (
                        old_rate is not None
                        and new_rate is not None
                        and abs(old_rate - new_rate) >= 0.01
                    ):
                        events.append(
                            {
                                "mc_user_id": mc_id,
                                "name": entry["name"],
                                "event_type": "contribution_changed",
                                "old_value": f"{old_rate:g}%",
                                "new_value": f"{new_rate:g}%",
                            }
                        )

                await conn.execute(
                    """
                    INSERT INTO members (mc_user_id, name, role, earned_credits,
                        contribution_rate, raw_member_since, is_active,
                        first_seen_at, last_seen_at, left_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                    ON CONFLICT(mc_user_id) DO UPDATE SET
                        name = excluded.name,
                        role = excluded.role,
                        earned_credits = excluded.earned_credits,
                        contribution_rate = excluded.contribution_rate,
                        raw_member_since = COALESCE(excluded.raw_member_since,
                                                    members.raw_member_since),
                        is_active = 1,
                        last_seen_at = excluded.last_seen_at,
                        left_at = NULL
                    """,
                    (
                        mc_id,
                        entry["name"],
                        entry.get("role"),
                        entry.get("earned_credits"),
                        entry.get("contribution_rate"),
                        entry.get("raw_member_since"),
                        now,
                        now,
                    ),
                )
                await conn.execute(
                    """
                    INSERT OR REPLACE INTO member_snapshots
                        (run_id, mc_user_id, name, role, earned_credits,
                         contribution_rate, taken_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        mc_id,
                        entry["name"],
                        entry.get("role"),
                        entry.get("earned_credits"),
                        entry.get("contribution_rate"),
                        now,
                    ),
                )

            if detect_changes:
                for mc_id, old in previous.items():
                    if mc_id in seen_ids:
                        continue
                    await conn.execute(
                        "UPDATE members SET is_active = 0, left_at = ? WHERE mc_user_id = ?",
                        (now, mc_id),
                    )
                    events.append(
                        {
                            "mc_user_id": mc_id,
                            "name": old["name"],
                            "event_type": "left",
                            "old_value": old["role"],
                            "new_value": None,
                        }
                    )

            for event in events:
                await conn.execute(
                    """
                    INSERT INTO member_events
                        (mc_user_id, name, event_type, old_value, new_value, occurred_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["mc_user_id"],
                        event["name"],
                        event["event_type"],
                        event["old_value"],
                        event["new_value"],
                        now,
                    ),
                )
        return events

    async def pending_events(self, limit: int = 25) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM member_events WHERE posted_at IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_event_posted(self, event_id: int) -> None:
        await self._db.execute(
            "UPDATE member_events SET posted_at = ? WHERE id = ?",
            (utcnow_iso(), event_id),
        )

    async def event_counts(
        self, start_iso: str | None, end_iso: str
    ) -> dict[str, int]:
        """Member events by type within a period (occurred_at)."""
        query = "SELECT event_type, COUNT(*) AS n FROM member_events WHERE occurred_at <= ?"
        params: list[Any] = [end_iso]
        if start_iso:
            query += " AND occurred_at >= ?"
            params.append(start_iso)
        query += " GROUP BY event_type"
        async with self._db.conn.execute(query, params) as cur:
            return {row["event_type"]: row["n"] for row in await cur.fetchall()}

    async def credit_deltas(self, since_iso: str, until_iso: str) -> list[aiosqlite.Row]:
        """Earned-credit gains per member between two instants."""
        async with self._db.conn.execute(
            """
            SELECT mc_user_id, MAX(name) AS name,
                   MAX(earned_credits) - MIN(earned_credits) AS delta
            FROM member_snapshots
            WHERE taken_at >= ? AND taken_at <= ? AND earned_credits IS NOT NULL
            GROUP BY mc_user_id
            HAVING delta > 0
            ORDER BY delta DESC
            """,
            (since_iso, until_iso),
        ) as cur:
            return list(await cur.fetchall())


class ApplicationsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_seen(self, applications: list[dict[str, Any]]) -> list[int]:
        """Record currently-listed applications; returns ids of NEW ones.

        Applications that vanished from the page are marked resolved.
        """
        now = utcnow_iso()
        new_ids: list[int] = []
        async with self._db.transaction() as conn:
            listed_ids = [app["application_id"] for app in applications]
            for app in applications:
                cur = await conn.execute(
                    "SELECT application_id FROM applications WHERE application_id = ?",
                    (app["application_id"],),
                )
                exists = await cur.fetchone() is not None
                if exists:
                    await conn.execute(
                        "UPDATE applications SET last_seen_at = ?, resolved_at = NULL "
                        "WHERE application_id = ?",
                        (now, app["application_id"]),
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO applications
                            (application_id, applicant_name, mc_user_id,
                             first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            app["application_id"],
                            app["applicant_name"],
                            app.get("mc_user_id"),
                            now,
                            now,
                        ),
                    )
                    new_ids.append(app["application_id"])

            if listed_ids:
                placeholders = ",".join("?" for _ in listed_ids)
                await conn.execute(
                    f"UPDATE applications SET resolved_at = ? "
                    f"WHERE resolved_at IS NULL AND application_id NOT IN ({placeholders})",
                    (now, *listed_ids),
                )
            else:
                await conn.execute(
                    "UPDATE applications SET resolved_at = ? WHERE resolved_at IS NULL",
                    (now,),
                )
        return new_ids

    async def pending_announcements(self) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM applications WHERE posted_at IS NULL ORDER BY first_seen_at ASC"
        ) as cur:
            return list(await cur.fetchall())

    async def mark_posted(self, application_id: int) -> None:
        await self._db.execute(
            "UPDATE applications SET posted_at = ? WHERE application_id = ?",
            (utcnow_iso(), application_id),
        )

    async def open_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE resolved_at IS NULL"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]


class LogsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert_batch(
        self, rows: list[dict[str, Any]], *, mark_posted: bool = False
    ) -> int:
        """Insert parsed log rows; duplicates are skipped.

        ``rows`` must be in CHRONOLOGICAL order (oldest first) so that
        ascending id matches event order. Identical rows within a batch
        are disambiguated by occurrence_index; across batches the
        UNIQUE(signature, occurrence_index) constraint deduplicates
        re-scraped rows while keeping genuinely repeated events.

        ``mark_posted`` stamps the rows as already announced — used by the
        history backfill so old entries never flood the Discord feed.
        """
        now = utcnow_iso()
        posted_at = now if mark_posted else None
        inserted = 0
        conn = self._db.conn

        # Existing occurrence counts for signatures in this batch.
        signatures = list({row["signature"] for row in rows})
        counts: dict[str, int] = {}
        for chunk_start in range(0, len(signatures), 500):
            chunk = signatures[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            async with conn.execute(
                f"SELECT signature, MAX(occurrence_index) AS n FROM alliance_logs "
                f"WHERE signature IN ({placeholders}) GROUP BY signature",
                chunk,
            ) as cur:
                for row in await cur.fetchall():
                    counts[row["signature"]] = row["n"]

        batch_seen: dict[str, int] = {}
        async with self._db.transaction() as conn:
            for row in rows:
                sig = row["signature"]
                batch_seen[sig] = batch_seen.get(sig, 0) + 1
                occurrence = batch_seen[sig]
                if occurrence <= counts.get(sig, 0):
                    continue  # already stored from a previous scrape
                cur = await conn.execute(
                    """
                    INSERT OR IGNORE INTO alliance_logs
                        (signature, occurrence_index, raw_timestamp, event_at,
                         action_key, description, executed_name, executed_mc_id,
                         affected_name, affected_type, affected_mc_id,
                         contribution_amount, scraped_at, posted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sig,
                        occurrence,
                        row["raw_timestamp"],
                        row.get("event_at"),
                        row["action_key"],
                        row["description"],
                        row.get("executed_name"),
                        row.get("executed_mc_id"),
                        row.get("affected_name"),
                        row.get("affected_type"),
                        row.get("affected_mc_id"),
                        row.get("contribution_amount"),
                        now,
                        posted_at,
                    ),
                )
                inserted += cur.rowcount if cur.rowcount > 0 else 0
        return inserted

    async def known_signatures(self, signatures: list[str]) -> set[str]:
        result: set[str] = set()
        for chunk_start in range(0, len(signatures), 500):
            chunk = signatures[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            async with self._db.conn.execute(
                f"SELECT DISTINCT signature FROM alliance_logs "
                f"WHERE signature IN ({placeholders})",
                chunk,
            ) as cur:
                result.update(row["signature"] for row in await cur.fetchall())
        return result

    async def pending_posts(self, limit: int = 30) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            """
            SELECT * FROM alliance_logs WHERE posted_at IS NULL
            ORDER BY COALESCE(event_at, scraped_at) ASC, id ASC LIMIT ?
            """,
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_posted(self, log_id: int) -> None:
        await self._db.execute(
            "UPDATE alliance_logs SET posted_at = ? WHERE id = ?",
            (utcnow_iso(), log_id),
        )

    async def mark_all_posted(self) -> int:
        """Used on first sync so history isn't spammed into Discord."""
        return await self._db.execute(
            "UPDATE alliance_logs SET posted_at = ? WHERE posted_at IS NULL",
            (utcnow_iso(),),
        )

    async def count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM alliance_logs"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def applications_received(self) -> dict[str, int]:
        """Application outcomes recorded in the alliance log: accepted
        (``added_to_alliance``) and denied (``application_denied``).

        This is the reliable "how many applications came in" figure — the
        applications *table* only sees requests still open during a poll,
        and most are handled between polls. Coverage reaches as far back as
        the stored log history goes.
        """
        async with self._db.conn.execute(
            "SELECT action_key, COUNT(*) AS n FROM alliance_logs "
            "WHERE action_key IN ('added_to_alliance', 'application_denied') "
            "GROUP BY action_key"
        ) as cur:
            return {row["action_key"]: row["n"] for row in await cur.fetchall()}

    async def action_counts(
        self, start_iso: str | None, end_iso: str
    ) -> dict[str, int]:
        """Log rows by action_key within a period (event_at, else scraped_at)."""
        query = (
            "SELECT action_key, COUNT(*) AS n FROM alliance_logs "
            "WHERE COALESCE(event_at, scraped_at) <= ?"
        )
        params: list[Any] = [end_iso]
        if start_iso:
            query += " AND COALESCE(event_at, scraped_at) >= ?"
            params.append(start_iso)
        query += " GROUP BY action_key ORDER BY n DESC"
        async with self._db.conn.execute(query, params) as cur:
            return {row["action_key"]: row["n"] for row in await cur.fetchall()}


class TreasuryRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- balance -------------------------------------------------------

    async def record_balance(self, total_funds: int) -> None:
        await self._db.execute(
            "INSERT INTO treasury_balance (total_funds, scraped_at) VALUES (?, ?)",
            (total_funds, utcnow_iso()),
        )

    async def latest_balance(self) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM treasury_balance ORDER BY id DESC LIMIT 1"
        ) as cur:
            return await cur.fetchone()

    # -- income snapshots ----------------------------------------------

    async def store_income_snapshot(
        self, period: str, period_key: str, entries: list[dict[str, Any]]
    ) -> None:
        """Store a snapshot batch; readers use the newest batch per key.

        taken_at carries microsecond precision so two batches can never
        merge, even when stored within the same second.
        """
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        async with self._db.transaction() as conn:
            for rank, entry in enumerate(entries, start=1):
                await conn.execute(
                    """
                    INSERT INTO income_snapshots
                        (period, period_key, taken_at, rank, username, mc_user_id, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        period,
                        period_key,
                        now,
                        rank,
                        entry["username"],
                        entry.get("mc_user_id"),
                        entry["amount"],
                    ),
                )

    async def latest_snapshot(
        self, period: str, period_key: str
    ) -> list[aiosqlite.Row]:
        """Rows of the most recent snapshot taken for this period_key."""
        async with self._db.conn.execute(
            "SELECT MAX(taken_at) AS t FROM income_snapshots "
            "WHERE period = ? AND period_key = ?",
            (period, period_key),
        ) as cur:
            row = await cur.fetchone()
        if not row or row["t"] is None:
            return []
        async with self._db.conn.execute(
            "SELECT * FROM income_snapshots "
            "WHERE period = ? AND period_key = ? AND taken_at = ? ORDER BY rank ASC",
            (period, period_key, row["t"]),
        ) as cur:
            return list(await cur.fetchall())

    # -- expenses --------------------------------------------------------

    async def insert_expenses_chronological(self, rows: list[dict[str, Any]]) -> int:
        """Append expense rows; caller guarantees chronological order."""
        if not rows:
            return 0
        now = utcnow_iso()
        async with self._db.transaction() as conn:
            await conn.executemany(
                """
                INSERT INTO expenses
                    (signature, raw_date, event_at, username, amount, description, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["signature"],
                        row["raw_date"],
                        row.get("event_at"),
                        row["username"],
                        row["amount"],
                        row.get("description"),
                        now,
                    )
                    for row in rows
                ],
            )
        return len(rows)

    async def newest_signatures(self, limit: int = 60) -> list[str]:
        """Signatures of the newest stored expenses, in CHRONOLOGICAL order."""
        async with self._db.conn.execute(
            "SELECT signature FROM expenses ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [row["signature"] for row in reversed(rows)]

    async def expense_count(self) -> int:
        async with self._db.conn.execute("SELECT COUNT(*) AS n FROM expenses") as cur:
            row = await cur.fetchone()
        return row["n"]

    async def expense_summary(
        self, start_iso: str | None, end_iso: str, *, top: int = 5
    ) -> dict[str, Any]:
        """Total spend, row count and top spenders within a period."""
        where = "event_at IS NOT NULL AND event_at <= ?"
        params: list[Any] = [end_iso]
        if start_iso:
            where += " AND event_at >= ?"
            params.append(start_iso)
        async with self._db.conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total "
            f"FROM expenses WHERE {where}",
            params,
        ) as cur:
            row = await cur.fetchone()
        async with self._db.conn.execute(
            f"SELECT username, SUM(amount) AS spent FROM expenses WHERE {where} "
            f"GROUP BY username ORDER BY spent DESC LIMIT ?",
            (*params, top),
        ) as cur:
            spenders = [(r["username"], r["spent"]) for r in await cur.fetchall()]
        return {"count": row["n"], "total": row["total"], "top": spenders}

    # -- expenses backfill staging ---------------------------------------
    # Rows are appended in DISPLAY order (newest first) while walking
    # pages 1..last; finalize() copies them into expenses reversed.

    async def staging_append(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        async with self._db.transaction() as conn:
            await conn.executemany(
                """
                INSERT INTO expenses_backfill_staging
                    (signature, raw_date, event_at, username, amount, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["signature"],
                        row["raw_date"],
                        row.get("event_at"),
                        row["username"],
                        row["amount"],
                        row.get("description"),
                    )
                    for row in rows
                ],
            )
        return len(rows)

    async def staging_tail_signatures(self, limit: int = 60) -> list[str]:
        """Signatures of the most recently appended staging rows, in
        insertion order (matching display order of the walked pages)."""
        async with self._db.conn.execute(
            "SELECT signature FROM expenses_backfill_staging ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [row["signature"] for row in reversed(rows)]

    async def staging_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM expenses_backfill_staging"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def staging_finalize(
        self, done_key: str, next_page_key: str, *, current_year: int
    ) -> int:
        """Copy staging into expenses (chronological), clear staging, and
        flip the backfill-done state — all in ONE transaction, so the
        ledger and the 'done' flag can never disagree after a crash.

        The ledger's dates are yearless, so as we finalize we infer each
        row's year from its position in the newest→oldest sequence
        (``infer_expense_event_ats``) — making historical expenses datable
        and therefore reportable by period.
        """
        now = utcnow_iso()
        async with self._db.transaction() as conn:
            # id ASC = insertion order = display order (newest first).
            async with conn.execute(
                "SELECT signature, raw_date, username, amount, description "
                "FROM expenses_backfill_staging ORDER BY id ASC"
            ) as cur:
                staged = list(await cur.fetchall())

            event_ats = infer_expense_event_ats(
                [row["raw_date"] for row in staged], current_year=current_year
            )
            # Insert oldest-first so ascending expense ids match event order.
            ordered = list(zip(staged, event_ats))
            ordered.reverse()
            await conn.executemany(
                """
                INSERT INTO expenses
                    (signature, raw_date, event_at, username, amount, description, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["signature"], row["raw_date"], event_at,
                        row["username"], row["amount"], row["description"], now,
                    )
                    for row, event_at in ordered
                ],
            )
            copied = len(ordered)
            await conn.execute("DELETE FROM expenses_backfill_staging")
            await conn.execute(
                "INSERT INTO scraper_state (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (done_key,),
            )
            await conn.execute("DELETE FROM scraper_state WHERE key = ?", (next_page_key,))
        return copied


class BoardRepo:
    """Seen board posts per thread; the source of poll dedup state."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def last_seen_post_id(self, thread_id: int) -> int | None:
        async with self._db.conn.execute(
            "SELECT MAX(post_id) AS m FROM board_posts WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        return row["m"]



class AutomationRepo:
    """Member requests extracted from board posts + their outcomes."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        kind: str,
        thread_id: int,
        post_id: int,
        requester_name: str | None,
        requester_mc_id: int | None,
        payload: str | None = None,
    ) -> int:
        now = utcnow_iso()
        return await self._db.execute_returning_id(
            """
            INSERT INTO automation_requests
                (kind, thread_id, post_id, requester_name, requester_mc_id,
                 payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, thread_id, post_id, requester_name, requester_mc_id, payload, now, now),
        )

    async def record_post_and_request(
        self,
        thread_id: int,
        post: dict[str, Any],
        kind: str,
        request: dict[str, Any] | None,
    ) -> tuple[bool, int | None]:
        """Atomically mark a board post seen AND, if it is a request,
        create its automation_request in status 'pending'.

        This is the fix for the stranding bug: the post's seen-state and
        the request's existence commit together, so a crash can never
        leave a post marked seen with its request lost. Returns
        (post_was_new, request_id_or_None).
        """
        now = utcnow_iso()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                """
                INSERT OR IGNORE INTO board_posts
                    (thread_id, post_id, author_name, author_mc_id,
                     raw_timestamp, content, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    post["post_id"],
                    post.get("author_name"),
                    post.get("author_mc_id"),
                    post.get("raw_timestamp"),
                    post["content"],
                    now,
                ),
            )
            if cur.rowcount == 0:
                return (False, None)  # already recorded on a prior poll
            if request is None:
                return (True, None)
            rcur = await conn.execute(
                """
                INSERT INTO automation_requests
                    (kind, thread_id, post_id, requester_name, requester_mc_id,
                     payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    thread_id,
                    post["post_id"],
                    request.get("requester_name"),
                    request.get("requester_mc_id"),
                    request.get("payload"),
                    now,
                    now,
                ),
            )
            return (True, rcur.lastrowid)

    async def claimable(self, kind: str) -> list[aiosqlite.Row]:
        """Requests ready to execute: fresh 'pending' ones plus 'waiting'
        ones whose retry time is due."""
        async with self._db.conn.execute(
            """
            SELECT * FROM automation_requests
            WHERE kind = ? AND (
                status = 'pending'
                OR (status = 'waiting'
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
            )
            ORDER BY id ASC
            """,
            (kind, utcnow_iso()),
        ) as cur:
            return list(await cur.fetchall())

    async def claim(self, request_id: int) -> bool:
        """Move a request pending/waiting -> processing atomically.

        Returns True only if this caller won the claim, so two concurrent
        polls can never execute the same (non-idempotent) action twice.
        """
        n = await self._db.execute(
            "UPDATE automation_requests SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'waiting')",
            (utcnow_iso(), request_id),
        )
        return n == 1

    async def requeue(self, request_id: int, *, payload: str | None = None) -> bool:
        """Admin-approved retry: back to 'pending' with a clean attempt
        budget. Only terminal requests can be re-queued (an open one is
        already being worked on)."""
        sets = (
            "status = 'pending', status_detail = 're-queued by admin', "
            "attempts = 0, next_attempt_at = NULL, updated_at = ?"
        )
        params: list = [utcnow_iso()]
        if payload is not None:
            sets += ", payload = ?"
            params.append(payload)
        params.append(request_id)
        n = await self._db.execute(
            f"UPDATE automation_requests SET {sets} "
            "WHERE id = ? AND status IN ('failed', 'skipped')",
            tuple(params),
        )
        return n == 1

    async def sweep_processing(self, *, requeue: bool = False) -> int:
        """A request left 'processing' was interrupted mid-action by a
        crash/restart. Re-running could repeat a non-idempotent
        MissionChief action, so flag it for manual review instead of
        silently losing it or blindly retrying. Call once at startup.

        ``requeue`` (dry-run) instead re-arms it to 'pending': in dry-run no
        real action can have half-run, so it's safe (and less alarming) to
        just re-process it cleanly."""
        if requeue:
            return await self._db.execute(
                "UPDATE automation_requests SET status = 'pending', "
                "status_detail = NULL, updated_at = ? WHERE status = 'processing'",
                (utcnow_iso(),),
            )
        return await self._db.execute(
            "UPDATE automation_requests SET status = 'failed', "
            "status_detail = 'interrupted mid-action — please verify on MissionChief', "
            "posted_at = NULL, updated_at = ? WHERE status = 'processing'",
            (utcnow_iso(),),
        )

    async def sweep_stale_processing(self, cutoff_iso: str, *, requeue: bool = False) -> int:
        """Release requests stuck in 'processing' since before ``cutoff_iso``.

        The startup sweep only runs at boot; this is the periodic safety net
        for a request that got stranded while the bot kept running (an
        interrupted action that never reached a terminal state). Only rows
        whose ``updated_at`` predates the cutoff are touched, so a genuinely
        in-flight action (claimed just now) is never disturbed. ``requeue``
        (dry-run) re-arms it to 'pending' rather than failing it."""
        if requeue:
            return await self._db.execute(
                "UPDATE automation_requests SET status = 'pending', "
                "status_detail = NULL, updated_at = ? "
                "WHERE status = 'processing' AND updated_at < ?",
                (utcnow_iso(), cutoff_iso),
            )
        return await self._db.execute(
            "UPDATE automation_requests SET status = 'failed', "
            "status_detail = 'interrupted mid-action (stale) — please verify on MissionChief', "
            "posted_at = NULL, updated_at = ? "
            "WHERE status = 'processing' AND updated_at < ?",
            (utcnow_iso(), cutoff_iso),
        )

    async def set_status(
        self,
        request_id: int,
        status: str,
        detail: str | None = None,
        *,
        payload: str | None = None,
        next_attempt_at: str | None = None,
        bump_attempts: bool = False,
        announce: bool = True,
    ) -> None:
        """Update a request. ``announce=True`` re-arms the Discord
        notification; pass False for retries that don't change state."""
        sets = ["status = ?", "status_detail = ?", "updated_at = ?"]
        params: list[Any] = [status, detail, utcnow_iso()]
        if announce:
            sets.append("posted_at = NULL")
        if payload is not None:
            sets.append("payload = ?")
            params.append(payload)
        sets.append("next_attempt_at = ?")
        params.append(next_attempt_at)
        if bump_attempts:
            sets.append("attempts = attempts + 1")
        params.append(request_id)
        await self._db.execute(
            f"UPDATE automation_requests SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    async def get(self, request_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM automation_requests WHERE id = ?", (request_id,)
        ) as cur:
            return await cur.fetchone()

    async def recent(self, limit: int = 15) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM automation_requests ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())

    async def pending_announcements(self, limit: int = 20) -> list[aiosqlite.Row]:
        """Terminal-state requests not yet announced in Discord."""
        async with self._db.conn.execute(
            """
            SELECT * FROM automation_requests
            WHERE posted_at IS NULL AND status IN ('done', 'failed', 'skipped', 'waiting')
            ORDER BY id ASC LIMIT ?
            """,
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_posted(self, request_id: int) -> None:
        await self._db.execute(
            "UPDATE automation_requests SET posted_at = ? WHERE id = ?",
            (utcnow_iso(), request_id),
        )

    async def open_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM automation_requests "
            "WHERE status IN ('pending', 'waiting', 'processing')"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def activity_counts(
        self, start_iso: str | None, end_iso: str
    ) -> list[aiosqlite.Row]:
        """Request counts grouped by (kind, status) within a period."""
        query = (
            "SELECT kind, status, COUNT(*) AS n FROM automation_requests "
            "WHERE created_at <= ?"
        )
        params: list[Any] = [end_iso]
        if start_iso:
            query += " AND created_at >= ?"
            params.append(start_iso)
        query += " GROUP BY kind, status ORDER BY kind, status"
        async with self._db.conn.execute(query, params) as cur:
            return list(await cur.fetchall())


class MissionsRepo:
    """Custom "Own mission" queue: member-parameterised large scale
    alliance missions, started one at a time at the next free slot.

    Mirrors :class:`AutomationRepo`'s claim/sweep semantics so a crash can
    never double-start a (non-idempotent) mission.
    """

    # Canonical request columns (the unified model). Legacy footprint
    # columns (mission_type_id/poi_type/size/shape/amount/coins) still exist
    # for old rows but are no longer written — the model is now
    # kind/mission_source/preset_type_id/caption/custom_values/saved_name.
    _COLUMNS = (
        "source", "kind", "mission_source", "preset_type_id", "caption",
        "custom_values", "saved_name", "recurring", "rotation_id",
        "event_type_id", "event_random", "area", "shape", "call_volume",
        "location_text", "latitude", "longitude", "address",
        "requester_name", "requester_mc_id", "discord_user_id", "channel_id",
        "board_thread_id", "board_post_id",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, **fields: Any) -> int:
        cols = [c for c in self._COLUMNS if c in fields]
        now = utcnow_iso()
        placeholders = ", ".join(["?"] * (len(cols) + 2))
        params = [fields[c] for c in cols] + [now, now]
        return await self._db.execute_returning_id(
            f"INSERT INTO scheduled_missions ({', '.join(cols)}, created_at, updated_at) "
            f"VALUES ({placeholders})",
            tuple(params),
        )

    async def create_from_board(
        self, thread_id: int, post_id: int, spec_fields: dict[str, Any],
        *, requester_name: str | None, requester_mc_id: int | None,
    ) -> int | None:
        """Insert a board-sourced mission, deduped on (thread, post).

        ``spec_fields`` carries the unified model keys (kind, mission_source,
        preset_type_id, caption, custom_values, saved_name, recurring,
        location_text). Returns the new id, or None if this post already
        enqueued a mission (idempotent re-scan)."""
        now = utcnow_iso()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                """
                INSERT OR IGNORE INTO scheduled_missions
                    (source, kind, mission_source, preset_type_id, caption,
                     custom_values, saved_name, recurring,
                     event_type_id, event_random, area, shape, call_volume,
                     location_text, requester_name, requester_mc_id,
                     board_thread_id, board_post_id, created_at, updated_at)
                VALUES ('board', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec_fields.get("kind", "large"),
                    spec_fields.get("mission_source", "preset"),
                    spec_fields.get("preset_type_id"),
                    spec_fields.get("caption"),
                    spec_fields.get("custom_values"),
                    spec_fields.get("saved_name"),
                    1 if spec_fields.get("recurring") else 0,
                    spec_fields.get("event_type_id"),
                    1 if spec_fields.get("event_random") else 0,
                    spec_fields.get("area"),
                    # `shape` is the legacy NOT NULL column (from 0003); never
                    # insert NULL or INSERT OR IGNORE would silently drop the row.
                    spec_fields.get("shape") or "rectangle",
                    spec_fields.get("call_volume"),
                    spec_fields.get("location_text"),
                    requester_name,
                    requester_mc_id,
                    thread_id,
                    post_id,
                    now,
                    now,
                ),
            )
            return cur.lastrowid if cur.rowcount else None

    async def claimable(self) -> list[aiosqlite.Row]:
        """Missions ready to run: 'pending' plus due 'waiting' ones."""
        async with self._db.conn.execute(
            """
            SELECT * FROM scheduled_missions
            WHERE status = 'pending'
               OR (status = 'waiting'
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
            ORDER BY id ASC
            """,
            (utcnow_iso(),),
        ) as cur:
            return list(await cur.fetchall())

    async def claim(self, mission_id: int) -> bool:
        """Move pending/waiting -> processing atomically (single-winner)."""
        n = await self._db.execute(
            "UPDATE scheduled_missions SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'waiting')",
            (utcnow_iso(), mission_id),
        )
        return n == 1

    async def sweep_processing(self, *, requeue: bool = False) -> int:
        """Flag missions interrupted mid-start for manual review (startup).
        ``requeue`` (dry-run) re-arms them to 'pending' instead — nothing real
        can have half-run in dry-run, so they just re-process cleanly."""
        if requeue:
            return await self._db.execute(
                "UPDATE scheduled_missions SET status = 'pending', "
                "status_detail = NULL, updated_at = ? WHERE status = 'processing'",
                (utcnow_iso(),),
            )
        return await self._db.execute(
            "UPDATE scheduled_missions SET status = 'failed', "
            "status_detail = 'interrupted mid-start — please verify on MissionChief', "
            "posted_at = NULL, updated_at = ? WHERE status = 'processing'",
            (utcnow_iso(),),
        )

    async def sweep_stale_processing(self, cutoff_iso: str, *, requeue: bool = False) -> int:
        """Release missions stuck in 'processing' since before ``cutoff_iso``
        (periodic safety net; only rows older than the cutoff, so a just-
        claimed start is never disturbed). ``requeue`` (dry-run) re-arms to
        'pending' instead of failing."""
        if requeue:
            return await self._db.execute(
                "UPDATE scheduled_missions SET status = 'pending', "
                "status_detail = NULL, updated_at = ? "
                "WHERE status = 'processing' AND updated_at < ?",
                (utcnow_iso(), cutoff_iso),
            )
        return await self._db.execute(
            "UPDATE scheduled_missions SET status = 'failed', "
            "status_detail = 'interrupted mid-start (stale) — please verify on MissionChief', "
            "posted_at = NULL, updated_at = ? "
            "WHERE status = 'processing' AND updated_at < ?",
            (utcnow_iso(), cutoff_iso),
        )

    async def set_status(
        self,
        mission_id: int,
        status: str,
        detail: str | None = None,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        address: str | None = None,
        next_attempt_at: str | None = None,
        bump_attempts: bool = False,
        reset_attempts: bool = False,
        announce: bool = True,
    ) -> None:
        sets = ["status = ?", "status_detail = ?", "updated_at = ?"]
        params: list[Any] = [status, detail, utcnow_iso()]
        if announce:
            sets.append("posted_at = NULL")
        if latitude is not None:
            sets.append("latitude = ?")
            params.append(latitude)
        if longitude is not None:
            sets.append("longitude = ?")
            params.append(longitude)
        if address is not None:
            sets.append("address = ?")
            params.append(address)
        sets.append("next_attempt_at = ?")
        params.append(next_attempt_at)
        if bump_attempts:
            sets.append("attempts = attempts + 1")
        elif reset_attempts:
            sets.append("attempts = 0")
        params.append(mission_id)
        await self._db.execute(
            f"UPDATE scheduled_missions SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    async def link_rotation(self, mission_id: int, rotation_id: int) -> None:
        """Record that this (recurring) request was promoted to a rotation
        entry, so it can never be promoted twice."""
        await self._db.execute(
            "UPDATE scheduled_missions SET rotation_id = ?, updated_at = ? WHERE id = ?",
            (rotation_id, utcnow_iso(), mission_id),
        )

    async def cancel(self, mission_id: int) -> bool:
        """Cancel a not-yet-started mission. Returns True if it was open."""
        n = await self._db.execute(
            "UPDATE scheduled_missions SET status = 'cancelled', "
            "status_detail = 'cancelled by admin', posted_at = NULL, updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'waiting')",
            (utcnow_iso(), mission_id),
        )
        return n == 1

    async def delete(self, mission_id: int) -> bool:
        """Hard-delete a mission row (any status). Returns True if removed."""
        n = await self._db.execute(
            "DELETE FROM scheduled_missions WHERE id = ?", (mission_id,)
        )
        return n == 1

    async def delete_terminal(self) -> int:
        """Remove all finished missions (done/failed/skipped/cancelled) — a
        tidy-up that leaves open (pending/waiting/processing) ones alone."""
        return await self._db.execute(
            "DELETE FROM scheduled_missions "
            "WHERE status IN ('done', 'failed', 'skipped', 'cancelled')"
        )

    async def get(self, mission_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM scheduled_missions WHERE id = ?", (mission_id,)
        ) as cur:
            return await cur.fetchone()

    async def recent(self, limit: int = 15) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM scheduled_missions ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return list(await cur.fetchall())

    async def next_pending(self) -> aiosqlite.Row | None:
        """The member request at the head of the line — the next thing the
        scheduler would start (before falling back to the rotation). Used to
        answer "what's next and where" for the eventpinger."""
        async with self._db.conn.execute(
            "SELECT * FROM scheduled_missions "
            "WHERE status IN ('pending', 'waiting') ORDER BY id ASC LIMIT 1"
        ) as cur:
            return await cur.fetchone()

    async def reverify_waiting(self, cap_iso: str) -> int:
        """Pull every waiting row's next retry within the cap, so parked
        requests are re-verified against the live form at least that often
        — including rows parked far out by older code or a skewed clock."""
        return await self._db.execute(
            "UPDATE scheduled_missions SET next_attempt_at = ?, updated_at = ? "
            "WHERE status = 'waiting' AND next_attempt_at > ?",
            (cap_iso, utcnow_iso(), cap_iso),
        )

    async def open_recurring_unpromoted(self, *, limit: int = 25) -> list[aiosqlite.Row]:
        """Open recurring requests not yet in the rotation — promoted at
        intake so the schedule reflects them immediately, instead of only
        after their first start."""
        async with self._db.conn.execute(
            "SELECT * FROM scheduled_missions "
            "WHERE recurring = 1 AND rotation_id IS NULL "
            "AND status IN ('pending', 'waiting', 'processing') "
            "ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def open_for_kind(self, kind: str, *, limit: int = 15) -> list[aiosqlite.Row]:
        """Open (queued) requests of one kind, oldest first — the board's
        'what is on the schedule' post."""
        async with self._db.conn.execute(
            "SELECT * FROM scheduled_missions "
            "WHERE kind = ? AND status IN ('pending', 'waiting', 'processing') "
            "ORDER BY id ASC LIMIT ?",
            (kind, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def open_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_missions "
            "WHERE status IN ('pending', 'waiting', 'processing')"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def status_counts(
        self, start_iso: str | None, end_iso: str
    ) -> dict[str, int]:
        """Mission counts by status within a period (created_at)."""
        query = "SELECT status, COUNT(*) AS n FROM scheduled_missions WHERE created_at <= ?"
        params: list[Any] = [end_iso]
        if start_iso:
            query += " AND created_at >= ?"
            params.append(start_iso)
        query += " GROUP BY status ORDER BY n DESC"
        async with self._db.conn.execute(query, params) as cur:
            return {row["status"]: row["n"] for row in await cur.fetchall()}

    async def pending_announcements(self, limit: int = 20) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            """
            SELECT * FROM scheduled_missions
            WHERE posted_at IS NULL
              AND status IN ('done', 'failed', 'skipped', 'waiting', 'cancelled')
            ORDER BY id ASC LIMIT ?
            """,
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_posted(self, mission_id: int) -> None:
        await self._db.execute(
            "UPDATE scheduled_missions SET posted_at = ? WHERE id = ?",
            (utcnow_iso(), mission_id),
        )


class RotationRepo:
    """The admin rotation list: locations the bot auto-starts and keeps
    cycling forever, one per free mission slot, filling gaps when no member
    request is pending.

    "Next up" is the active entry started least recently (never-started
    first), so the cycle is fair round-robin without a stored pointer.
    """

    _COLUMNS = (
        "location_text", "kind", "mission_source", "preset_type_id",
        "caption", "custom_values", "saved_name",
        "event_type_id", "event_random", "area", "shape", "call_volume",
        "latitude", "longitude", "address", "active", "created_by",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, **fields: Any) -> int:
        cols = [c for c in self._COLUMNS if c in fields]
        now = utcnow_iso()
        placeholders = ", ".join(["?"] * (len(cols) + 2))
        params = [fields[c] for c in cols] + [now, now]
        return await self._db.execute_returning_id(
            f"INSERT INTO mission_rotation ({', '.join(cols)}, created_at, updated_at) "
            f"VALUES ({placeholders})",
            tuple(params),
        )

    async def get(self, rotation_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM mission_rotation WHERE id = ?", (rotation_id,)
        ) as cur:
            return await cur.fetchone()

    async def list_all(self, *, active_only: bool = False) -> list[aiosqlite.Row]:
        query = "SELECT * FROM mission_rotation"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id ASC"
        async with self._db.conn.execute(query) as cur:
            return list(await cur.fetchall())

    async def next_entry(self, kind: str | None = None) -> aiosqlite.Row | None:
        """The active entry due to run next: least-recently started first,
        never-started (NULL last_started_at) ahead of all, ties by id.
        With ``kind``, the next entry of that kind only — the large and
        event cooldowns are separate, so a blocked kind must not hide the
        other kind's runnable head.

        Entries whose ORIGINATING queue request is still open are skipped:
        recurring requests are promoted at intake, so until that first
        queued start happens the rotation must not also start the same
        location — one request would otherwise run twice."""
        kind_filter = "AND r.kind = ? " if kind is not None else ""
        params: tuple = (kind,) if kind is not None else ()
        async with self._db.conn.execute(
            "SELECT * FROM mission_rotation r WHERE r.active = 1 "
            f"{kind_filter}"
            "AND NOT EXISTS (SELECT 1 FROM scheduled_missions m "
            "  WHERE m.rotation_id = r.id "
            "  AND m.status IN ('pending', 'waiting', 'processing')) "
            "ORDER BY (r.last_started_at IS NOT NULL), r.last_started_at, r.id "
            "LIMIT 1",
            params,
        ) as cur:
            return await cur.fetchone()

    async def active_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM mission_rotation WHERE active = 1"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def mark_started(
        self,
        rotation_id: int,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        address: str | None = None,
    ) -> None:
        """Advance the cycle for this entry, caching its resolved location."""
        sets = [
            "last_started_at = ?", "start_count = start_count + 1", "updated_at = ?"
        ]
        params: list[Any] = [utcnow_iso(), utcnow_iso()]
        if latitude is not None:
            sets.append("latitude = ?")
            params.append(latitude)
        if longitude is not None:
            sets.append("longitude = ?")
            params.append(longitude)
        if address is not None:
            sets.append("address = ?")
            params.append(address)
        params.append(rotation_id)
        await self._db.execute(
            f"UPDATE mission_rotation SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    async def cache_location(
        self, rotation_id: int, latitude: float, longitude: float, address: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE mission_rotation SET latitude = ?, longitude = ?, address = ?, "
            "updated_at = ? WHERE id = ?",
            (latitude, longitude, address, utcnow_iso(), rotation_id),
        )

    async def set_active(self, rotation_id: int, active: bool) -> bool:
        n = await self._db.execute(
            "UPDATE mission_rotation SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, utcnow_iso(), rotation_id),
        )
        return n == 1

    async def deactivate_with_note(self, rotation_id: int, note: str) -> None:
        """Pause an entry that can't run (e.g. its location won't geocode),
        recording why so an admin sees it in the list."""
        await self._db.execute(
            "UPDATE mission_rotation SET active = 0, address = ?, updated_at = ? "
            "WHERE id = ?",
            (note[:200], utcnow_iso(), rotation_id),
        )

    async def remove(self, rotation_id: int) -> bool:
        n = await self._db.execute(
            "DELETE FROM mission_rotation WHERE id = ?", (rotation_id,)
        )
        return n == 1


class EventPingsRepo:
    """Outbox of real alliance mission/event starts awaiting a role ping.

    The scheduler adds a row per confirmed start; the EventPinger cog
    delivers it to Discord and marks it posted. Same pattern as the
    mission outcome publisher: Discord stays out of the scheduler and a
    restart between start and ping loses nothing.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        kind: str,
        name: str | None,
        address: str | None,
        latitude: float | None,
        longitude: float | None,
    ) -> int:
        return await self._db.execute_returning_id(
            "INSERT INTO event_pings (kind, name, address, latitude, longitude, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, name, address, latitude, longitude, utcnow_iso()),
        )

    async def unposted(self, limit: int = 10) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM event_pings WHERE posted_at IS NULL "
            "ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_posted(self, ping_id: int) -> None:
        await self._db.execute(
            "UPDATE event_pings SET posted_at = ? WHERE id = ?",
            (utcnow_iso(), ping_id),
        )


class BoardDeletionRepo:
    """Board request posts scheduled for later deletion (the 12h tidy-up).

    A post is scheduled once it's been handled; the sweep deletes it after
    ``due_at``. On failure the sweep bumps ``attempts`` and pushes ``due_at``
    out; after ``MAX_ATTEMPTS`` the row is dropped so it can't retry forever.
    """

    MAX_ATTEMPTS = 6

    def __init__(self, db: Database) -> None:
        self._db = db

    async def schedule(
        self, thread_id: int, post_id: int, *, due_at: str, reason: str | None = None
    ) -> None:
        """Queue a post for deletion at ``due_at``. Idempotent per (thread,
        post): a repeat keeps the earlier due time."""
        await self._db.execute(
            """
            INSERT INTO board_pending_deletions
                (thread_id, post_id, reason, due_at, attempts, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(thread_id, post_id) DO UPDATE SET
                due_at = MIN(due_at, excluded.due_at),
                reason = COALESCE(reason, excluded.reason)
            """,
            (int(thread_id), int(post_id), reason, due_at, utcnow_iso()),
        )

    async def due(self, *, limit: int = 50) -> list[aiosqlite.Row]:
        """Rows whose deletion time has arrived, soonest first."""
        async with self._db.conn.execute(
            "SELECT * FROM board_pending_deletions WHERE due_at <= ? "
            "ORDER BY due_at ASC LIMIT ?",
            (utcnow_iso(), limit),
        ) as cur:
            return await cur.fetchall()

    async def remove(self, row_id: int) -> None:
        await self._db.execute(
            "DELETE FROM board_pending_deletions WHERE id = ?", (row_id,)
        )

    async def bump(self, row_id: int, *, backoff_seconds: int, error: str | None) -> None:
        """Record a failed attempt and push the next try out by ``backoff_seconds``."""
        next_due = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=backoff_seconds)
        ).isoformat()
        await self._db.execute(
            "UPDATE board_pending_deletions SET attempts = attempts + 1, "
            "last_error = ?, due_at = ? WHERE id = ?",
            ((error or "")[:200] or None, next_due, row_id),
        )

    async def pending_count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM board_pending_deletions"
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0


class RemindersRepo:
    """Training-finished reminders for Discord-sourced requests."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        discord_user_id: int,
        channel_id: int | None,
        training: str,
        due_at: str,
        request_id: int | None = None,
    ) -> int:
        return await self._db.execute_returning_id(
            "INSERT INTO training_reminders "
            "(discord_user_id, channel_id, training, due_at, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (discord_user_id, channel_id, training, due_at, request_id, utcnow_iso()),
        )

    async def due(self, *, limit: int = 25) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM training_reminders "
            "WHERE posted_at IS NULL AND due_at <= ? ORDER BY due_at ASC LIMIT ?",
            (utcnow_iso(), limit),
        ) as cur:
            return await cur.fetchall()

    async def mark_posted(self, reminder_id: int) -> None:
        await self._db.execute(
            "UPDATE training_reminders SET posted_at = ? WHERE id = ?",
            (utcnow_iso(), reminder_id),
        )


class LinksRepo:
    """Discord <-> MissionChief identity links + the verification queue."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_by_discord(self, discord_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM member_links WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_by_mc(self, mc_user_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM member_links WHERE mc_user_id = ?", (mc_user_id,)
        ) as cur:
            return await cur.fetchone()

    async def all_approved(self) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM member_links WHERE status = 'approved'"
        ) as cur:
            return await cur.fetchall()

    async def upsert(
        self, discord_id: int, mc_user_id: int, *, status: str, reviewer_id: int = 0
    ) -> None:
        """One link per Discord account; an MC account can only be claimed
        once — claiming it steals it from a stale link (people re-verify
        after renames), which the UNIQUE index would otherwise block."""
        now = utcnow_iso()
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM member_links WHERE mc_user_id = ? AND discord_id != ?",
                (mc_user_id, discord_id),
            )
            await conn.execute(
                "INSERT INTO member_links "
                "(discord_id, mc_user_id, status, reviewer_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET mc_user_id = excluded.mc_user_id, "
                "status = excluded.status, reviewer_id = excluded.reviewer_id, "
                "updated_at = excluded.updated_at",
                (discord_id, mc_user_id, status, reviewer_id, now, now),
            )

    async def delete(self, discord_id: int) -> bool:
        n = await self._db.execute(
            "DELETE FROM member_links WHERE discord_id = ?", (discord_id,)
        )
        return n > 0

    # -- verification queue ------------------------------------------------

    async def queue_add(
        self, discord_id: int, *, mc_user_id: int | None,
        display_name: str | None, guild_id: int | None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO verify_queue "
            "(discord_id, mc_user_id, display_name, guild_id, attempts, enqueued_at) "
            "VALUES (?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(discord_id) DO NOTHING",
            (discord_id, mc_user_id, display_name, guild_id, utcnow_iso()),
        )

    async def queue_get(self, discord_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM verify_queue WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return await cur.fetchone()

    async def queue_all(self) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM verify_queue ORDER BY enqueued_at ASC"
        ) as cur:
            return await cur.fetchall()

    async def queue_bump(self, discord_id: int) -> None:
        await self._db.execute(
            "UPDATE verify_queue SET attempts = attempts + 1 WHERE discord_id = ?",
            (discord_id,),
        )

    async def queue_remove(self, discord_id: int) -> None:
        await self._db.execute(
            "DELETE FROM verify_queue WHERE discord_id = ?", (discord_id,)
        )


class TaxWarningsRepo:
    """Per-member tax (alliance donation) warning state for the automated
    5%-donation warning system."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, mc_user_id: int) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM tax_warnings WHERE mc_user_id = ?", (mc_user_id,)
        ) as cur:
            return await cur.fetchone()

    async def all_open(self) -> list[aiosqlite.Row]:
        """Members with an unresolved warning trail (count > 0, not kicked)."""
        async with self._db.conn.execute(
            "SELECT * FROM tax_warnings WHERE warning_count > 0 "
            "AND kicked_at IS NULL"
        ) as cur:
            return list(await cur.fetchall())

    async def record_warning(
        self, mc_user_id: int, username: str, *, count: int
    ) -> None:
        now = utcnow_iso()
        await self._db.execute(
            "INSERT INTO tax_warnings (mc_user_id, username, warning_count, "
            "last_warning_at, resolved_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(mc_user_id) DO UPDATE SET username = excluded.username, "
            "warning_count = excluded.warning_count, "
            "last_warning_at = excluded.last_warning_at, "
            "resolved_at = NULL, updated_at = excluded.updated_at",
            (mc_user_id, username, count, now, now),
        )

    async def mark_resolved(self, mc_user_id: int) -> None:
        """The member fixed their donation: warnings reset so they stop
        immediately, and a later dip starts over at warning 1 (a fresh
        friendly reminder — the old gap doesn't apply to a new dip)."""
        now = utcnow_iso()
        await self._db.execute(
            "UPDATE tax_warnings SET warning_count = 0, last_warning_at = NULL, "
            "kick_flagged_at = NULL, resolved_at = ?, updated_at = ? "
            "WHERE mc_user_id = ?",
            (now, now, mc_user_id),
        )

    async def mark_kick_flagged(self, mc_user_id: int) -> None:
        now = utcnow_iso()
        await self._db.execute(
            "UPDATE tax_warnings SET kick_flagged_at = ?, updated_at = ? "
            "WHERE mc_user_id = ?",
            (now, now, mc_user_id),
        )

    async def mark_kicked(self, mc_user_id: int) -> None:
        now = utcnow_iso()
        await self._db.execute(
            "UPDATE tax_warnings SET kicked_at = ?, updated_at = ? "
            "WHERE mc_user_id = ?",
            (now, now, mc_user_id),
        )

    async def clear(self, mc_user_id: int) -> None:
        """Forget a member entirely (left the alliance)."""
        await self._db.execute(
            "DELETE FROM tax_warnings WHERE mc_user_id = ?", (mc_user_id,)
        )

    async def reset_all(self) -> int:
        """Wipe every warning trail (admin reset). Returns rows removed."""
        return await self._db.execute("DELETE FROM tax_warnings")

    async def reset_by_username(self, username: str) -> int:
        """Wipe one member's warning trail by (case-insensitive) name."""
        return await self._db.execute(
            "DELETE FROM tax_warnings WHERE lower(username) = lower(?)",
            (username,),
        )


class MissionsForumRepo:
    """mission_key → forum-thread mapping for the missions-database forum.

    The primary key is what guarantees no mission is ever posted twice;
    ``content_hash`` (raw einsaetze.json data + format version) decides
    whether an existing post needs an in-place edit."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, mission_key: str) -> aiosqlite.Row | None:
        async with self._db.conn.execute(
            "SELECT * FROM missions_forum_posts WHERE mission_key = ?",
            (mission_key,),
        ) as cur:
            return await cur.fetchone()

    async def count(self) -> int:
        async with self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM missions_forum_posts"
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def all(self) -> list[aiosqlite.Row]:
        async with self._db.conn.execute(
            "SELECT * FROM missions_forum_posts"
        ) as cur:
            return list(await cur.fetchall())

    async def record(
        self, mission_key: str, thread_id: int, content_hash: str, name: str
    ) -> None:
        now = utcnow_iso()
        await self._db.execute(
            "INSERT INTO missions_forum_posts (mission_key, thread_id, name, "
            "content_hash, posted_at, updated_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(mission_key) DO UPDATE SET "
            "thread_id = excluded.thread_id, name = excluded.name, "
            "content_hash = excluded.content_hash, "
            "updated_at = excluded.updated_at, last_seen_at = excluded.last_seen_at",
            (mission_key, thread_id, name, content_hash, now, now, now),
        )

    async def touch_seen(self, mission_keys: list[str]) -> None:
        """Mark keys as still present in the JSON (bulk, no content change)."""
        if not mission_keys:
            return
        now = utcnow_iso()
        await self._db.conn.executemany(
            "UPDATE missions_forum_posts SET last_seen_at = ? WHERE mission_key = ?",
            [(now, key) for key in mission_keys],
        )
        await self._db.conn.commit()

    async def delete(self, mission_key: str) -> None:
        """Forget a mapping (e.g. its thread was deleted by hand)."""
        await self._db.execute(
            "DELETE FROM missions_forum_posts WHERE mission_key = ?",
            (mission_key,),
        )


def ny_period_keys(now_utc: dt.datetime | None = None) -> tuple[str, str]:
    """(daily, monthly) period keys for the current New York game day."""
    from zoneinfo import ZoneInfo

    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    return ny.strftime("%Y-%m-%d"), ny.strftime("%Y-%m")
