"""HTTP client that waits its turn, honours Retry-After and caches API answers."""

from __future__ import annotations

import email.utils
import json
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from . import __version__
from .config import Config
from .ratelimit import RateLimiter

MAX_REDIRECTS = 10
MAX_RETRY_WAIT = 120.0  # beyond this we give up rather than block the caller


class RateLimited(Exception):
    def __init__(self, host: str, retry_after: float | None):
        self.host = host
        self.retry_after = retry_after
        wait = f"; asked to wait {retry_after:.0f} s" if retry_after else ""
        super().__init__(f"{host} is rate limiting us{wait}")


class CloudflareBlocked(RateLimited):
    """Cloudflare's error 1015: the site's rate-limit rule banned this IP for a while. Waiting does not help."""

    def __init__(self, host: str):
        self.host = host
        self.retry_after = None
        Exception.__init__(self, f"blocked by Cloudflare at {host} (error 1015); waiting will not help")


class TooLarge(Exception):
    pass


class HTTPError(Exception):
    def __init__(self, url: str, status: int):
        self.url = url
        self.status = status
        super().__init__(f"{url}: HTTP {status}")


@dataclass
class Response:
    url: str  # final URL after redirects
    status: int
    headers: httpx.Headers
    content: bytes
    history: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers), errors="replace")

    def json(self):
        return json.loads(self.content)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _charset(headers: httpx.Headers) -> str:
    for part in headers.get("content-type", "").split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset" and v:
            return v.strip('"')
    return "utf-8"


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - (now if now is not None else time.time()))


def user_agent(email_addr: str | None) -> str:
    contact = f"; mailto:{email_addr}" if email_addr else ""
    return f"paperreader/{__version__} (+https://github.com/gchrupala/reader{contact})"


class Cache:
    """URL -> (status, body) store for API lookups, so repeated questions cost nothing."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, timeout=60, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, at REAL, status INTEGER, body BLOB)"
        )

    def get(self, key: str, ttl: float) -> tuple[int, bytes] | None:
        row = self._db.execute("SELECT at, status, body FROM cache WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row[0] < ttl:
            return row[1], row[2]
        return None

    def put(self, key: str, status: int, body: bytes) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO cache (key, at, status, body) VALUES (?, ?, ?, ?)",
            (key, time.time(), status, body),
        )


class PoliteClient:
    def __init__(
        self,
        config: Config,
        limiter: RateLimiter,
        cache: Cache | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = config
        self.limiter = limiter
        self.cache = cache
        self._client = httpx.Client(
            follow_redirects=False,
            timeout=config.timeout,
            headers={"User-Agent": user_agent(config.email)},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        retries: int = 3,
    ) -> Response:
        """Send a request, following redirects one hop at a time so each hop is rate limited."""
        history: list[str] = []
        attempt = 0
        while True:
            host = urlsplit(url).hostname or ""
            self.limiter.acquire(host)
            with self._client.stream(method, url, headers=headers) as r:
                if r.status_code == 429 and r.headers.get("server", "").lower() == "cloudflare":
                    if b"1015" in r.read()[:20_000]:
                        raise CloudflareBlocked(host)
                wait = retry_after_seconds(r.headers.get("retry-after"))
                # 429 always means "slow down"; 503 only when the server says when to come back
                # (otherwise it is often a bot wall, and hammering it would not help).
                if (r.status_code == 429 or (r.status_code == 503 and wait is not None)) and attempt < retries:
                    if wait is None:
                        wait = min(MAX_RETRY_WAIT, 5.0 * 2**attempt + random.uniform(0, 2))
                    if wait > MAX_RETRY_WAIT:
                        raise RateLimited(host, wait)
                    self.limiter.penalize(host, wait)
                    attempt += 1
                    continue
                if r.status_code == 429:
                    raise RateLimited(host, wait)
                if r.is_redirect and "location" in r.headers:
                    if len(history) >= MAX_REDIRECTS:
                        raise httpx.TooManyRedirects(f"too many redirects from {history[0]}")
                    history.append(url)
                    url = urljoin(url, r.headers["location"])
                    if r.status_code == 303:
                        method = "GET"
                    continue
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if max_bytes is not None and len(body) > max_bytes:
                        raise TooLarge(f"{url}: response larger than {max_bytes} bytes")
                return Response(str(r.url), r.status_code, r.headers, bytes(body), history)

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def get_cached(
        self,
        url: str,
        params: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
        secret_params: dict | None = None,
        ttl: float = 7 * 86400,
    ) -> tuple[int, bytes]:
        """GET an API endpoint; answers (including 404s) are cached for `ttl` seconds.

        `secret_params` (API keys) are sent but kept out of the cache key.
        """
        key = url + ("?" + urlencode(params) if params else "")
        if self.cache and (hit := self.cache.get(key, ttl)):
            return hit
        all_params = {**(params or {}), **(secret_params or {})}
        full = url + ("?" + urlencode(all_params) if all_params else "")
        r = self.get(full, headers=headers)
        if r.status >= 500:
            raise HTTPError(url, r.status)
        if self.cache and (r.ok or r.status in (400, 404, 410, 422)):
            self.cache.put(key, r.status, r.content)
        return r.status, r.content

    def get_json(self, url: str, params: dict | None = None, **kw):
        """Like get_cached, decoded. Returns None when the API says it has no such record."""
        headers = {"Accept": "application/json", **(kw.pop("headers", None) or {})}
        status, body = self.get_cached(url, params, headers=headers, **kw)
        if status in (400, 404, 410, 422):
            return None
        if not 200 <= status < 300:
            raise HTTPError(url, status)
        return json.loads(body)
