"""Parse the many ways people refer to a paper into a normalized identifier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

KINDS = ("doi", "arxiv", "pmid", "pmcid", "acl", "openreview", "osf", "url")

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
ARXIV_NEW_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")
ARXIV_OLD_RE = re.compile(r"[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?")
ACL_RE = re.compile(r"\d{4}\.[a-z0-9][a-z0-9\-]*\.\d+|[A-Z]\d{2}-\d{4}")
PMCID_RE = re.compile(r"PMC\d+", re.IGNORECASE)
OSF_ID_RE = re.compile(r"[a-z0-9]{5}(?:_v\d+)?")
# OSF preprint servers with their own domain; their links look like psyarxiv.com/<id>/download.
OSF_HOSTS = ("osf.io", "psyarxiv.com", "socarxiv.org", "edarxiv.org", "mindrxiv.org", "metaarxiv.org")

ARXIV_DOI_PREFIX = "10.48550/arxiv."
ACL_DOI_PREFIX = "10.18653/v1/"


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    # The URL the user gave, when the identifier was extracted from one.
    url: str | None = None

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


class UnrecognizedIdentifier(ValueError):
    pass


def normalize_doi(doi: str) -> str:
    doi = unquote(doi).strip().lower()
    doi = doi.rstrip(".,;")
    # Drop a trailing ")" that was not opened inside the DOI, e.g. "(doi:10.1/x)".
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    return doi


def arxiv_base(arxiv_id: str) -> str:
    """Strip the version suffix: 1706.03762v5 -> 1706.03762."""
    return re.sub(r"v\d+$", "", arxiv_id)


def osf_base(osf_id: str) -> str:
    """Strip the version suffix: xbyr3_v2 -> xbyr3."""
    return re.sub(r"_v\d+$", "", osf_id.lower())


def osf_from_url(url: str | None) -> str | None:
    """The OSF id in a preprint link: osf.io/xbyr3, osf.io/preprints/psyarxiv/xbyr3_v1, psyarxiv.com/qzbgx/download.

    Not osf.io/download/<id>, which is already a file.
    """
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host not in OSF_HOSTS:
        return None
    m = re.fullmatch(r"/(?:preprints/[a-z]+/)?([^/]+)(?:/download)?/?", parts.path.lower())
    return osf_base(m.group(1)) if m and OSF_ID_RE.fullmatch(m.group(1)) else None


def _from_doi(doi: str, url: str | None = None) -> Identifier:
    doi = normalize_doi(doi)
    if doi.startswith(ARXIV_DOI_PREFIX):
        return Identifier("arxiv", doi[len(ARXIV_DOI_PREFIX):], url)
    return Identifier("doi", doi, url)


def _from_url(url: str) -> Identifier:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = unquote(parts.path)
    query = parse_qs(parts.query)

    if host in ("doi.org", "dx.doi.org", "www.doi.org"):
        return _from_doi(path.lstrip("/"))

    if host.endswith("arxiv.org"):
        m = re.match(r"/(?:abs|pdf|html)/(.+?)(?:\.pdf)?/?$", path)
        if m and (ARXIV_NEW_RE.fullmatch(m.group(1)) or ARXIV_OLD_RE.fullmatch(m.group(1))):
            return Identifier("arxiv", m.group(1), url)

    if host == "pubmed.ncbi.nlm.nih.gov":
        m = re.match(r"/(\d+)/?$", path)
        if m:
            return Identifier("pmid", m.group(1), url)

    if host.endswith("ncbi.nlm.nih.gov") or host.endswith("europepmc.org"):
        m = PMCID_RE.search(path)
        if m:
            return Identifier("pmcid", m.group(0).upper(), url)

    if host == "aclanthology.org":
        m = re.match(r"/(.+?)(?:\.pdf)?/?$", path)
        if m and ACL_RE.fullmatch(m.group(1)):
            return Identifier("acl", m.group(1), url)

    if host == "openreview.net" and "id" in query:
        return Identifier("openreview", query["id"][0], url)

    if osf := osf_from_url(url):
        return Identifier("osf", osf, url)

    # Many publisher URLs embed the DOI: dl.acm.org/doi/10.1145/..., link.springer.com/article/10.1007/...
    m = DOI_RE.search(path) or DOI_RE.search(unquote(parts.query))
    if m:
        doi = re.sub(r"(\.pdf|/(full|abstract|pdf|epdf|html|fulltext))+$", "", m.group(0))
        doi = doi.split("&")[0]
        return _from_doi(doi, url)

    return Identifier("url", url, url)


def parse(text: str) -> Identifier:
    """Parse a DOI, arXiv id, PMID, PMCID, ACL Anthology id, OpenReview id, OSF id or URL."""
    s = text.strip().strip("<>").strip()
    if not s:
        raise UnrecognizedIdentifier("empty identifier")

    if re.match(r"https?://", s, re.IGNORECASE):
        return _from_url(s)

    prefix, sep, rest = s.partition(":")
    if sep:
        p = prefix.strip().lower()
        rest = rest.strip()
        if p == "doi":
            return _from_doi(rest)
        if p == "arxiv":
            return Identifier("arxiv", rest)
        if p == "pmid":
            return Identifier("pmid", rest)
        if p in ("pmc", "pmcid"):
            return Identifier("pmcid", rest.upper() if rest.upper().startswith("PMC") else f"PMC{rest}")
        if p == "acl":
            return Identifier("acl", rest)
        if p == "openreview":
            return Identifier("openreview", rest)
        if p == "osf" and OSF_ID_RE.fullmatch(rest.lower()):
            return Identifier("osf", osf_base(rest))
        if p == "url" and re.match(r"https?://", rest, re.IGNORECASE):  # as Identifier.__str__ writes it
            return _from_url(rest)

    if re.fullmatch(r"10\.\d{4,9}/\S+", s):
        return _from_doi(s)
    if ARXIV_NEW_RE.fullmatch(s) or ARXIV_OLD_RE.fullmatch(s):
        return Identifier("arxiv", s)
    if PMCID_RE.fullmatch(s):
        return Identifier("pmcid", s.upper())
    if ACL_RE.fullmatch(s):
        return Identifier("acl", s)

    raise UnrecognizedIdentifier(f"not a recognized paper identifier: {text!r}")
