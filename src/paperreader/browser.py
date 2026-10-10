"""A real browser for sites that refuse plain HTTP clients, and for institutional sessions.

The browser runs with a private, persistent profile, so a login you complete once
(`paper login`) is reused later. When a site shows a human-verification page, the
browser is made visible and you complete the check yourself; nothing here solves
CAPTCHAs or disguises the browser.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Config
from .html import looks_like_challenge
from .http import Response
from .ratelimit import RateLimiter

INSTALL_HINT = ("install the browser extra (pip install 'paperreader[browser]'), then run `paper install-browser`, "
                "or set browser.channel = \"chrome\" to use an installed Chrome")
SKIPPED_RESOURCES = {"image", "media", "font"}
CHALLENGE_HEADER = "x-paperreader-challenge"  # set on responses that are an unsolved human check

FETCH_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, type: r.headers.get('content-type') || '', url: r.url, data: btoa(s)};
}"""


class BrowserUnavailable(Exception):
    pass


def display_available() -> bool:
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _prefer_pdf_download(profile: Path) -> None:
    """Make Chromium save PDFs instead of opening its viewer, so we get the file once."""
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(prefs_path.read_text())
    except (OSError, ValueError):
        prefs = {}
    prefs.setdefault("plugins", {})["always_open_pdf_externally"] = True
    prefs.setdefault("download", {})["prompt_for_download"] = False
    prefs_path.write_text(json.dumps(prefs))


class _ProfileLock:
    """A browser profile can only be open in one process at a time."""

    def __init__(self, path: Path, log: Callable[[str], None]):
        self.path = path
        self.log = log
        self._f = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:  # Windows: Chromium itself refuses a second instance
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "w")
        try:
            fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.log("waiting for another paperreader process to finish with the browser...")
            fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._f:
            self._f.close()
            self._f = None


def _launch_args(config: Config) -> dict:
    if config.browser_executable:
        return {"executable_path": config.browser_executable}
    return {"channel": config.browser_channel}


def _route(route) -> None:
    if route.request.resource_type in SKIPPED_RESOURCES:
        route.abort()
    else:
        route.continue_()


class Browser:
    def __init__(
        self,
        config: Config,
        limiter: RateLimiter,
        visible: bool = False,
        log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    ):
        self.config = config
        self.limiter = limiter
        self.visible = visible
        self.log = log
        self._pw = None
        self._browser = None
        self._ctx = None
        self._lock = None

    def _start(self) -> None:
        if self._ctx is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserUnavailable(f"no browser available; {INSTALL_HINT}") from None
        self._pw = sync_playwright().start()
        try:
            if self.config.browser_cdp_url:
                self._browser = self._pw.chromium.connect_over_cdp(self.config.browser_cdp_url)
                self._ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            else:
                profile = self.config.browser_profile
                self._lock = _ProfileLock(self.config.state_dir / "browser.lock", self.log).__enter__()
                _prefer_pdf_download(profile)
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(profile), headless=not self.visible, accept_downloads=True, no_viewport=self.visible,
                    **_launch_args(self.config),
                )
        except Exception as e:
            self.close()
            raise BrowserUnavailable(f"could not start the browser ({str(e).splitlines()[0]}); {INSTALL_HINT}") from e

    def close(self) -> None:
        try:
            # With cdp_url the browser is the user's own: leave it (and its context) running.
            if self._ctx is not None and not self.config.browser_cdp_url:
                self._ctx.close()
            if self._pw is not None:
                self._pw.stop()
        finally:
            self._pw = self._browser = self._ctx = None
            if self._lock:
                self._lock.__exit__()
                self._lock = None

    def reopen(self, visible: bool) -> None:
        self.close()
        self.visible = visible

    def fetch(self, url: str, human_wait: float = 0.0) -> Response:
        """Load `url` like a person would. Returns the PDF if one arrives, else the final page's HTML.

        `human_wait`: how long to wait for someone to finish a verification check.
        """
        from playwright.sync_api import Error as PlaywrightError

        self._start()
        self.limiter.acquire(urlsplit(url).hostname or "")
        page = self._ctx.new_page()
        if not self.visible:
            page.route("**/*", _route)
        downloads, nav = [], {}
        page.on("download", lambda d: downloads.append(d))

        def on_response(r):
            if r.request.is_navigation_request() and r.frame == page.main_frame:
                nav["last"] = r

        page.on("response", on_response)
        timeout_ms = self.config.timeout * 1000
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightError as e:
                # Navigating to a file that downloads aborts the navigation; that's what we want.
                if "Download is starting" not in str(e) and "ERR_ABORTED" not in str(e):
                    raise
            told = False
            start = time.monotonic()
            while not downloads:
                html, title = _snapshot(page)
                challenged = html is not None and looks_like_challenge(html, title)
                elapsed = time.monotonic() - start
                if html is not None and not challenged and ("last" in nav or elapsed > 3):
                    break
                if challenged and human_wait and not told:
                    self.log(f"{urlsplit(url).hostname} wants a human check: complete it in the browser window "
                             f"(waiting up to {human_wait:.0f} s)")
                    told = True
                if elapsed > (human_wait if challenged and human_wait else 15):
                    break
                page.wait_for_timeout(500)

            if downloads:
                return self._save_download(downloads[0])

            html, title = _snapshot(page)
            if page.evaluate("document.contentType") == "application/pdf":
                return self._fetch_in_page(page, page.url)
            last = nav.get("last")
            status = last.status if last is not None else 200
            headers = httpx.Headers(last.headers if last is not None else {"content-type": "text/html"})
            if html is not None and looks_like_challenge(html, title):
                status = 403
                headers[CHALLENGE_HEADER] = "1"
            return Response(page.url, status, headers, (html or "").encode())
        finally:
            page.close()

    def _save_download(self, download) -> Response:
        fd, tmp = tempfile.mkstemp(suffix=".download")
        os.close(fd)
        try:
            download.save_as(tmp)
            data = Path(tmp).read_bytes()
        finally:
            os.unlink(tmp)
        if len(data) > self.config.max_pdf_bytes:
            raise ValueError(f"download larger than {self.config.max_pdf_bytes} bytes")
        ctype = "application/pdf" if data[:5] == b"%PDF-" else "application/octet-stream"
        return Response(download.url, 200, httpx.Headers({"content-type": ctype}), data)

    def _fetch_in_page(self, page, url: str) -> Response:
        """Get the bytes of a PDF the built-in viewer is showing, through the browser's own network stack."""
        r = page.evaluate(FETCH_JS, url)
        return Response(r["url"], r["status"], httpx.Headers({"content-type": r["type"]}), base64.b64decode(r["data"]))


def _snapshot(page) -> tuple[str | None, str | None]:
    try:
        return page.content(), page.title()
    except Exception:  # navigation in progress
        return None, None


def login(config: Config, url: str) -> None:
    """Open a visible browser on the private profile so the user can sign in; returns when it is closed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserUnavailable(f"no browser available; {INSTALL_HINT}") from None
    log = lambda s: print(s, file=sys.stderr)  # noqa: E731
    with _ProfileLock(config.state_dir / "browser.lock", log), sync_playwright() as p:
        _prefer_pdf_download(config.browser_profile)
        ctx = p.chromium.launch_persistent_context(
            str(config.browser_profile), headless=False, no_viewport=True, **_launch_args(config)
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        log("Sign in in the browser window. Close the window when you are done; the session is kept.")
        ctx.wait_for_event("close", timeout=0)
