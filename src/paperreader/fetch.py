"""Find a paper's full text, trying the cheapest and most polite routes first.

Order of attempts:
  1. the local library (no network)
  2. a URL the user gave
  3. open repositories reachable from the ids alone (arXiv, ACL Anthology, OpenReview, PMC),
     and preprint servers through their APIs (bioRxiv, medRxiv, OSF)
  4. open-access locations from OpenAlex, Unpaywall and Semantic Scholar
  5. Elsevier's API (if you have a key) and the publisher's site over plain HTTP,
     which works when your network has IP-based access
  6. the institution's proxy, in a real browser
  7. that browser for whatever refused plain HTTP, reusing your logged-in sessions

A human check that nobody completes is asked once: the site's other pages, and pages elsewhere that
lead there (doi.org, the proxy), are skipped for the rest of the run, and for a while
(`browser.check_memory_hours`) in later runs that are not --visible.
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from . import sources
from .browser import CHALLENGE_HEADER, SIGN_IN, Browser, BrowserUnavailable, CheckAbandoned, display_available
from .config import Config
from .extract import is_pdf, jats_to_text, pdf_info, pdf_pages, pdf_to_text
from .figures import MARKER_RE, FigureSpec, caption_label, figure_line, image_line, pdf_figures, with_page_figures
from .html import cloudflare_error, looks_like_challenge, parse_page
from .html_paper import html_to_markdown, is_latexml
from .http import CloudflareBlocked, HTTPError, PoliteClient, RateLimited, Response, TooLarge
from .identifiers import Identifier
from .library import Library, Record
from .models import Attempt, Candidate, Paper
from .publishers import elsevier_candidates, proxied, proxy_host, publisher_candidates
from .ratelimit import QuotaExceeded
from .verify import check as check_document
from .verify import head_text

BLOCKED_STATUSES = {401, 402, 403, 418, 429, 451}  # 418: bot block (IEEE)
# Sources whose pages may unlock with a logged-in session; worth a second try in the browser.
SESSION_SOURCES = {"input", "publisher"}


# Attempt details that name the host of a human check: "bot check at H", "human check at H not completed",
# "the window was closed during the human check at H".
CHECK_HOST_RE = re.compile(r"^(?:bot check(?: at)?|human check at|the window was closed during the human check at)\s*"
                           r"([^\s:]*)")

VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, None: 2, "submittedVersion": 3}

# Metadata for ids that OpenAlex may not know.
METADATA_LOOKUPS = {
    "arxiv": sources.arxiv_metadata,
    "pmid": sources.europepmc_metadata,
    "pmcid": sources.europepmc_metadata,
    "acl": sources.acl_metadata,
    "openreview": sources.openreview_metadata,
}


def _version_rank(c: Candidate) -> int:
    return VERSION_RANK.get(c.version, 2)


class FetchFailed(Exception):
    def __init__(self, paper: Paper, attempts: list[Attempt], hints: list[str], reason: str = "",
                 open_url: str | None = None):
        self.paper = paper
        self.attempts = attempts
        self.hints = hints
        self.reason = reason  # why, in a few words
        self.open_url = open_url  # where a person could get it by hand
        super().__init__(f"could not get the full text of {paper.title or paper.ids()}")


class _AlreadyHave(Exception):
    def __init__(self, record: Record):
        self.record = record


@dataclass
class Found:
    content: bytes
    fmt: str  # "pdf" | "xml" | "html"
    candidate: Candidate
    url: str
    companion: bytes | None = None  # a PDF of the paper, for figure images


MAX_IMAGE_DOWNLOADS = 40  # figure images fetched one by one (arXiv HTML without a PDF), per paper
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


@dataclass
class _Run:
    paper: Paper
    attempts: list[Attempt] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)  # (via, url)
    browser_queue: list[Candidate] = field(default_factory=list)
    exhausted: set[str] = field(default_factory=set)  # hosts out of budget
    cloudflare_blocked: set[str] = field(default_factory=set)  # hosts that answered with Cloudflare's error 1015
    looked_up: set[str] = field(default_factory=set)  # preprint server lookups done
    unfinished: dict[str, str] = field(default_factory=dict)  # site -> the check there nobody completed
    landed: dict[str, str] = field(default_factory=dict)  # URL queued for the browser -> host plain HTTP ended at
    waited: float = 0.0  # seconds spent waiting for a person
    abandoned: bool = False  # the window was closed during a check: skip the paper


def _site(host: str) -> str:
    """The host a check belongs to: www.science.org and science.org share one."""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


class Fetcher:
    def __init__(
        self,
        config: Config,
        client: PoliteClient,
        library: Library,
        use_browser: bool = True,
        visible: bool | None = None,
        log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
        recheck: bool = False,
    ):
        self.config = config
        self.client = client
        self.library = library
        self.use_browser = use_browser and config.browser_enabled
        mode = config.browser_mode
        self.visible = (mode == "visible") if visible is None else visible
        self.may_show = visible is not False and mode == "auto" and display_available()
        self.keep_visible = self.visible  # asked for a window throughout; otherwise only during a check
        # Ask again at sites whose check was not completed in an earlier run (--visible, `pending --retry`).
        self.recheck = recheck or self.visible
        self.log = log
        self._browser: Browser | None = None
        self._browser_error: str | None = None
        self._unfinished: dict[str, str] = {}  # site -> the check there nobody completed in this run

    def close(self) -> None:
        if self._browser:
            self._browser.close()

    # -- public ---------------------------------------------------------------------------

    def get(self, ident: Identifier, force: bool = False, key: str | None = None) -> Record:
        """The paper from the library, or fetched into it; stored under `key` if given."""
        paper = Paper.from_identifier(ident)
        sources.derive_ids(paper)
        if not force and (rec := self.library.find(paper)):
            return self.library.rename(rec, key) if key else rec
        run = _Run(paper)
        try:
            found = self._search(run, ident, check_library=not force)
        except _AlreadyHave as e:
            return self.library.rename(e.record, key) if key else e.record
        if found is None:
            raise FetchFailed(paper, run.attempts, self._hints(run), *self._reason(run))
        self._complete_metadata(run)
        self._arxiv_year(run, found)
        self._lookup(run, sources.crossref)  # after the arXiv year: a published article's issue year wins
        found.companion = self._companion(run, found)
        return self._store(paper, found, key)

    def rebuild(self, rec: Record) -> Record:
        """Extract text and figures again from the stored document (after an improvement, say)."""
        fmt = next(k for k in ("html", "xml", "pdf") if k in rec.files)
        content = rec.files[fmt].read_bytes()
        companion = rec.files["pdf"].read_bytes() if fmt != "pdf" and "pdf" in rec.files else None
        cand = Candidate(rec.source.get("url") or "", fmt, rec.source.get("source") or "library",
                         via=rec.source.get("via") or "direct", version=rec.source.get("version"))
        return self._store(rec.paper, Found(content, fmt, cand, cand.url, companion), rec.key)

    def add(self, ident: Identifier, content: bytes, origin: str, key: str | None = None) -> Record:
        """Store a PDF (or JATS XML) the user downloaded by hand, with metadata as `get` collects it."""
        paper = self.describe(ident)
        fmt = "pdf" if is_pdf(content) else "xml"
        return self._store(paper, Found(content, fmt, Candidate(origin, fmt, "user", via="by hand"), origin), key)

    def describe(self, ident: Identifier) -> Paper:
        """Metadata only, as `get` would collect it, without downloading the paper."""
        paper = Paper.from_identifier(ident)
        sources.derive_ids(paper)
        run = _Run(paper)
        if metadata := METADATA_LOOKUPS.get(ident.kind):
            self._lookup(run, metadata)
        self._preprint_servers(run)
        self._lookup(run, sources.openalex)
        self._complete_metadata(run)
        self._lookup(run, sources.crossref)
        return paper

    def resolve(self, ident: Identifier) -> tuple[Paper, list[Candidate]]:
        """Metadata and every candidate location, without downloading anything."""
        paper = Paper.from_identifier(ident)
        sources.derive_ids(paper)
        cands, seen = [], set()
        for c in self._candidates(_Run(paper), ident, check_library=False):
            if c.url not in seen:
                seen.add(c.url)
                cands.append(c)
        cands += self._institutional(paper)
        return paper, cands

    # -- candidates -----------------------------------------------------------------------

    def _lookup(self, run: _Run, fn) -> list[Candidate]:
        try:
            return fn(self.client, self.config, run.paper)
        except (httpx.HTTPError, HTTPError, RateLimited, QuotaExceeded, ValueError, SyntaxError) as e:
            run.attempts.append(Attempt(fn.__name__, fn.__name__, "api", "failed", str(e)))
            self.log(f"  {'failed':8} {'api':7} {fn.__name__:16} ({e})")
            return []

    def _candidates(self, run: _Run, ident: Identifier, check_library: bool) -> Iterator[Candidate]:
        paper = run.paper
        if ident.url and ident.kind in ("doi", "url"):
            yield Candidate(ident.url, "pdf", "input")

        if metadata := METADATA_LOOKUPS.get(ident.kind):
            self._lookup(run, metadata)

        def check():
            sources.derive_ids(paper)
            if check_library and (rec := self.library.find(paper)):
                raise _AlreadyHave(rec)

        yield from self._preprint_servers(run)
        # OpenAlex first: one cheap call gives metadata, other ids and open-access copies.
        for lookup in (sources.openalex, sources.unpaywall, sources.semantic_scholar):
            found = self._lookup(run, lookup)
            check()
            # Repository copies first in case the lookup discovered new ids; versions of record first.
            found = sources.repository_candidates(paper) + self._preprint_servers(run) + found
            yield from sorted(found, key=_version_rank)
        yield from elsevier_candidates(self.config, paper)
        yield from publisher_candidates(paper)

    def _preprint_servers(self, run: _Run) -> list[Candidate]:
        """bioRxiv/medRxiv and OSF through their APIs, once their ids are known (a lookup may find them)."""
        p, out = run.paper, []
        biorxiv = bool(p.doi and sources.BIORXIV_DOI_RE.fullmatch(p.doi))
        for lookup, ready in ((sources.biorxiv, biorxiv), (sources.osf, bool(p.osf))):
            if ready and lookup.__name__ not in run.looked_up:
                run.looked_up.add(lookup.__name__)
                out += self._lookup(run, lookup)
        return out

    def _institutional(self, paper: Paper) -> list[Candidate]:
        if not (self.config.proxy and paper.doi):
            return []
        return [
            Candidate(proxied(self.config, c.url), c.kind, "institution", via="browser")
            for c in publisher_candidates(paper)
        ]

    # -- the search loop ------------------------------------------------------------------

    def _search(self, run: _Run, ident: Identifier, check_library: bool) -> Found | None:
        for cand in self._candidates(run, ident, check_library):
            if found := self._attempt(run, cand):
                return found
        if not self.use_browser:
            return None
        # The proxy first: without it a paywalled publisher page only fails, sometimes after
        # a human check that keeps the user waiting.
        later = self._institutional(run.paper) + [c.with_(via="browser") for c in run.browser_queue]
        for cand in later:
            if self._browser_error or run.abandoned:
                break
            if found := self._attempt(run, cand):
                return found
        return None

    def _fetch(self, run: _Run, cand: Candidate) -> Response:
        if cand.via == "direct":
            return self.client.get(
                cand.url,
                headers={"Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8", **dict(cand.headers)},
                max_bytes=self.config.max_pdf_bytes,
            )
        if self._browser is None:
            self._browser = Browser(self.config, self.client.limiter, visible=self.visible, log=self.log)
        r = self._browse(run, cand.url)
        if (CHALLENGE_HEADER in r.headers and not self.visible and self.may_show and self._wait_left(run) > 0
                and not self._unfinished_check(run, urlsplit(r.url).hostname or "")):
            # Let the user see the check and complete it; the session is kept afterwards.
            self.visible = True
            self._browser.reopen(visible=True)
            r = self._browse(run, cand.url)
        return r

    def _wait_left(self, run: _Run) -> float:
        """How long this paper may still wait for a person: one check's wait, within the paper's total."""
        return max(0.0, min(self.config.browser_challenge_wait, self.config.browser_paper_wait - run.waited))

    def _browse(self, run: _Run, url: str) -> Response:
        """One page in the browser. A person is asked about a check only where they have not given up on it."""
        wait = self._wait_left(run) if self.visible else 0.0
        try:
            r = self._browser.fetch(url, human_wait=wait, may_ask=lambda h: not self._unfinished_check(run, h))
        finally:
            if self._browser.wait:
                self._after_wait(run, url)
        if not self._browser.wait:
            self._note_redirect_to_check(run, url, r)
        return r

    def _note_redirect_to_check(self, run: _Run, url: str, r: Response) -> None:
        """A page that led to another site's check, which nobody completed: skip it from now on."""
        if r.headers.get(CHALLENGE_HEADER) in (None, SIGN_IN):
            return
        page_host = urlsplit(r.url).hostname or ""
        site = _site(urlsplit(url).hostname or "")
        if _site(page_host) != site and (why := self._unfinished_check(run, page_host)):
            run.unfinished.setdefault(site, why)  # this paper's other pages there (doi.org, the proxy)
            self.client.limiter.note_check_url(url, _site(page_host))  # this page, in later runs

    def _after_wait(self, run: _Run, url: str) -> None:
        """Remember whether the person got past the check, so a check nobody completes is not shown again."""
        w = self._browser.wait
        run.waited += w.seconds
        site = _site(w.host)
        if w.passed:
            self._unfinished.pop(site, None)
            self.client.limiter.clear_checks(site)
            return
        why = f"{'sign-in' if w.sign_in else 'human check'} at {w.host} not completed"
        # Other pages of the paper at the same address lead to the same check.
        run.unfinished.update({site: why, _site(urlsplit(url).hostname or ""): why})
        self._unfinished[site] = why
        if not w.sign_in:  # `paper login` is the way to sign in; a check has no other way
            self.client.limiter.mark_check(site, url)
        if not self.keep_visible:
            self.visible = False  # nobody is being asked anything now: hide the window
            self._browser.reopen(visible=False)

    def _unfinished_check(self, run: _Run, host: str) -> str | None:
        """Why not to ask a person about a check at `host`: they did not complete one there."""
        site = _site(host)
        if why := run.unfinished.get(site) or self._unfinished.get(site):
            return why
        if not self.recheck and self.client.limiter.unfinished_check(site, self.config.browser_check_memory):
            return f"human check at {host} not completed earlier (--visible tries again)"
        return None

    def _leads_to_unfinished_check(self, run: _Run, url: str) -> str | None:
        """Why not to load `url` in the browser: it ends at a check nobody completed, after redirects."""
        if (landed := run.landed.get(url)) and (why := self._unfinished_check(run, landed)):
            return why
        if not self.recheck and (site := self.client.limiter.check_url_leads_to(url, self.config.browser_check_memory)):
            return f"human check at {site} not completed earlier (--visible tries again)"
        return None

    def _attempt(self, run: _Run, cand: Candidate, depth: int = 0) -> Found | None:
        if (cand.via, cand.url) in run.seen:
            return None
        run.seen.add((cand.via, cand.url))
        host = urlsplit(cand.url).hostname or ""

        def note(outcome: str, detail: str = "") -> None:
            run.attempts.append(Attempt(cand.url, cand.source, cand.via, outcome, detail))
            self.log(f"  {outcome:8} {cand.via:7} {cand.source:16} {cand.url}" + (f"  ({detail})" if detail else ""))

        if host in run.exhausted:
            note("skipped", "host budget used up")
            return None
        if host in run.cloudflare_blocked and cand.via == "direct":
            note("skipped", f"blocked by Cloudflare at {host}")
            self._queue_for_browser(run, cand)
            return None
        if cand.via == "browser" and (
                why := self._unfinished_check(run, host) or self._leads_to_unfinished_check(run, cand.url)):
            note("skipped", why)
            return None
        try:
            r = self._fetch(run, cand)
        except QuotaExceeded as e:
            run.exhausted.add(e.host)
            note("skipped", str(e))
            return None
        except CloudflareBlocked as e:
            # Plain HTTP will not get through for a while; a browser sometimes does.
            run.cloudflare_blocked.add(e.host)
            note("blocked", str(e))
            self._queue_for_browser(run, cand, e.host)
            return None
        except RateLimited as e:
            run.exhausted.add(e.host)
            note("blocked", str(e))
            return None
        except BrowserUnavailable as e:
            self._browser_error = str(e)
            note("failed", str(e))
            return None
        except CheckAbandoned as e:
            run.abandoned = True
            note("skipped", f"{e}: skipping this paper")
            self._browser.close()  # the next paper starts with a new browser, hidden unless asked otherwise
            self.visible = self.keep_visible
            return None
        except (httpx.HTTPError, TooLarge, ValueError) as e:
            note("failed", f"{type(e).__name__}: {e}")
            return None
        except Exception as e:  # browser errors (timeouts, crashed pages)
            if cand.via != "browser":
                raise
            note("failed", f"{type(e).__name__}: {str(e).splitlines()[0]}")
            return None

        if r.status in BLOCKED_STATUSES or (r.status >= 400 and self._is_check_page(r)):
            check = r.headers.get(CHALLENGE_HEADER)
            page_host = urlsplit(r.url).hostname or host
            if check:
                detail = "sign-in required" if check == SIGN_IN else "bot check"
                if check != SIGN_IN and _site(page_host) != _site(host):
                    detail += f" at {page_host}"  # where it was shown, after redirects
            elif code := cloudflare_error(r.text):
                detail = f"blocked by Cloudflare at {host} (error {code})"
            else:
                detail = f"HTTP {r.status}"
            note("blocked", detail)
            self._queue_for_browser(run, cand, page_host)
            return None
        if r.status >= 400:
            note("failed", f"HTTP {r.status}")
            return None

        if cand.kind == "html":  # arXiv's HTML version
            if is_latexml(r.content):
                note("ok")
                return Found(r.content, "html", cand, r.url)
            note("failed", "no HTML version")
            return None

        if is_pdf(r.content):
            if cand.source == "elsevier-api" and pdf_info(r.content)["pages"] <= 1:
                note("failed", "first-page preview only: not entitled from this network")
                return None
            if why := self._mismatch(run, r, "pdf"):
                note("rejected", why)
                return None
            note("ok", r.url if r.url != cand.url else "")
            return Found(r.content, "pdf", cand, r.url)

        head = r.content[:2000].lstrip()
        if cand.kind == "jats" and (head.startswith(b"<?xml") or b"<article" in head):
            if b"<body" not in r.content:
                # NCBI returns the front matter alone when the publisher does not allow XML downloads.
                note("failed", "XML without the full text")
                return None
            if why := self._mismatch(run, r, "xml"):
                note("rejected", why)
                return None
            note("ok")
            return Found(r.content, "xml", cand, r.url)

        if "html" in r.content_type or head[:1] == b"<":
            page = parse_page(r.text, r.url)
            if looks_like_challenge(r.text, page.title):
                note("blocked", "bot check")
                self._queue_for_browser(run, cand, urlsplit(r.url).hostname)
                return None
            sources.update_from_page(run.paper, page)
            if (pdf_url := page.pdf_url) and depth < 2:
                if cand.source == "institution" and not self._is_proxied(pdf_url):
                    pdf_url = proxied(self.config, pdf_url)
                note("page", "links to " + pdf_url)
                return self._attempt(run, cand.with_(url=pdf_url, kind="pdf"), depth + 1)
            if (target := page.refresh_url) and depth < 2:
                note("page", "redirects to " + target)
                return self._attempt(run, cand.with_(url=target), depth + 1)
            note("no-pdf", "page without a PDF link" + (f": {page.title[:80]!r}" if page.title else ""))
            if cand.via == "direct" and self._may_need_session(cand):
                self._queue_for_browser(run, cand, urlsplit(r.url).hostname)
            return None

        note("failed", f"unexpected content type {r.content_type or 'unknown'}")
        return None

    def _mismatch(self, run: _Run, r: Response, fmt: str) -> str | None:
        """Why this document is not the requested paper, if it evidently is not."""
        ids = set(run.paper.ids())
        if other := next((o for o in self.library.by_source_url(r.url) if not set(o.paper.ids()) & ids), None):
            return f"already in the library as {other.key}, another paper, from this URL"
        if not run.paper.title:
            self._complete_metadata(run)  # the title to compare with (lookups are cached)
        verdict = check_document(run.paper, head_text(r.content, fmt))
        if verdict.ok is False:
            return f"the download does not match the requested paper ({verdict.detail})"
        return None

    @staticmethod
    def _is_check_page(r: Response) -> bool:
        """A human check served with another error status (AWS WAF's CAPTCHA comes with 405)."""
        return "html" in r.content_type and looks_like_challenge(r.text, parse_page(r.text, r.url).title)

    @staticmethod
    def _queue_for_browser(run: _Run, cand: Candidate, landed: str | None = None) -> None:
        """Retry later in the browser: what refused plain HTTP may open there. XML is for APIs, not browsers.

        `landed`: the host plain HTTP ended at after redirects (from doi.org, say); the browser likely ends there too.
        """
        if cand.via == "direct" and cand.kind != "jats":
            run.browser_queue.append(cand)
            if landed:
                run.landed[cand.url] = landed

    @staticmethod
    def _may_need_session(cand: Candidate) -> bool:
        """Publisher pages, which may show the full text to a logged-in browser."""
        return cand.source in SESSION_SOURCES or (urlsplit(cand.url).hostname or "") in ("doi.org", "dx.doi.org")

    def _is_proxied(self, url: str) -> bool:
        host = proxy_host(self.config) or ""
        base = host.split(".", 1)[1] if host.startswith("login.") else host
        return bool(base) and (urlsplit(url).hostname or "").endswith(base)

    def _arxiv_year(self, run: _Run, found: Found) -> None:
        """For a copy from arXiv, take the year from arXiv.

        OpenAlex sometimes merges a paper with later reposts and reports their year
        (e.g. 2025 for "Attention Is All You Need").
        """
        if not (run.paper.arxiv and (urlsplit(found.url).hostname or "").endswith("arxiv.org")):
            return
        try:
            year = sources.arxiv_year(self.client, run.paper.arxiv)
        except (httpx.HTTPError, HTTPError, RateLimited, QuotaExceeded, SyntaxError) as e:
            self.log(f"  {'failed':8} {'api':7} {'arxiv_year':16} ({e})")
            return
        if year:
            run.paper.year = year

    def _complete_metadata(self, run: _Run) -> None:
        """The full text may have turned up before any metadata lookup ran. (Repeat lookups hit the cache.)"""
        for lookup in (sources.openalex, sources.semantic_scholar):
            p = run.paper
            if p.title and p.authors and p.year:
                return
            self._lookup(run, lookup)

    # -- results --------------------------------------------------------------------------

    def _companion(self, run: _Run, found: Found) -> bytes | None:
        """The PDF named by an XML or HTML candidate, to crop its figures and tables from."""
        url = found.candidate.companion
        if not url or found.fmt == "pdf" or (urlsplit(url).hostname or "") in run.cloudflare_blocked:
            return None
        try:
            r = self.client.get(url, max_bytes=self.config.max_pdf_bytes)
        except (httpx.HTTPError, HTTPError, RateLimited, QuotaExceeded, TooLarge) as e:
            self.log(f"  {'failed':8} {'direct':7} {'figures':16} {url}  ({e})")
            return None
        ok = r.ok and is_pdf(r.content)
        detail = "" if ok else f"  (HTTP {r.status})"
        self.log(f"  {'ok' if ok else 'failed':8} {'direct':7} {'figures':16} {url}{detail}")
        return r.content if ok else None

    def _store(self, paper: Paper, found: Found, key: str | None = None) -> Record:
        files: dict[str, bytes] = {}
        if found.fmt == "pdf":
            pages = pdf_pages(found.content)
            figures = pdf_figures(found.content, pages)
            text = pdf_to_text(found.content, with_page_figures(pages, figures))
            files = {f.name: f.png for f in figures}
            if not paper.title:
                paper.update(title=pdf_info(found.content)["title"])
            if len("".join(pages)) < 200:
                text += "\n\n(No text layer found: this PDF is probably a scan.)"
        else:
            if found.fmt == "xml":
                text, meta, specs = jats_to_text(found.content)
            else:
                text, meta, specs = html_to_markdown(found.content.decode("utf-8", "replace"), found.url)
            paper.update(**meta)
            text, files = self._place_figures(paper, text, specs, found.companion)
        if not paper.title and found.url:
            paper.update(title=urlsplit(found.url).path.rsplit("/", 1)[-1] or None)
        source = {
            "source": found.candidate.source,
            "url": found.url,
            "via": found.candidate.via,
            "version": found.candidate.version,
            "figures": len(files),  # also says that figures were looked for (papers stored earlier lack it)
        }
        attachments = {"pdf": found.companion} if found.companion else None
        return self.library.save(paper, found.content, found.fmt, text, source, key=key, figures=files,
                                 attachments=attachments)

    # -- figures of XML and HTML papers -----------------------------------------------------

    def _place_figures(self, paper: Paper, text: str, specs: list[FigureSpec],
                       companion: bytes | None) -> tuple[str, dict[str, bytes]]:
        """Put each figure's image where its marker is.

        Images come from the companion PDF (cropped, matched by label), from Europe PMC (open-access
        PMC articles), or from the image URLs of arXiv's HTML. When there is none, the text says where
        a person can see the figure.
        """
        crops = pdf_figures(companion) if companion else []
        by_label = {c.label: c for c in reversed(crops) if c.label}
        files: dict[str, bytes] = {}
        used: set[str] = set()
        pmc_images: dict[str, tuple[str, bytes]] | None = None
        downloads = [0]

        def resolve(m: re.Match) -> str:
            nonlocal pmc_images
            spec = specs[int(m.group(1))]
            # HTML captions start with their label ("Figure 1: ..."), JATS ones do not.
            alt = spec.caption if caption_label(spec.caption) else ": ".join(filter(None, (spec.label, spec.caption)))
            if (crop := by_label.get(spec.label)) is not None:
                used.add(crop.name)
                files[crop.name] = crop.png
                return image_line(alt or crop.caption, crop.name)
            images: list[tuple[str, bytes]] = []
            for src in spec.sources:
                if re.match(r"https?://", src):
                    if downloads[0] < MAX_IMAGE_DOWNLOADS and (img := self._download_image(src)):
                        downloads[0] += 1
                        images.append((PurePosixPath(urlsplit(src).path).name, img))
                elif paper.pmcid:
                    if pmc_images is None:
                        pmc_images = self._pmc_figures(paper.pmcid) or self._pmc_page_figures(paper, specs)
                    if (hit := pmc_images.get(PurePosixPath(src).stem)) is not None:
                        images.append(hit)
            lines = []
            for name, data in images:
                files[name] = data
                lines.append(image_line(alt or name, name))
            if not lines and spec.kind == "figure" and paper.pmcid and spec.ident:
                where = f"https://pmc.ncbi.nlm.nih.gov/articles/{paper.pmcid}/figure/{spec.ident}/"
                lines.append(f"(The image is not open to scripts; with the browser, `paper figures --rebuild` gets it; "
                             f"or see {where})")
            return "\n\n".join(lines)

        text = MARKER_RE.sub(resolve, text)
        rest = [c for c in crops if c.name not in used]
        if rest:  # crops whose label the text does not have
            files.update({c.name: c.png for c in rest})
            text += "\n\n## Figures and tables from the PDF\n\n" + "\n\n".join(figure_line(c) for c in rest)
        return re.sub(r"\n{3,}", "\n\n", text), files

    def _download_image(self, url: str) -> bytes | None:
        try:
            r = self.client.get(url, max_bytes=20 * 1024 * 1024)
        except (httpx.HTTPError, HTTPError, RateLimited, QuotaExceeded, TooLarge):
            return None
        return r.content if r.ok and r.content_type in IMAGE_TYPES else None

    def _pmc_page_figures(self, paper: Paper, specs: list[FigureSpec]) -> dict[str, tuple[str, bytes]]:
        """Figure images of a PMC author manuscript: its article page, in the browser, links them on NCBI's CDN.

        The page sits behind a proof-of-work check that the browser solves by itself; the CDN serves
        the images to plain HTTP.
        """
        wanted = {PurePosixPath(src).stem for spec in specs for src in spec.sources if not re.match(r"https?://", src)}
        if not (wanted and self.use_browser) or self._browser_error:
            return {}
        page = Candidate(f"https://pmc.ncbi.nlm.nih.gov/articles/{paper.pmcid}/", "landing", "pmc-figures",
                         via="browser")
        try:
            r = self._fetch(_Run(paper), page)
        except BrowserUnavailable as e:
            self._browser_error = str(e)
            return {}
        except Exception as e:  # browser errors (timeouts, crashed pages)
            self.log(f"  {'failed':8} {'browser':7} {'figures':16} {page.url}  ({type(e).__name__})")
            return {}
        links = re.findall(r'"(https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^"]+?\.(?:jpe?g|png|gif|webp))"', r.text)
        self.log(f"  {'ok' if links else 'failed':8} {'browser':7} {'figures':16} {page.url}")
        out: dict[str, tuple[str, bytes]] = {}
        for url in dict.fromkeys(links):
            name = PurePosixPath(urlsplit(url).path).name
            if (stem := PurePosixPath(name).stem) in wanted and stem not in out and (img := self._download_image(url)):
                out[stem] = (name, img)
        return out

    def _pmc_figures(self, pmcid: str) -> dict[str, tuple[str, bytes]]:
        """Figure images of an open-access PMC article, from Europe PMC's zip: file stem -> (name, data)."""
        url = f"{sources.EUROPEPMC}/{pmcid}/supplementaryFiles?includeInlineImage=true"
        try:
            r = self.client.get(url, max_bytes=self.config.max_pdf_bytes)
        except (httpx.HTTPError, HTTPError, RateLimited, QuotaExceeded, TooLarge) as e:
            self.log(f"  {'failed':8} {'direct':7} {'figures':16} {url}  ({e})")
            return {}
        if not (r.ok and r.content[:2] == b"PK"):  # author manuscripts: no files
            return {}
        self.log(f"  {'ok':8} {'direct':7} {'figures':16} {url}")
        out: dict[str, tuple[str, bytes]] = {}
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for info in z.infolist():
                name = PurePosixPath(info.filename).name
                stem, ext = PurePosixPath(name).stem, PurePosixPath(name).suffix.lower()
                if ext not in (".jpg", ".jpeg", ".png", ".gif"):
                    continue
                # Prefer the large JPEG or PNG over the GIF thumbnail.
                if stem not in out or (ext != ".gif" and info.file_size > len(out[stem][1])):
                    out[stem] = (name, z.read(info))
        return out

    def _reason(self, run: _Run) -> tuple[str, str | None]:
        """Why nothing worked, in a few words, and the page a person should open."""
        attempts, paper = run.attempts, run.paper
        doi_url = f"https://doi.org/{paper.doi}" if paper.doi else paper.url
        via_proxy = proxied(self.config, doi_url) if self.config.proxy and doi_url else doi_url
        first = lambda pred: next((a for a in attempts if pred(a)), None)  # noqa: E731
        if first(lambda a: a.detail == "sign-in required" or a.detail.startswith("sign-in at")):
            return "sign-in needed at the library proxy", via_proxy
        if a := first(lambda a: a.detail.startswith("blocked by Cloudflare")):
            return a.detail.split(" (error")[0].split(";")[0], a.url
        if a := first(lambda a: CHECK_HOST_RE.match(a.detail)):
            return f"human check at {CHECK_HOST_RE.match(a.detail).group(1) or urlsplit(a.url).hostname}", a.url
        if run.browser_queue and (self._browser_error or not self.use_browser):
            first_queued = run.browser_queue[0]
            if self._may_need_session(first_queued):
                return "no open copy; the publisher refused plain HTTP (needs the browser)", via_proxy
            return f"{urlsplit(first_queued.url).hostname} refused plain HTTP (needs the browser)", first_queued.url
        if first(lambda a: a.source in ("publisher", "institution") and a.outcome in ("blocked", "no-pdf")):
            if self.config.proxy:
                return "no open copy, and the publisher gave no PDF through the proxy", via_proxy
            return "no open copy; the publisher gave no PDF (no institutional proxy set)", doi_url
        return "no full text found", doi_url

    def _hints(self, run: _Run) -> list[str]:
        hints = []
        blocked = [c for c in run.browser_queue if self._may_need_session(c)]
        if self._browser_error:
            hints.append(self._browser_error)
        elif not self.use_browser and run.browser_queue:
            hints.append("some sites refused plain HTTP and the browser fallback is off "
                         "(enable it in the config, or drop --no-browser)")
        if any(a.via == "browser" and CHECK_HOST_RE.match(a.detail) for a in run.attempts) and not self.recheck:
            hints.append("a site asked for a human check: when you can do it, run `paper pending --retry` "
                         "(or rerun with --visible) and complete it in the window")
        if any(a.detail == "sign-in required" or a.detail.startswith("sign-in at") for a in run.attempts):
            hints.append("the proxy asked you to sign in: run `paper login`, then try again")
        elif blocked and not self.config.proxy:
            hints.append("for paywalled papers: set institution.proxy in the config, or sign in on the "
                         "publisher's site once with `paper login <url>` (\"access through your institution\")")
        elif blocked:
            hints.append("if your proxy session expired, sign in again with `paper login`")
        if any(a.outcome == "skipped" and (a.detail == "host budget used up" or ": reached " in a.detail)
               for a in run.attempts):
            hints.append("a per-host request budget was used up; try later or raise it in [rate_limits]")
        return hints
