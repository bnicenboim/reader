"""The sections of a paper's text, so an agent can read the methods without the references.

Text from JATS XML has Markdown headings. Text from PDFs has none, so headings are guessed:
numbered lines ("2.1 Participants") whose numbers run in order, Roman-numbered ones
("III. RESULTS"), and the usual section names on a line of their own ("Discussion").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PAGE_RE = re.compile(r"<!-- page (\d+) -->")
MARKDOWN_RE = re.compile(r"(#{2,6})\s+(.+)")
NUMBERED_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-Z][^\n]{1,80})")
ROMAN_RE = re.compile(r"([IVX]{1,5})\.\s+([A-Z][A-Za-z ,:&-]{2,80})")
APPENDIX_RE = re.compile(r"(?:Appendix|APPENDIX)(?:\s+[A-Z0-9]{1,2})?(?:[.:]\s+.{1,80})?")

KNOWN_NAMES = {
    "abstract", "introduction", "background", "related work", "method", "methods", "materials and methods",
    "methodology", "experiments", "experimental setup", "results", "results and discussion", "discussion",
    "general discussion", "conclusion", "conclusions", "limitations", "future work", "acknowledgments",
    "acknowledgements", "references", "bibliography", "literature cited", "references and notes",
    "supplementary material", "supplementary materials", "data availability", "data availability statement",
    "funding", "author contributions", "competing interests", "conflict of interest", "ethics statement",
}
REFERENCE_NAMES = {"references", "bibliography", "literature cited", "references and notes", "works cited"}


@dataclass
class Heading:
    line: int  # index into the text's lines
    level: int  # 1 for top-level sections
    title: str  # as printed: "2.1 Participants"
    name: str  # lower case, without its number: "participants"
    page: int | None  # PDF page, from the page markers


def _name(title: str) -> str:
    title = re.sub(r"^(?:\d{1,2}(?:\.\d{1,2})*\.?|[IVX]{1,5}\.)\s+", "", title)
    return " ".join(title.lower().rstrip(":. ").split())


def _plausible_title(title: str) -> bool:
    """Section titles are short, have words, and do not end like a sentence."""
    words = title.split()
    letters = sum(c.isalpha() for c in title)
    return 0 < len(words) <= 12 and letters >= 3 and letters >= len(title) / 2 and not title.endswith((".", ",", ";"))


def headings(text: str) -> list[Heading]:
    lines = text.split("\n")
    # Skip the header that `paper get` writes ("# Title", authors, source, then "---").
    start = next((i + 1 for i, line in enumerate(lines[:40]) if line.strip() == "---"), 0)
    out: list[Heading] = []
    page = None
    top = 0  # the last top-level section number, so "3 participants were excluded" in prose is not a heading
    for i in range(start, len(lines)):
        line = lines[i].strip()
        if m := PAGE_RE.fullmatch(line):
            page = int(m.group(1))
            continue
        if not line or len(line) > 90:
            continue
        level = None
        if m := MARKDOWN_RE.fullmatch(line):
            level, title = len(m.group(1)) - 1, m.group(2).strip()
        elif (m := NUMBERED_RE.fullmatch(line)) and _plausible_title(m.group(2)):
            parts = [int(n) for n in m.group(1).split(".")]
            if len(parts) == 1 and parts[0] in (top + 1, 1):  # numbering may restart after affiliations
                top = parts[0]
                level, title = 1, line
            elif len(parts) > 1 and parts[0] == top:
                level, title = len(parts), line
        elif (m := ROMAN_RE.fullmatch(line)) and _plausible_title(m.group(2)):
            level, title = 1, line
        elif _name(line) in KNOWN_NAMES or APPENDIX_RE.fullmatch(line):
            level, title = 1, line.rstrip(":")
        if level is not None:
            out.append(Heading(i, level, title, _name(title), page))
    # Numbered lines above the abstract are affiliations ("1 Tokyo Institute of Technology").
    abstract = next((k for k, h in enumerate(out) if h.name == "abstract"), 0)
    return [h for k, h in enumerate(out) if k >= abstract or not h.title[:1].isdigit()]


def _page_before(lines: list[str], index: int) -> str | None:
    """The last page marker above a line, so an excerpt still says where it is."""
    for line in reversed(lines[:index]):
        if PAGE_RE.fullmatch(line.strip()):
            return line.strip()
    return None


def _end(hs: list[Heading], k: int, n_lines: int) -> int:
    """Where section k ends: at the next heading of the same or a higher level."""
    return next((h.line for h in hs[k + 1:] if h.level <= hs[k].level), n_lines)


def find(hs: list[Heading], wanted: str) -> int | None:
    """Index of the heading that best matches `wanted`: same name, then a prefix, then a substring."""
    w = _name(wanted)
    for test in (lambda n: n == w, lambda n: n.startswith(w), lambda n: w in n):
        if (k := next((k for k, h in enumerate(hs) if test(h.name)), None)) is not None:
            return k
    return None


def extract(text: str, wanted: list[str]) -> tuple[str, list[str]]:
    """The text of the named sections, and the names that matched nothing."""
    lines = text.split("\n")
    hs = headings(text)
    parts, missing = [], []
    for w in wanted:
        k = find(hs, w)
        if k is None:
            missing.append(w)
            continue
        body = lines[hs[k].line:_end(hs, k, len(lines))]
        if (marker := _page_before(lines, hs[k].line)) is not None:
            body.insert(0, marker + "\n")
        parts.append("\n".join(body).strip())
    return "\n\n".join(parts), missing


def without_references(text: str) -> str:
    """The text with the reference list cut out (appendices after it stay)."""
    lines = text.split("\n")
    hs = headings(text)
    k = next((k for k, h in enumerate(hs) if h.name in REFERENCE_NAMES), None)
    if k is None:
        return text
    end = _end(hs, k, len(lines))
    rest = lines[end:]
    # Keep the page marker of the page the remaining text starts on.
    marker = next((line for line in reversed(lines[hs[k].line:end]) if PAGE_RE.fullmatch(line.strip())), None)
    if rest and marker and not PAGE_RE.fullmatch(rest[0].strip()):
        rest.insert(0, marker + "\n")
    return "\n".join(lines[:hs[k].line] + ["(references omitted)", ""] + rest).rstrip() + "\n"
