"""SQLite storage: connection management + migrations.

One shared aiosqlite connection, WAL journal mode (safe against power
loss on the Pi), busy timeout, foreign keys on. Schema changes are
numbered SQL files in ``fra_bot/db/migrations/``; applied versions are
tracked in ``schema_migrations`` so upgrades are one-way and explicit.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _split_sql(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    Full-line ``--`` comments are dropped; the migration files contain no
    semicolons inside string literals, so splitting on ``;`` is safe here.
    """
    kept = [
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ]
    body = "\n".join(kept)
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


class Database:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        # Serializes explicit BEGIN..commit blocks. A single aiosqlite
        # connection is shared by every scheduler task, so without this
        # two coroutines could interleave their transactions on the one
        # connection (SQLite allows only one open transaction) — the
        # second BEGIN would fail and its rollback would abort the
        # first's in-flight writes. All multi-statement writes take this.
        self._tx_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    @asynccontextmanager
    async def transaction(self):
        """Run a serialized explicit transaction on the shared connection."""
        async with self._tx_lock:
            conn = self.conn
            await conn.execute("BEGIN")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """Serialized single-statement write. Returns rowcount.

        Reads never take the lock (WAL readers are non-blocking); only
        writes are serialized so they can't interleave into another
        coroutine's open transaction on the shared connection.
        """
        async with self._tx_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.rowcount

    async def execute_returning_id(self, sql: str, params: tuple = ()) -> int:
        async with self._tx_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 30000")
        await conn.execute("PRAGMA foreign_keys = ON")
        self._conn = conn
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def _migrate(self) -> None:
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        await self.conn.commit()

        async with self.conn.execute("SELECT version FROM schema_migrations") as cur:
            applied = {row["version"] for row in await cur.fetchall()}

        seen_versions: set[int] = set()
        for version, name, sql_path in self._discover_migrations():
            if version in seen_versions:
                raise RuntimeError(
                    f"Duplicate migration version {version:04d} "
                    f"({sql_path.name}); refusing to start"
                )
            seen_versions.add(version)
            if version in applied:
                continue
            log.info("Applying migration %04d_%s", version, name)
            statements = _split_sql(sql_path.read_text(encoding="utf-8"))
            try:
                # Each migration + its bookkeeping row commit together or
                # not at all. executescript() auto-commits between
                # statements, so a mid-migration failure there can't be
                # rolled back and bricks startup; running the statements
                # inside one explicit transaction fixes that. Combined
                # with idempotent DDL (IF NOT EXISTS), a replay after a
                # partial apply is harmless.
                async with self.transaction() as conn:
                    for statement in statements:
                        await conn.execute(statement)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (version, name, utcnow_iso()),
                    )
            except Exception:
                log.exception("Migration %04d_%s failed; database unchanged", version, name)
                raise

    @staticmethod
    def _discover_migrations() -> list[tuple[int, str, Path]]:
        result = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            match = _MIGRATION_RE.match(path.name)
            if not match:
                continue
            version = int(match.group(1))
            name = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
            result.append((version, name, path))
        return result
