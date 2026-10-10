from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from .identifiers import Identifier, arxiv_base

ID_FIELDS = ("doi", "arxiv", "pmid", "pmcid", "acl", "openreview", "osf")


@dataclass
class Paper:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    acl: str | None = None
    openreview: str | None = None
    osf: str | None = None  # OSF preprint (PsyArXiv, SocArXiv, ...) or file id
    url: str | None = None  # a URL the user gave that is not one of the ids above
    # Publication details, from Crossref, for citations.
    pub_type: str | None = None  # Crossref type: journal-article, proceedings-article, posted-content, ...
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str | None = None

    @classmethod
    def from_identifier(cls, ident: Identifier) -> Paper:
        p = cls()
        if ident.kind == "url":
            p.url = ident.value
        else:
            setattr(p, ident.kind, ident.value)
        return p

    def ids(self) -> list[str]:
        """Keys that identify this paper in the library, e.g. ['doi:10.1/x', 'arxiv:2001.00001']."""
        out = []
        for f in ID_FIELDS:
            v = getattr(self, f)
            if v:
                v = arxiv_base(v) if f == "arxiv" else v
                out.append(f"{f}:{v.lower() if f == 'doi' else v}")
        if self.url:
            out.append(f"url:{self.url}")
        return out

    def update(self, **kw) -> None:
        """Fill in fields that are still empty; never overwrite what we already know."""
        for k, v in kw.items():
            if v in (None, "", []):
                continue
            if k == "doi":
                v = v.lower()
            if getattr(self, k) in (None, "", []):
                setattr(self, k, v)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Paper:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Candidate:
    """A place where the full text might be."""

    url: str
    kind: str  # "pdf" (expected PDF), "landing" (HTML page that links to it), "jats" (JATS XML), "html" (arXiv)
    source: str  # where we learned about it: "arxiv", "openalex", "publisher", ...
    via: str = "direct"  # "direct" (plain HTTP) or "browser"
    version: str | None = None  # submittedVersion / acceptedVersion / publishedVersion
    headers: tuple[tuple[str, str], ...] = ()
    companion: str | None = None  # a PDF of the same paper to take figure images from (for XML and HTML)

    def with_(self, **kw) -> Candidate:
        return replace(self, **kw)


@dataclass
class Attempt:
    url: str
    source: str
    via: str
    outcome: str  # ok | blocked | no-pdf | failed | skipped | rejected (another paper)
    detail: str = ""
