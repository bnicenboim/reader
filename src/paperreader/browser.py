"""A real browser for sites that refuse plain HTTP clients, and for institutional sessions.

The browser runs with a private, persistent profile, so a login you complete once
(`paper login`) is reused later. When a site shows a human-verification page, the
browser is made visible and you complete the check yourself; closing the window or
the tab instead skips the paper.

The browser leaves out the switches that announce automation (`navigator.webdriver`, the
"controlled by automated test software" bar): Cloudflare's checks never passed while it showed
them, even when a person completed them (issue #9). It is not disguised beyond that; headless, its
user agent still says HeadlessChrome.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Config
from .html import looks_like_challenge, looks_like_sign_in, refuses_headless
from .http import Response
from .ratelimit import RateLimiter

INSTALL_HINT = ("install the browser extra (pip install 'paperreader[browser]'), then run `paper install-browser`, "
                "or set browser.channel = \"chrome\" to use an installed Chrome")
SKIPPED_RESOURCES = {"image", "media", "font"}
CHALLENGE_HEADER = "x-paperreader-challenge"  # set on responses that are an unsolved human check or sign-in
SIGN_IN = "sign-in"  # CHALLENGE_HEADER value when the page waits for an institutional sign-in

FETCH_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return {status: r.status, type: r.headers.get('content-type') || '', url: r.url, data: btoa(s)};
}"""


class BrowserUnavailable(Exception):
    pass


class BrowserHung(RuntimeError):
    """A page load ran far past its deadline, so the browser was ended; the next page starts a new one."""

    def __init__(self, host: str, seconds: float):
        super().__init__(f"the browser stopped responding at {host} for {seconds:.0f} s; it was restarted")


# Beyond the page timeout (twice: the page, then a download) and the wait for a person, how long a page
# load may take before the browser counts as hung. Twice on 10 October 2026 a Playwright call never returned
# while the browser sat idle (a proxied Elsevier page, headless); the cause is not known.
HANG_MARGIN = 30.0


class CheckAbandoned(Exception):
    """The window or tab was closed while we waited for a person to complete a check: skip the paper."""

    def __init__(self, host: str, sign_in: bool = False):
        self.host = host
        super().__init__(f"the window was closed during the {'sign-in' if sign_in else 'human check'} at {host}")


@dataclass
class PersonWait:
    """A check or sign-in that a person was asked to complete during one `Browser.fetch`."""

    host: str  # where it was shown (after redirects), not the host of the requested URL
    sign_in: bool  # an institutional sign-in rather than a human check
    seconds: float = 0.0  # how long we waited
    passed: bool = False  # whether the page got past it


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


def _forget_downloads(profile: Path) -> None:
    """Clear Chromium's download history.

    With an earlier download in its history (a Playwright temporary file, since deleted), a visible
    Chromium crashes with SIGTRAP as soon as the next download starts (seen with Chromium 153).
    """
    history = profile / "Default" / "History"
    if not history.exists():
        return
    try:
        with closing(sqlite3.connect(history)) as db, db:
            for table in ("downloads", "downloads_url_chains", "downloads_slices"):
                db.execute(f"DELETE FROM {table}")
    except sqlite3.Error:
        pass


SESSION_COOKIES_MAX_AGE = 24 * 3600  # servers expire their sessions sooner; this only bounds stale files


def _session_cookies_path(config: Config) -> Path:
    return config.state_dir / "session-cookies.json"


def save_session_cookies(config: Config, cookies: list[dict]) -> None:
    """Keep cookies that only last a browser session (EZproxy's, identity providers').

    Chromium drops them when it exits, which would undo a `paper login` as soon as its window closes.
    """
    path = _session_cookies_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"saved_at": time.time(), "cookies": [c for c in cookies if c.get("expires", -1) < 0]})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)


def load_session_cookies(config: Config) -> list[dict]:
    try:
        data = json.loads(_session_cookies_path(config).read_text())
    except (OSError, ValueError):
        return []
    if time.time() - data.get("saved_at", 0) > SESSION_COOKIES_MAX_AGE:
        return []
    return data.get("cookies", [])


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


# Playwright's default switches without the ones that tell pages the browser is automated (issue #9).
UNMARKED_ARGS = {"ignore_default_args": ["--enable-automation"],
                 "args": ["--disable-blink-features=AutomationControlled"]}


def _browser_pid(profile: Path) -> int | None:
    """The main browser process: Chromium's profile lock is a link to "<host>-<pid>" (Linux, macOS)."""
    try:
        return int(os.readlink(profile / "SingletonLock").rsplit("-", 1)[1])
    except (OSError, ValueError, IndexError):  # Windows has no such link
        return None


def _wait_for_exit(pid: int, seconds: float = 10.0) -> None:
    """Wait for an ended browser to be gone before a new one takes its profile; force it after `seconds`.

    A precaution: Chromium hands a launch over to a running instance with the same profile.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def _launch_args(config: Config) -> dict:
    """For `launch_persistent_context`: which browser, and the switches it runs with (headless or not)."""
    if config.browser_executable:
        return {"executable_path": config.browser_executable, **UNMARKED_ARGS}
    return {"channel": config.browser_channel, **UNMARKED_ARGS}


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
        self._gone = False  # the browser was closed by hand
        self._pid: int | None = None  # the browser process we launched, to end it if it hangs
        self._hung = False  # set by the watchdog when it ended the browser
        self.wait: PersonWait | None = None  # set by fetch()

    def _start(self) -> None:
        if self._ctx is not None and not self._gone:
            return
        if self._ctx is not None:
            self.close()  # someone closed the window: start a new browser
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
                _forget_downloads(profile)
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(profile), headless=not self.visible, accept_downloads=True, no_viewport=self.visible,
                    **_launch_args(self.config),
                )
                self._ctx.on("close", lambda _: setattr(self, "_gone", True))
                self._pid = _browser_pid(profile)
                if cookies := load_session_cookies(self.config):
                    self._ctx.add_cookies(cookies)
        except Exception as e:
            self.close()
            raise BrowserUnavailable(f"could not start the browser ({str(e).splitlines()[0]}); {INSTALL_HINT}") from e

    def close(self) -> None:
        ended = self._pid if self._hung else None
        try:
            # With cdp_url the browser is the user's own: leave it (and its context) running.
            if self._ctx is not None and not self.config.browser_cdp_url:
                try:
                    save_session_cookies(self.config, self._ctx.cookies())
                except Exception:  # the browser crashed or was closed by hand
                    pass
                self._ctx.close()
            if self._pw is not None:
                self._pw.stop()
        finally:
            self._pw = self._browser = self._ctx = self._pid = None
            self._gone = False
            if ended:
                _wait_for_exit(ended)
            if self._lock:
                self._lock.__exit__()
                self._lock = None

    def reopen(self, visible: bool) -> None:
        self.close()
        self.visible = visible

    def fetch(self, url: str, human_wait: float = 0.0, may_ask: Callable[[str], bool] | None = None) -> Response:
        """Load `url` like a person would. Returns the PDF if one arrives, else the final page's HTML.

        `human_wait`: how long to wait for someone to finish a verification check or sign-in.
        `may_ask(host)`: whether to wait for the check shown at `host`; if not, the check page is returned.
        Afterwards `self.wait` says where a person was asked, for how long, and whether the page got past it.
        Raises `CheckAbandoned` when the window or tab is closed during that wait, and `BrowserHung` when
        the page takes far longer than its deadline.
        """
        self.wait = None
        self._start()
        host = urlsplit(url).hostname or ""
        self.limiter.acquire(host)
        deadline = 2 * self.config.timeout + human_wait + HANG_MARGIN
        self._hung = False
        watchdog = threading.Timer(deadline, self._end_hung_browser) if self._pid else None
        if watchdog:
            watchdog.daemon = True
            watchdog.start()
        try:
            return self._load(url, human_wait, may_ask)
        except Exception as e:
            if self._hung:
                raise BrowserHung(host, deadline) from e
            raise
        finally:
            if watchdog:
                watchdog.cancel()

    def _end_hung_browser(self) -> None:
        """Called from the watchdog thread: end the browser, so that the call waiting on it fails."""
        self._hung = self._gone = True
        try:
            os.kill(self._pid, signal.SIGTERM)
        except (OSError, TypeError):
            pass

    def _load(self, url: str, human_wait: float, may_ask: Callable[[str], bool] | None) -> Response:
        from playwright.sync_api import Error as PlaywrightError

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
                if "Download is starting" not in str(e) and "ERR_ABORTED" not in str(e) and not page.is_closed():
                    raise
            asked, asked_at = None, 0.0  # the check or sign-in a person was asked to complete
            start = time.monotonic()
            while not downloads:
                html, title = _snapshot(page)
                challenged = html is not None and looks_like_challenge(html, title)
                # Sign-in hops through identity providers; some pass by themselves, others wait for the user.
                signing_in = self._signing_in(page.url)
                waiting = challenged or signing_in
                elapsed = time.monotonic() - start
                if html is not None and not waiting and ("last" in nav or elapsed > 3):
                    break
                if waiting and human_wait and asked is None:
                    host = urlsplit(page.url).hostname or ""
                    if may_ask is None or may_ask(host):
                        what = "sign in" if signing_in else f"complete the human check of {host}"
                        self.log(f"{what} in the browser window (waiting up to {human_wait:.0f} s)")
                        asked, asked_at = PersonWait(host, signing_in), time.monotonic()
                    else:
                        human_wait = 0
                if elapsed > (human_wait if waiting and human_wait else 15):
                    break
                try:
                    page.wait_for_timeout(500)
                except PlaywrightError:
                    if not page.is_closed():
                        raise
                    break  # a visible Chrome closes a new tab whose navigation turned into a download

            if asked is not None:
                asked.seconds = time.monotonic() - asked_at
                self.wait = asked
            if downloads:
                r = self._save_download(downloads[0])
                if self.wait:
                    self.wait.passed = True
                return r
            if page.is_closed():
                if asked is not None:
                    raise CheckAbandoned(asked.host, asked.sign_in)
                raise RuntimeError("the browser closed the page before anything arrived")

            html, title = _snapshot(page)
            if page.evaluate("document.contentType") == "application/pdf":
                r = self._fetch_in_page(page, page.url)
                if self.wait:
                    self.wait.passed = True
                return r
            last = nav.get("last")
            status = last.status if last is not None else 200
            headers = httpx.Headers(last.headers if last is not None else {"content-type": "text/html"})
            if html is not None and looks_like_challenge(html, title):
                status = 403
                headers[CHALLENGE_HEADER] = "1"
            elif not self.visible and refuses_headless(status, html or ""):
                # IEEE Xplore's and ScienceDirect's answer to headless Chromium; a visible window gets the page.
                headers[CHALLENGE_HEADER] = "1"
            elif self._signing_in(page.url):
                status = 401
                headers[CHALLENGE_HEADER] = SIGN_IN
            if self.wait:
                self.wait.passed = CHALLENGE_HEADER not in headers
            return Response(page.url, status, headers, (html or "").encode())
        finally:
            try:
                page.close()
            except PlaywrightError:  # the browser is gone
                pass

    def _signing_in(self, url: str) -> bool:
        return looks_like_sign_in(url, urlsplit(self.config.proxy).hostname if self.config.proxy else None)

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
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserUnavailable(f"no browser available; {INSTALL_HINT}") from None
    log = lambda s: print(s, file=sys.stderr)  # noqa: E731
    with _ProfileLock(config.state_dir / "browser.lock", log), sync_playwright() as p:
        _prefer_pdf_download(config.browser_profile)
        _forget_downloads(config.browser_profile)
        ctx = p.chromium.launch_persistent_context(
            str(config.browser_profile), headless=False, no_viewport=True, **_launch_args(config)
        )
        if cookies := load_session_cookies(config):
            ctx.add_cookies(cookies)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        log("Sign in in the browser window. Close the window when you are done; the session is kept.")
        # Once the window is closed the session cookies are gone, so save them while it is open.
        while True:
            try:
                save_session_cookies(config, ctx.cookies())
                ctx.wait_for_event("close", timeout=2000)
                break
            except PlaywrightTimeout:
                continue
            except PlaywrightError:
                break
