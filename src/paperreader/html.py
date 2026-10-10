"""Read the scholarly metadata that publisher landing pages embed, and spot bot walls."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit


@dataclass
class PageInfo:
    url: str
    title: str | None = None  # <title>
    meta: dict[str, list[str]] = field(default_factory=dict)
    pdf_links: list[str] = field(default_factory=list)  # <link rel=alternate type=application/pdf>

    def first(self, name: str) -> str | None:
        values = self.meta.get(name)
        return values[0] if values else None

    @property
    def pdf_url(self) -> str | None:
        """The Highwire `citation_pdf_url` tag that most publishers and repositories emit."""
        for name in ("citation_pdf_url", "eprints.document_url", "bepress_citation_pdf_url"):
            if v := self.first(name):
                return urljoin(self.url, v)
        if self.pdf_links:
            return urljoin(self.url, self.pdf_links[0])
        return _ieee_pdf_url(self.url)

    @property
    def refresh_url(self) -> str | None:
        """Target of a <meta http-equiv="refresh"> redirect (e.g. Elsevier's linkinghub)."""
        m = re.search(r"url\s*=\s*['\"]?([^'\">]+)", self.first("refresh") or "", re.IGNORECASE)
        return urljoin(self.url, m.group(1).strip()) if m else None

    @property
    def doi(self) -> str | None:
        for name in ("citation_doi", "dc.identifier", "prism.doi", "bepress_citation_doi"):
            for v in self.meta.get(name, []):
                m = re.search(r"10\.\d{4,9}/\S+", v)
                if m:
                    return m.group(0)
        return None



def _ieee_pdf_url(url: str) -> str | None:
    """IEEE Xplore pages have no citation_pdf_url; the PDF is found by the article number in the URL.

    Matches ieeexplore.ieee.org and its proxied form (ieeexplore-ieee-org.<proxy>).
    """
    parts = urlsplit(url)
    m = re.match(r"/document/(\d+)", parts.path)
    if m and (parts.hostname or "").startswith("ieeexplore"):
        return urljoin(url, f"/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}")
    return None

class _Parser(HTMLParser):
    def __init__(self, info: PageInfo):
        super().__init__(convert_charrefs=True)
        self.info = info
        self._in_title = False
        self._title: list[str] = []
        self._noscript = 0

    def handle_starttag(self, tag, attrs):
        if tag == "noscript":
            self._noscript += 1
        if self._noscript:
            # For browsers without JavaScript, e.g. Microsoft's sign-in page refreshes to /jsdisabled.
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if name and "content" in a:
                self.info.meta.setdefault(name, []).append(a["content"].strip())
        elif tag == "link":
            if "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() == "application/pdf":
                if a.get("href"):
                    self.info.pdf_links.append(a["href"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "noscript" and self._noscript:
            self._noscript -= 1

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)


def parse_page(html: str, url: str) -> PageInfo:
    info = PageInfo(url=url)
    p = _Parser(info)
    # Metadata lives in <head>; don't parse megabytes of body for it.
    head_end = html.lower().find("</head>")
    p.feed(html[: head_end + 7] if head_end != -1 else html[:500_000])
    p.close()
    title = " ".join("".join(p._title).split())
    info.title = title or None
    if m := DELIVER_FORM_RE.search(html):
        info.pdf_links.append(unescape(m.group(1)))
    if pdf := _sciencedirect_pdf(html):
        info.pdf_links.append(pdf)
    return info


def _sciencedirect_pdf(html: str) -> str | None:
    """ScienceDirect pages have no citation_pdf_url; the "View PDF" link is built from the page's data.

    `"pdfDownload":{"urlMetadata":{"queryParams":{"md5":..,"pid":..},"pii":..,"pdfExtension":"/pdfft",
    "path":"science/article/pii"}}` gives /science/article/pii/<pii>/pdfft?md5=..&pid=..
    """
    i = html.find('"pdfDownload":')
    if i == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, i + len('"pdfDownload":'))
        meta = data["urlMetadata"]
        return f"/{meta['path']}/{meta['pii']}{meta['pdfExtension']}?{urlencode(meta['queryParams'])}"
    except (ValueError, KeyError, TypeError):
        return None


# Annual Reviews (and other sites on the same platform, pub2web) offer the PDF only through a form in the
# page body, with no citation_pdf_url. A GET of the form's action returns the PDF too (10 October 2026).
DELIVER_FORM_RE = re.compile(r"""<form\b[^>]*\baction=["'](/deliver/fulltext/[^"']+?\.pdf\?[^"']*mimeType=pdf[^"']*)""",
                             re.IGNORECASE)


CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "checking your browser",
    "please verify you are a human",
    "are you a robot",
    "access denied",
    "verifying you are human",
    "security check",
    "bot verification",
    "one more step",
    "preparing your download",
    "human verification",  # AWS WAF's CAPTCHA (HTTP 405), e.g. dspace.mit.edu
)

CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "_cf_chl_opt",
    "cf-turnstile",
    "px-captcha",
    "captcha-delivery.com",  # DataDome
    "/_fs-ch-",  # proof-of-work interstitial used by e.g. PMC
    "__identity_pow",  # PMC's newer proof-of-work page; a browser solves it by itself
)


def cloudflare_error(html: str) -> str | None:
    """The code of a Cloudflare error page (1015: rate-limit ban, 1020: firewall rule), if this is one."""
    m = re.search(r'class="cf-error-code">\s*(\d{4})|error code: (\d{4})', html[:50_000], re.IGNORECASE)
    return (m.group(1) or m.group(2)) if m else None


# Pages that refuse headless Chromium but open in a visible window: ScienceDirect's (also through a proxy)
# names the HeadlessChrome user agent. IEEE Xplore answers headless Chromium with HTTP 418 instead.
HEADLESS_REFUSALS = ("there was a problem providing the content you requested",)


def refuses_headless(status: int, html: str) -> bool:
    """Is this a site's answer to headless Chromium, which a visible window would not get?"""
    if status == 418:
        return True
    # The whole page: ScienceDirect's message comes after some 770 kB of inline styles.
    return status == 403 and any(m in html.lower() for m in HEADLESS_REFUSALS)


def looks_like_challenge(html: str, title: str | None = None) -> bool:
    """Heuristic: is this an anti-bot interstitial rather than real content?"""
    if cloudflare_error(html):
        return False  # a block, not a check: nothing for a person to complete
    t = (title or "").strip().lower()
    if any(t.startswith(c) for c in CHALLENGE_TITLES):
        return True
    head = html[:200_000].lower()
    return any(m in head for m in CHALLENGE_MARKERS)


# Identity providers that institutional sign-in (EZproxy, Shibboleth, OpenAthens) passes through.
SIGN_IN_HOSTS = (
    "login.microsoftonline.com",
    "login.live.com",
    "accounts.google.com",
    "engine.surfconext.nl",
    "login.openathens.net",
    "my.openathens.net",
)
SIGN_IN_HOST_PREFIXES = ("adfs.", "idp.", "sso.", "wayf.", "shibboleth.")
SIGN_IN_PATHS = ("/adfs/ls", "/idp/profile/", "/saml2", "/simplesaml/", "/cas/login", "/shibboleth.sso/")


def looks_like_sign_in(url: str, proxy_host: str | None = None) -> bool:
    """Is this page part of an institutional sign-in rather than the content it leads to?"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    if proxy_host and host == proxy_host.lower() and path.startswith("/login"):
        return True  # EZproxy's own login page, or its hand-off to the identity provider
    return (
        host in SIGN_IN_HOSTS
        or host.startswith(SIGN_IN_HOST_PREFIXES)
        or any(p in path for p in SIGN_IN_PATHS)
    )
