"""Repositories: all SQL lives here, grouped per domain.

Every repository takes the shared :class:`~fra_bot.db.database.Database`.
Timestamps in and out are UTC ISO-8601 strings.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import aiosqlite

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

    async def insert_batch(self, rows: list[dict[str, Any]]) -> int:
        """Insert parsed log rows; duplicates are skipped.

        ``rows`` must be in CHRONOLOGICAL order (oldest first) so that
        ascending id matches event order. Identical rows within a batch
        are disambiguated by occurrence_index; across batches the
        UNIQUE(signature, occurrence_index) constraint deduplicates
        re-scraped rows while keeping genuinely repeated events.
        """
        now = utcnow_iso()
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
                         contribution_amount, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    async def staging_finalize(self, done_key: str, next_page_key: str) -> int:
        """Copy staging into expenses (chronological), clear staging, and
        flip the backfill-done state — all in ONE transaction, so the
        ledger and the 'done' flag can never disagree after a crash."""
        now = utcnow_iso()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                """
                INSERT INTO expenses
                    (signature, raw_date, event_at, username, amount, description, scraped_at)
                SELECT signature, raw_date, event_at, username, amount, description, ?
                FROM expenses_backfill_staging ORDER BY id DESC
                """,
                (now,),
            )
            copied = cur.rowcount
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

    async def record_posts(self, thread_id: int, posts: list[dict[str, Any]]) -> list[int]:
        """Store posts; returns the post_ids that were new."""
        now = utcnow_iso()
        new_ids: list[int] = []
        async with self._db.transaction() as conn:
            for post in posts:
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
                if cur.rowcount > 0:
                    new_ids.append(post["post_id"])
        return new_ids


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

    async def waiting_requests(self, kind: str) -> list[aiosqlite.Row]:
        """Waiting requests whose next attempt is due (or unset)."""
        async with self._db.conn.execute(
            """
            SELECT * FROM automation_requests
            WHERE kind = ? AND status = 'waiting'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id ASC
            """,
            (kind, utcnow_iso()),
        ) as cur:
            return list(await cur.fetchall())

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
            "WHERE status IN ('pending', 'waiting')"
        ) as cur:
            row = await cur.fetchone()
        return row["n"]


def ny_period_keys(now_utc: dt.datetime | None = None) -> tuple[str, str]:
    """(daily, monthly) period keys for the current New York game day."""
    from zoneinfo import ZoneInfo

    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    return ny.strftime("%Y-%m-%d"), ny.strftime("%Y-%m")
