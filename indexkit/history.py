"""Submission history in SQLite.

Lets a run skip URLs that were already accepted by an engine and have not changed since (based on the
sitemap lastmod), and gives an audit trail of what was sent, when, and what the API answered.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from indexkit.submitters.base import Status, SubmissionResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_submissions_engine_url ON submissions (engine, url, submitted_at);
"""


class History:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(str(path))
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def record(self, run_id: str, results: Iterable[SubmissionResult], when: datetime | None = None) -> None:
        stamp = (when or datetime.now(UTC)).isoformat()
        with self.connection:
            self.connection.executemany(
                "INSERT INTO submissions (run_id, engine, url, status, http_status, detail, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, r.engine, r.url, r.status.value, r.http_status, r.detail, stamp) for r in results],
            )

    def last_accepted(self, engine: str, urls: Iterable[str]) -> dict[str, datetime]:
        """Most recent accepted submission time per URL for this engine."""
        found: dict[str, datetime] = {}
        url_list = list(urls)
        for start in range(0, len(url_list), 500):  # stay under SQLite's variable limit
            chunk = url_list[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.connection.execute(
                f"SELECT url, MAX(submitted_at) FROM submissions WHERE engine = ? AND status = ? AND url IN ({placeholders}) GROUP BY url",
                [engine, Status.ACCEPTED.value, *chunk],
            )
            found.update({url: datetime.fromisoformat(stamp) for url, stamp in rows})
        return found

    def summary(self, limit: int = 20) -> list[tuple[str, str, str, int]]:
        """(run_id, engine, status, count) for the most recent runs."""
        rows = self.connection.execute(
            """
            SELECT run_id, engine, status, COUNT(*) FROM submissions
            WHERE run_id IN (SELECT run_id FROM submissions GROUP BY run_id ORDER BY MAX(submitted_at) DESC LIMIT ?)
            GROUP BY run_id, engine, status ORDER BY MAX(submitted_at) DESC, engine, status
            """,
            (limit,),
        )
        return [(str(a), str(b), str(c), int(d)) for a, b, c, d in rows]
