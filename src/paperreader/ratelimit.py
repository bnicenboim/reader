"""Per-host request budgets shared by every process on the machine.

An agent may run several `paper` commands at once, so the budget lives in a
SQLite file rather than in memory. Each request reserves the next free slot for
its host inside a write transaction, then sleeps until that slot arrives.

The same file remembers hosts whose human check was shown and not completed, and the URLs
that led to those checks, so that later processes do not open a window for them again.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .config import Limit

HOUR = 3600.0
DAY = 86400.0


class QuotaExceeded(Exception):
    def __init__(self, host: str, window: str, limit: int, retry_after: float):
        self.host = host
        self.window = window
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"{host}: reached {limit} requests per {window}; "
            f"next request allowed in {retry_after / 60:.0f} min"
        )


class RateLimiter:
    def __init__(
        self,
        db_path: Path,
        limit_for: Callable[[str], Limit],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, timeout=60, isolation_level=None)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, next_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (host TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS requests_host_at ON requests(host, at);
            CREATE TABLE IF NOT EXISTS checks (host TEXT PRIMARY KEY, at REAL NOT NULL, url TEXT);
            CREATE TABLE IF NOT EXISTS check_urls (url TEXT PRIMARY KEY, host TEXT NOT NULL);
            """
        )
        self._limit_for = limit_for
        self._clock = clock
        self._sleep = sleep

    def _check_window(self, host: str, start: float, window: float, name: str, cap: int | None) -> None:
        if cap is None:
            return
        count, oldest = self._db.execute(
            "SELECT COUNT(*), MIN(at) FROM requests WHERE host = ? AND at > ?", (host, start - window)
        ).fetchone()
        if count >= cap:
            raise QuotaExceeded(host, name, cap, oldest + window - self._clock())

    def acquire(self, host: str) -> float:
        """Block until a request to `host` is allowed. Returns the seconds waited."""
        host = host.lower()
        limit = self._limit_for(host)
        now = self._clock()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT next_at FROM hosts WHERE host = ?", (host,)).fetchone()
            start = max(now, row[0] if row else 0.0)
            self._check_window(host, start, HOUR, "hour", limit.per_hour)
            self._check_window(host, start, DAY, "day", limit.per_day)
            self._db.execute("INSERT INTO requests (host, at) VALUES (?, ?)", (host, start))
            self._db.execute(
                "INSERT INTO hosts (host, next_at) VALUES (?, ?) "
                "ON CONFLICT(host) DO UPDATE SET next_at = excluded.next_at",
                (host, start + limit.interval),
            )
            self._db.execute("DELETE FROM requests WHERE at < ?", (now - DAY,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        wait = start - now
        if wait > 0:
            self._sleep(wait)
        return wait

    def penalize(self, host: str, seconds: float) -> None:
        """Push the host's next slot back, e.g. after a 429 with Retry-After."""
        host = host.lower()
        until = self._clock() + seconds
        self._db.execute(
            "INSERT INTO hosts (host, next_at) VALUES (?, ?) "
            "ON CONFLICT(host) DO UPDATE SET next_at = MAX(next_at, excluded.next_at)",
            (host, until),
        )

    def mark_check(self, host: str, url: str) -> None:
        """Remember that `host` showed a human check that nobody completed, on the way to `url`."""
        self._db.execute("INSERT OR REPLACE INTO checks (host, at, url) VALUES (?, ?, ?)",
                         (host.lower(), self._clock(), url))
        self.note_check_url(url, host)

    def note_check_url(self, url: str, host: str) -> None:
        """Remember that `url` leads to the check at `host` (after redirects), without renewing the mark."""
        self._db.execute("INSERT OR REPLACE INTO check_urls (url, host) VALUES (?, ?)", (url, host.lower()))

    def unfinished_check(self, host: str, within: float) -> str | None:
        """The URL that led to a check at `host` nobody completed in the last `within` seconds, if any."""
        row = self._db.execute("SELECT url FROM checks WHERE host = ? AND at > ?",
                               (host.lower(), self._clock() - within)).fetchone()
        return row[0] if row else None

    def check_url_leads_to(self, url: str, within: float) -> str | None:
        """The host of a check nobody completed in the last `within` seconds that `url` led to, if any."""
        row = self._db.execute("SELECT c.host FROM check_urls u JOIN checks c ON c.host = u.host "
                               "WHERE u.url = ? AND c.at > ?", (url, self._clock() - within)).fetchone()
        return row[0] if row else None

    def clear_checks(self, host: str | None = None) -> None:
        """Forget the unfinished check at `host`, or at every host."""
        for table in ("checks", "check_urls"):
            if host is None:
                self._db.execute(f"DELETE FROM {table}")
            else:
                self._db.execute(f"DELETE FROM {table} WHERE host = ?", (host.lower(),))

    def usage(self) -> list[tuple[str, int, int]]:
        """(host, requests in the last hour, requests in the last day)."""
        now = self._clock()
        return self._db.execute(
            "SELECT host, SUM(at > ?), COUNT(*) FROM requests WHERE at > ? GROUP BY host ORDER BY host",
            (now - HOUR, now - DAY),
        ).fetchall()

    def close(self) -> None:
        self._db.close()
