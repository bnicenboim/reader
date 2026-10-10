"""Check that a document is the paper it is stored as.

A browser download once put one paper's PDF under another paper's DOI. So before a download is
stored, and in `paper verify`, the first pages are compared with the metadata: they should show
the DOI (or the arXiv id or PMCID), or most of the title's distinctive words close together.
Text is compared as lowercase letters and digits only, so ligatures, accents, hyphenation at
line ends, punctuation and spacing do not matter.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .extract import _clean, is_pdf
from .library import STOPWORDS, TRANSLIT, Record
from .models import Paper

HEAD_PAGES = 2
TITLE_SHARE = 0.85  # of the title's distinctive words; papers of one field shared up to 4 of 5 of them
MIN_TEXT = 100  # letters and digits; less means a scan without a text layer


@dataclass
class Verdict:
    ok: bool | None  # None: nothing to compare (no metadata, or no text)
    detail: str


def head_text(content: bytes, fmt: str | None = None) -> str:
    """The text of a PDF's first pages, or the front matter of a JATS article (title, ids, abstract)."""
    if fmt == "pdf" or (fmt is None and is_pdf(content)):
        import pypdfium2 as pdfium

        try:
            doc = pdfium.PdfDocument(content)
        except pdfium.PdfiumError:
            return ""
        pages = []
        try:
            for i in range(min(HEAD_PAGES, len(doc))):
                page = doc[i]
                textpage = page.get_textpage()
                pages.append(_clean(textpage.get_text_range()))
                textpage.close()
                page.close()
        finally:
            doc.close()
        return "\n".join(pages)
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return ""
    front = next((el for el in root.iter() if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == "front"), root)
    return " ".join(" ".join(front.itertext()).split())


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.translate(TRANSLIT))  # also splits ligatures: "ﬁ" -> "fi"
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", _fold(text))


def title_words(title: str) -> list[str]:
    """The title's distinctive words: no stopwords, no words under three letters (numbers stay)."""
    title = html.unescape(re.sub(r"<[^>]+>", " ", title))  # OpenAlex titles can carry <i>, &amp;
    words = []
    for w in re.findall(r"\w+", _fold(title)):
        if w not in STOPWORDS and (len(w) >= 3 or w.isdigit()) and w not in words:
            words.append(w)
    return words


def _most_words_together(words: list[str], text: str, width: int) -> int:
    """The largest number of different `words` found within `width` characters of `text`."""
    hits = sorted((m.start(), i) for i, w in enumerate(words) for m in re.finditer(re.escape(w), text))
    best, counts, lo = 0, {}, 0
    for pos, i in hits:
        counts[i] = counts.get(i, 0) + 1
        while pos - hits[lo][0] > width:
            j = hits[lo][1]
            counts[j] -= 1
            if not counts[j]:
                del counts[j]
            lo += 1
        best = max(best, len(counts))
    return best


def check(paper: Paper, text: str) -> Verdict:
    """Is `text` (the first pages) the paper described by `paper`?"""
    squashed = _squash(text)
    if len(squashed) < MIN_TEXT:
        return Verdict(None, "no text on the first pages")
    ids = [("DOI", paper.doi), ("arXiv id", f"arxiv{paper.arxiv}" if paper.arxiv else None), ("PMCID", paper.pmcid)]
    for name, value in ids:
        if value and _squash(value) in squashed:
            return Verdict(True, f"{name} on the first pages")
    missing = " or ".join(name for name, value in ids if value)
    words = title_words(paper.title or "")
    if words:
        # Title lines can be interleaved with a second column or a running head, hence the slack.
        found = _most_words_together(words, squashed, 2 * len(_squash(paper.title)) + 200)
        if found >= math.ceil(TITLE_SHARE * len(words)):
            return Verdict(True, f"{found} of {len(words)} title words on the first pages")
        return Verdict(False, f"{found} of {len(words)} title words on the first pages"
                       + (f", and no {missing}" if missing else ""))
    if missing:
        return Verdict(False, f"no {missing} on the first pages, and no title to compare")
    return Verdict(None, "no DOI or title to compare")


def check_record(rec: Record) -> Verdict:
    """`check` for a paper in the library, on its stored PDF or XML."""
    if all(i.startswith("url:") for i in rec.paper.ids()):
        return Verdict(None, "fetched from a URL: its metadata came from the document itself")
    fmt = "pdf" if "pdf" in rec.files else "xml"
    path = rec.files.get(fmt)
    if path is None or not path.exists():
        return Verdict(None, "the document file is missing")
    return check(rec.paper, head_text(path.read_bytes(), fmt))
