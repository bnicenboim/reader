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
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from . import sources
from .browser import CHALLENGE_HEADER, SIGN_IN, Browser, BrowserUnavailable, display_available
from .config import Config
from .extract import is_pdf, jats_to_text, pdf_info, pdf_to_text
from .html import cloudflare_error, looks_like_challenge, parse_page
from .http import CloudflareBlocked, HTTPError, PoliteClient, RateLimited, Response, TooLarge
from .identifiers import Identifier
from .library import Library, Record
from .models import Attempt, Candidate, Paper
from .publishers import elsevier_candidates, proxied, proxy_host, publisher_candidates
from .ratelimit import QuotaExceeded

BLOCKED_STATUSES = {401, 402, 403, 418, 429, 451}  # 418: bot block (IEEE)
# Sources whose pages may unlock with a logged-in session; worth a second try in the browser.
SESSION_SOURCES = {"input", "publisher"}


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
    fmt: str  # "pdf" | "xml"
    candidate: Candidate
    url: str


@dataclass
class _Run:
    paper: Paper
    attempts: list[Attempt] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)  # (via, url)
    browser_queue: list[Candidate] = field(default_factory=list)
    exhausted: set[str] = field(default_factory=set)  # hosts out of budget
    cloudflare_blocked: set[str] = field(default_factory=set)  # hosts that answered with Cloudflare's error 1015
    looked_up: set[str] = field(default_factory=set)  # preprint server lookups done


class Fetcher:
    def __init__(
        self,
        config: Config,
        client: PoliteClient,
        library: Library,
        use_browser: bool = True,
        visible: bool | None = None,
        log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    ):
        self.config = config
        self.client = client
        self.library = library
        self.use_browser = use_browser and config.browser_enabled
        mode = config.browser_mode
        self.visible = (mode == "visible") if visible is None else visible
        self.may_show = visible is not False and mode == "auto" and display_available()
        self.log = log
        self._browser: Browser | None = None
        self._browser_error: str | None = None

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
        return self._store(paper, found, key)

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
            if self._browser_error:
                break
            if found := self._attempt(run, cand):
                return found
        return None

    def _fetch(self, cand: Candidate) -> Response:
        if cand.via == "direct":
            return self.client.get(
                cand.url,
                headers={"Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8", **dict(cand.headers)},
                max_bytes=self.config.max_pdf_bytes,
            )
        if self._browser is None:
            self._browser = Browser(self.config, self.client.limiter, visible=self.visible, log=self.log)
        r = self._browser.fetch(cand.url, human_wait=self.config.browser_challenge_wait if self.visible else 0)
        if CHALLENGE_HEADER in r.headers and not self.visible and self.may_show:
            # Let the user see the check and complete it; the session is kept afterwards.
            self.visible = True
            self._browser.reopen(visible=True)
            r = self._browser.fetch(cand.url, human_wait=self.config.browser_challenge_wait)
        return r

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
        try:
            r = self._fetch(cand)
        except QuotaExceeded as e:
            run.exhausted.add(e.host)
            note("skipped", str(e))
            return None
        except CloudflareBlocked as e:
            # Plain HTTP will not get through for a while; a browser sometimes does.
            run.cloudflare_blocked.add(e.host)
            note("blocked", str(e))
            self._queue_for_browser(run, cand)
            return None
        except RateLimited as e:
            run.exhausted.add(e.host)
            note("blocked", str(e))
            return None
        except BrowserUnavailable as e:
            self._browser_error = str(e)
            note("failed", str(e))
            return None
        except (httpx.HTTPError, TooLarge, ValueError) as e:
            note("failed", f"{type(e).__name__}: {e}")
            return None
        except Exception as e:  # browser errors (timeouts, crashed pages)
            if cand.via != "browser":
                raise
            note("failed", f"{type(e).__name__}: {str(e).splitlines()[0]}")
            return None

        if r.status in BLOCKED_STATUSES:
            check = r.headers.get(CHALLENGE_HEADER)
            if check:
                detail = "sign-in required" if check == SIGN_IN else "bot check"
            elif code := cloudflare_error(r.text):
                detail = f"blocked by Cloudflare at {host} (error {code})"
            else:
                detail = f"HTTP {r.status}"
            note("blocked", detail)
            self._queue_for_browser(run, cand)
            return None
        if r.status >= 400:
            note("failed", f"HTTP {r.status}")
            return None

        if is_pdf(r.content):
            if cand.source == "elsevier-api" and pdf_info(r.content)["pages"] <= 1:
                note("failed", "first-page preview only: not entitled from this network")
                return None
            note("ok", r.url if r.url != cand.url else "")
            return Found(r.content, "pdf", cand, r.url)

        head = r.content[:2000].lstrip()
        if cand.kind == "jats" and (head.startswith(b"<?xml") or b"<article" in head):
            if b"<body" not in r.content:
                # NCBI returns the front matter alone when the publisher does not allow XML downloads.
                note("failed", "XML without the full text")
                return None
            note("ok")
            return Found(r.content, "xml", cand, r.url)

        if "html" in r.content_type or head[:1] == b"<":
            page = parse_page(r.text, r.url)
            if looks_like_challenge(r.text, page.title):
                note("blocked", "bot check")
                self._queue_for_browser(run, cand)
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
            note("no-pdf", "page without a PDF link")
            if cand.via == "direct" and self._may_need_session(cand):
                run.browser_queue.append(cand)
            return None

        note("failed", f"unexpected content type {r.content_type or 'unknown'}")
        return None

    @staticmethod
    def _queue_for_browser(run: _Run, cand: Candidate) -> None:
        """Retry later in the browser: what refused plain HTTP may open there. XML is for APIs, not browsers."""
        if cand.via == "direct" and cand.kind != "jats":
            run.browser_queue.append(cand)

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

    def _store(self, paper: Paper, found: Found, key: str | None = None) -> Record:
        if found.fmt == "pdf":
            text = pdf_to_text(found.content)
            if not paper.title:
                paper.update(title=pdf_info(found.content)["title"])
            if len(text.replace("<!-- page", "")) < 200:
                text += "\n\n(No text layer found: this PDF is probably a scan.)"
        else:
            text, meta = jats_to_text(found.content)
            paper.update(**meta)
        if not paper.title and found.url:
            paper.update(title=urlsplit(found.url).path.rsplit("/", 1)[-1] or None)
        source = {
            "source": found.candidate.source,
            "url": found.url,
            "via": found.candidate.via,
            "version": found.candidate.version,
        }
        return self.library.save(paper, found.content, found.fmt, text, source, key=key)

    def _reason(self, run: _Run) -> tuple[str, str | None]:
        """Why nothing worked, in a few words, and the page a person should open."""
        attempts, paper = run.attempts, run.paper
        doi_url = f"https://doi.org/{paper.doi}" if paper.doi else paper.url
        via_proxy = proxied(self.config, doi_url) if self.config.proxy and doi_url else doi_url
        first = lambda pred: next((a for a in attempts if pred(a)), None)  # noqa: E731
        if first(lambda a: a.detail == "sign-in required"):
            return "sign-in needed at the library proxy", via_proxy
        if a := first(lambda a: a.detail.startswith("blocked by Cloudflare")):
            return a.detail.split(" (error")[0].split(";")[0], a.url
        if a := first(lambda a: a.detail == "bot check"):
            return f"human check at {urlsplit(a.url).hostname}", a.url
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
        if any(a.via == "browser" and a.detail == "bot check" for a in run.attempts) and not self.visible:
            hints.append("a site asked for a human check; rerun with --visible and complete it in the window")
        if any(a.detail == "sign-in required" for a in run.attempts):
            hints.append("the proxy asked you to sign in: run `paper login`, then try again")
        elif blocked and not self.config.proxy:
            hints.append("for paywalled papers: set institution.proxy in the config, or sign in on the "
                         "publisher's site once with `paper login <url>` (\"access through your institution\")")
        elif blocked:
            hints.append("if your proxy session expired, sign in again with `paper login`")
        if any(a.outcome == "skipped" for a in run.attempts):
            hints.append("a per-host request budget was used up; try later or raise it in [rate_limits]")
        return hints
