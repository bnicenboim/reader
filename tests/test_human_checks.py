"""Human checks in the browser: asked once, remembered, capped, and skipped when the window is closed."""

from urllib.parse import urlsplit

import httpx
import pytest
from conftest import make_pdf

from paperreader import fetch as fetch_mod
from paperreader.browser import (
    CHALLENGE_HEADER,
    SIGN_IN,
    Browser,
    BrowserUnavailable,
    CheckAbandoned,
    PersonWait,
    _launch_args,
)
from paperreader.fetch import Fetcher, FetchFailed
from paperreader.http import Response
from paperreader.identifiers import parse
from paperreader.library import Library

DOI = "10.1126/science.198.4316.519"
SCIENCE_PDF = f"https://www.science.org/doi/pdf/{DOI}"
DOI_URL = f"https://doi.org/{DOI}"
PDF = make_pdf(["Selective Vocal Learning in a Sparrow", "Peter Marler and Susan Peters"])


class FakeBrowser:
    """Stands in for `Browser`: each page ends at a human check that a person passes, ignores, or closes."""

    def __init__(self, person="ignores", lands_on=None, sign_in=False):
        self.person = person  # "passes" | "ignores" | "closes"
        self.lands_on = lands_on or {}  # requested URL -> URL of the page after redirects (doi.org: Science)
        self.sign_in = sign_in
        self.visible = False
        self.wait = None
        self.fetched = []  # (url, visible, human_wait)
        self.asked = []  # hosts a person was asked about
        self.closed = 0

    def reopen(self, visible):
        self.visible = visible

    def close(self):
        self.closed += 1

    def fetch(self, url, human_wait=0.0, may_ask=None):
        self.wait = None
        self.fetched.append((url, self.visible, human_wait))
        page = self.lands_on.get(url) or url.replace("https://doi.org/", "https://www.science.org/doi/")
        host = urlsplit(page).hostname
        if human_wait and (may_ask is None or may_ask(host)):
            self.asked.append(host)
            passed = self.person == "passes"
            self.wait = PersonWait(host, self.sign_in, seconds=5 if passed else human_wait, passed=passed)
            if self.person == "closes":
                raise CheckAbandoned(host)
            if passed:
                return Response(page, 200, httpx.Headers({"content-type": "application/pdf"}), PDF)
        headers = httpx.Headers({"content-type": "text/html", CHALLENGE_HEADER: SIGN_IN if self.sign_in else "1"})
        return Response(page, 401 if self.sign_in else 403, headers, b"<html><title>Just a moment...</title></html>")


@pytest.fixture
def setup(config, client, router, monkeypatch):
    """The Science paper of issue #4: plain HTTP is refused, so both publisher URLs go to the browser."""
    monkeypatch.setattr(fetch_mod, "display_available", lambda: True)
    config.browser_enabled = True
    router.add(SCIENCE_PDF, status=403)
    router.add(DOI_URL, status=403)

    def make(browser, **kw):
        f = Fetcher(config, client, Library(config.library_dir), log=lambda s: None, **kw)
        f._browser = browser
        return f

    return make


def test_a_check_nobody_completes_is_shown_once(setup, client, config):
    browser = FakeBrowser()
    f = setup(browser)
    with pytest.raises(FetchFailed) as e:
        f.get(parse(DOI))
    # One window, for the first page; doi.org leads to the same site and gets none.
    assert browser.asked == ["www.science.org"]
    assert browser.fetched == [(SCIENCE_PDF, False, 0.0), (SCIENCE_PDF, True, 120.0), (DOI_URL, False, 0.0)]
    assert not browser.visible  # hidden again once nobody is being asked anything
    assert [a.detail for a in e.value.attempts if a.via == "browser"] == ["bot check", "bot check at www.science.org"]
    assert e.value.reason == "human check at www.science.org"
    assert any("paper pending --retry" in h for h in e.value.hints)
    assert client.limiter.unfinished_check("science.org", config.browser_check_memory) == SCIENCE_PDF


def test_later_runs_skip_the_site_unless_rechecking_and_a_passed_check_clears_it(setup, client, config):
    with pytest.raises(FetchFailed):
        setup(FakeBrowser()).get(parse(DOI))

    # The next process loads neither page: not Science's, nor doi.org's, which led there in the browser (#7).
    browser = FakeBrowser()
    with pytest.raises(FetchFailed) as e:
        setup(browser).get(parse(DOI))
    assert browser.fetched == []
    assert [(a.url, a.detail) for a in e.value.attempts if a.via == "browser"] == [
        (SCIENCE_PDF, "human check at www.science.org not completed earlier (--visible tries again)"),
        (DOI_URL, "human check at science.org not completed earlier (--visible tries again)")]
    assert e.value.reason == "human check at www.science.org"

    # `paper pending --retry` asks again; the person passes the check, which clears the mark.
    browser = FakeBrowser(person="passes")
    rec = setup(browser, recheck=True).get(parse(DOI))
    assert browser.asked == ["www.science.org"] and rec.source["via"] == "browser"
    assert client.limiter.unfinished_check("science.org", config.browser_check_memory) is None
    assert client.limiter.check_url_leads_to(DOI_URL, config.browser_check_memory) is None


def test_a_page_that_redirects_to_a_site_with_an_unfinished_check_is_not_loaded(setup, router):
    # Plain HTTP shows where doi.org leads, so the browser skips it in the first run already (#7).
    landing = f"https://www.science.org/doi/{DOI}"
    router.add(DOI_URL, status=302, headers={"location": landing})
    router.add(landing, status=403)
    for run in (1, 2):
        browser = FakeBrowser()
        with pytest.raises(FetchFailed) as e:
            setup(browser).get(parse(DOI))
        assert [u for u, _, _ in browser.fetched] == ([SCIENCE_PDF, SCIENCE_PDF] if run == 1 else [])
        assert [a.detail for a in e.value.attempts if a.url == DOI_URL and a.via == "browser"] == [
            "human check at www.science.org not completed" + (" earlier (--visible tries again)" if run == 2 else "")]


def test_explicit_visible_asks_again(setup):
    with pytest.raises(FetchFailed):
        setup(FakeBrowser()).get(parse(DOI))
    browser = FakeBrowser(person="passes")
    browser.visible = True
    setup(browser, visible=True).get(parse(DOI))
    assert browser.asked == ["www.science.org"]


def test_the_mark_expires(setup, client, config, clock):
    with pytest.raises(FetchFailed):
        setup(FakeBrowser()).get(parse(DOI))
    clock.t += config.browser_check_memory + 1
    assert client.limiter.unfinished_check("science.org", config.browser_check_memory) is None
    browser = FakeBrowser()
    with pytest.raises(FetchFailed):
        setup(browser).get(parse(DOI))
    assert browser.asked == ["www.science.org"]


def test_a_batch_asks_once_per_site(setup, router):
    other = "10.1126/science.aaa0001"
    router.add(f"https://www.science.org/doi/pdf/{other}", status=403)
    router.add(f"https://doi.org/{other}", status=403)
    browser = FakeBrowser()
    f = setup(browser, recheck=True)  # even in `paper pending --retry`
    for doi in (DOI, other):
        with pytest.raises(FetchFailed):
            f.get(parse(doi))
    assert browser.asked == ["www.science.org"]


def test_one_paper_waits_for_a_person_at_most_paper_wait(setup, config, router):
    config.proxy = "https://proxy.example.edu/login?url={url}"
    proxied_pdf = f"https://proxy.example.edu/login?url={SCIENCE_PDF}"
    browser = FakeBrowser(lands_on={proxied_pdf: "https://a.example/check", SCIENCE_PDF: "https://b.example/check",
                                    DOI_URL: "https://c.example/check"})
    with pytest.raises(FetchFailed) as e:
        setup(browser).get(parse(DOI))
    # 120 s for the first check, the remaining 60 s for the second; none left for a window at c.example.
    assert browser.asked == ["a.example", "b.example"]
    assert [w for _, visible, w in browser.fetched if visible] == [120.0, 60.0]
    assert browser.fetched[-1] == (DOI_URL, False, 0.0)
    # The proxy's other page leads to the same check, so it is skipped.
    assert ("skipped", "human check at a.example not completed") in {(a.outcome, a.detail) for a in e.value.attempts}


def test_closing_the_window_skips_the_paper(setup):
    browser = FakeBrowser(person="closes")
    f = setup(browser)
    with pytest.raises(FetchFailed) as e:
        f.get(parse(DOI))
    assert [u for u, _, _ in browser.fetched] == [SCIENCE_PDF, SCIENCE_PDF]  # doi.org is not tried
    assert browser.closed and not f.visible  # the next paper starts a new, hidden browser
    assert e.value.attempts[-1].outcome == "skipped"
    assert e.value.attempts[-1].detail == ("the window was closed during the human check at www.science.org: "
                                           "skipping this paper")
    assert e.value.reason == "human check at www.science.org"


def test_an_unfinished_sign_in_is_asked_once_per_run_and_not_remembered(setup, client, config, router):
    config.proxy = "https://proxy.example.edu/login?url={url}"
    other = "10.1126/science.aaa0001"
    router.add(f"https://www.science.org/doi/pdf/{other}", status=403)
    router.add(f"https://doi.org/{other}", status=403)
    browser = FakeBrowser(sign_in=True)
    f = setup(browser)
    for doi in (DOI, other):
        with pytest.raises(FetchFailed) as e:
            f.get(parse(doi))
        assert e.value.reason == "sign-in needed at the library proxy"
    assert browser.asked.count("proxy.example.edu") == 1  # not for each proxy URL, nor for the second paper
    assert client.limiter.unfinished_check("proxy.example.edu", config.browser_check_memory) is None


def test_the_browser_does_not_announce_automation(config):
    # The headless browser, the visible window for a check and `paper login` all launch with these (#9).
    for executable in (None, "/usr/bin/chromium"):
        config.browser_executable = executable
        args = _launch_args(config)
        assert args["ignore_default_args"] == ["--enable-automation"]
        assert args["args"] == ["--disable-blink-features=AutomationControlled"]


def test_a_page_in_the_real_browser_sees_no_webdriver(config, client):
    """With Playwright's Chromium, if installed; the page is local, so nothing goes over the network."""
    pytest.importorskip("playwright")
    browser = Browser(config, client.limiter)
    try:
        browser._start()
    except BrowserUnavailable:
        pytest.skip("no browser installed (paper install-browser)")
    try:
        page = browser._ctx.new_page()
        page.set_content("<p>a local page</p>")
        assert page.evaluate("navigator.webdriver") is False
    finally:
        browser.close()


def test_a_hung_browser_is_ended_and_the_next_page_starts_a_new_one(config, client, monkeypatch):
    """A Playwright call that never returns (seen twice on 10 October 2026) ends in BrowserHung, not a hang."""
    pytest.importorskip("playwright")
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from paperreader import browser as browser_mod

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>ok</title><p>a local page</p>")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    config.timeout = 1
    monkeypatch.setattr(browser_mod, "HANG_MARGIN", 1.0)  # deadline: 2 * 1 + 1 s
    snapshot = browser_mod._snapshot
    monkeypatch.setattr(browser_mod, "_snapshot", lambda page: page.wait_for_timeout(600_000))
    browser = Browser(config, client.limiter)
    try:
        try:
            browser._start()
        except BrowserUnavailable:
            pytest.skip("no browser installed (paper install-browser)")
        if browser._pid is None:
            pytest.skip("no profile lock to find the browser process (Windows)")
        start = time.monotonic()
        with pytest.raises(browser_mod.BrowserHung):
            browser.fetch(url)
        assert time.monotonic() - start < 10
        monkeypatch.setattr(browser_mod, "_snapshot", snapshot)
        assert b"a local page" in browser.fetch(url).content
    finally:
        browser.close()
        server.shutdown()
