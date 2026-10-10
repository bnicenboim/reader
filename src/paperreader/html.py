"""Read the scholarly metadata that publisher landing pages embed, and spot bot walls."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin


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
        return None

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


class _Parser(HTMLParser):
    def __init__(self, info: PageInfo):
        super().__init__(convert_charrefs=True)
        self.info = info
        self._in_title = False
        self._title: list[str] = []

    def handle_starttag(self, tag, attrs):
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
    return info


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
)

CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "_cf_chl_opt",
    "cf-turnstile",
    "px-captcha",
    "captcha-delivery.com",  # DataDome
    "/_fs-ch-",  # proof-of-work interstitial used by e.g. PMC
)


def looks_like_challenge(html: str, title: str | None = None) -> bool:
    """Heuristic: is this an anti-bot interstitial rather than real content?"""
    t = (title or "").strip().lower()
    if any(t.startswith(c) for c in CHALLENGE_TITLES):
        return True
    head = html[:200_000].lower()
    return any(m in head for m in CHALLENGE_MARKERS)
