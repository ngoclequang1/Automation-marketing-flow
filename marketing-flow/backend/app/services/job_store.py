from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class JobStore:
    """Small persistent mapping for job state, safe across restarts and threads."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def __setitem__(self, job_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs(job_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (job_id, serialized, now, now),
            )

    def get(self, job_id: str, default: Any = None) -> Any:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def __getitem__(self, job_id: str) -> dict[str, Any]:
        payload = self.get(job_id)
        if payload is None:
            raise KeyError(job_id)
        return payload

    def __contains__(self, job_id: object) -> bool:
        return isinstance(job_id, str) and self.get(job_id) is not None

    def cleanup(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM jobs WHERE updated_at < ?", (cutoff,))
            return cursor.rowcount

    def recover_incomplete(self) -> int:
        """Mark jobs left in-flight by a previous process as interrupted."""
        now = datetime.now(timezone.utc).isoformat()
        recovered = 0
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT job_id, payload FROM jobs").fetchall()
            for job_id, serialized in rows:
                payload = json.loads(serialized)
                if payload.get("status") != "processing":
                    continue
                payload.update(
                    status="failed",
                    stage="interrupted",
                    error="The backend stopped before this job completed. Please start it again.",
                )
                connection.execute(
                    "UPDATE jobs SET payload = ?, updated_at = ? WHERE job_id = ?",
                    (json.dumps(payload, ensure_ascii=False), now, job_id),
                )
                recovered += 1
        return recovered
