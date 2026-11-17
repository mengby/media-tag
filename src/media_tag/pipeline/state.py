"""Resume checkpoint: SQLite state keyed by SHA256, independent of the source tree."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

STATUSES = (
    "discovered",
    "preprocessed",
    "inferred",
    "publishing",
    "published",
    "failed",
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS files(
        sha256 TEXT PRIMARY KEY,
        path TEXT,
        size_bytes INTEGER,
        kind TEXT,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        result_json TEXT,
        updated_at TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS files_status ON files(status)",
    """CREATE TABLE IF NOT EXISTS duplicates(
        sha256 TEXT NOT NULL,
        path TEXT NOT NULL,
        first_path TEXT,
        seen_at TEXT NOT NULL,
        PRIMARY KEY(sha256, path))""",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State:
    """Async SQLite checkpoint; one writer connection is enough for this pipeline."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None

    async def open(self) -> "State":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA busy_timeout=30000")
        for statement in SCHEMA:
            await connection.execute(statement)
        await connection.commit()
        self._connection = connection
        return self

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _require(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("state database is not open")
        return self._connection

    async def done_hashes(self) -> set[str]:
        """Published hashes are skipped on restart; failed items are retried."""

        connection = self._require()
        cursor = await connection.execute("SELECT sha256 FROM files WHERE status='published'")
        rows = await cursor.fetchall()
        await cursor.close()
        return {row[0] for row in rows}

    async def get(self, sha256: str) -> dict[str, Any] | None:
        connection = self._require()
        cursor = await connection.execute("SELECT * FROM files WHERE sha256=?", (sha256,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def mark(
        self,
        sha256: str,
        status: str,
        *,
        path: Path | None = None,
        size_bytes: int | None = None,
        kind: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        attempt: bool = False,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown pipeline status: {status}")
        connection = self._require()
        now = utc_now()
        await connection.execute(
            """INSERT INTO files(sha256,path,size_bytes,kind,status,attempts,error,result_json,updated_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sha256) DO UPDATE SET
                 path=COALESCE(excluded.path, files.path),
                 size_bytes=COALESCE(excluded.size_bytes, files.size_bytes),
                 kind=COALESCE(excluded.kind, files.kind),
                 status=CASE WHEN files.status='published' THEN files.status ELSE excluded.status END,
                 attempts=files.attempts + excluded.attempts,
                 error=CASE WHEN excluded.status='published' THEN NULL ELSE excluded.error END,
                 result_json=COALESCE(excluded.result_json, files.result_json),
                 updated_at=excluded.updated_at""",
            (
                sha256,
                str(path) if path is not None else None,
                size_bytes,
                kind,
                status,
                1 if attempt else 0,
                error,
                json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
                now,
                now,
            ),
        )
        await connection.commit()

    async def record_duplicate(self, sha256: str, path: Path, first_path: Path) -> None:
        connection = self._require()
        await connection.execute(
            "INSERT OR IGNORE INTO duplicates(sha256,path,first_path,seen_at) VALUES(?,?,?,?)",
            (sha256, str(path), str(first_path), utc_now()),
        )
        await connection.commit()

    async def counts(self) -> dict[str, int]:
        connection = self._require()
        cursor = await connection.execute("SELECT status, count(*) FROM files GROUP BY status")
        rows = await cursor.fetchall()
        await cursor.close()
        counts = {status: 0 for status in STATUSES}
        for status, number in rows:
            counts[str(status)] = int(number)
        return counts

    async def failures(self, *, limit: int = 200) -> list[dict[str, Any]]:
        connection = self._require()
        cursor = await connection.execute(
            "SELECT * FROM files WHERE status='failed' ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]
