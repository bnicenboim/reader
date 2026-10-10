"""BibTeX entries from the metadata `paper get` collects (Crossref for type, volume, issue and pages)."""

from __future__ import annotations

import re

from .library import STOPWORDS, slug, surname
from .models import Paper

ENTRY_TYPES = {
    "journal-article": "article",
    "proceedings-article": "inproceedings",
    "book-chapter": "incollection",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "dissertation": "phdthesis",
    "report": "techreport",
}
VENUE_FIELD = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}


def cite_key(paper: Paper) -> str:
    """Google Scholar style: surname, year, first word of the title (roseberry2014skype)."""
    who = slug(surname(paper.authors[0])).replace("-", "") if paper.authors else "anon"
    word = next((w for w in slug(paper.title or "").split("-") if w and w not in STOPWORDS), "")
    return f"{who}{paper.year or ''}{word}"


def _escape(value: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", " ".join(str(value).split()))


def _entry_type(paper: Paper) -> str:
    if paper.pub_type in ENTRY_TYPES:
        return ENTRY_TYPES[paper.pub_type]
    if paper.arxiv or paper.pub_type == "posted-content":
        return "misc"
    return "article" if paper.venue and paper.doi else "misc"


def bibtex(paper: Paper, key: str | None = None) -> str:
    kind = _entry_type(paper)
    fields: list[tuple[str, str]] = []
    if paper.authors:
        fields.append(("author", " and ".join(paper.authors)))
    if paper.title:
        fields.append(("title", paper.title))
    if paper.venue and kind in VENUE_FIELD:
        fields.append((VENUE_FIELD[kind], paper.venue))
    elif paper.venue and kind == "misc" and paper.venue != "arXiv":
        fields.append(("howpublished", paper.venue))
    fields += [(k, v) for k, v in (("year", paper.year), ("volume", paper.volume), ("number", paper.issue)) if v]
    if paper.pages:
        fields.append(("pages", re.sub(r"\s*[-–]+\s*", "--", paper.pages)))
    if paper.publisher and kind in ("book", "incollection", "inproceedings", "techreport"):
        fields.append(("publisher", paper.publisher))
    if paper.doi and not paper.doi.startswith("10.48550/"):
        fields.append(("doi", paper.doi))
    if paper.arxiv:
        fields += [("eprint", paper.arxiv), ("archiveprefix", "arXiv")]
    if not paper.doi and not paper.arxiv and paper.url:
        fields.append(("url", paper.url))
    body = ",\n".join(f"  {name} = {{{_escape(value) if name not in ('doi', 'url') else value}}}"
                      for name, value in fields)
    return f"@{kind}{{{key or cite_key(paper)},\n{body}\n}}"
