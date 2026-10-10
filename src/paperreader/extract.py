"""Turn PDFs and JATS XML into plain text an agent (or a person) can read."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .figures import FigureSpec, caption_label, label_of, marker

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def is_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


# Publisher watermarks on every page, e.g. "Downloaded from http://royalsocietypublishing.org/... " / "by guest"
# (Royal Society), "Downloaded from https://academic.oup.com/... by guest on 4 March 2024" (OUP), or IEEE's
# "Authorized licensed use limited to: <institution>. Downloaded on ... from IEEE Xplore. Restrictions apply."
WATERMARK_RE = re.compile(r"^(?:Downloaded from https?://\S+.*\n(?:(?:by guest|on \d).*\n)*"
                          r"|Authorized licensed use limited to: .*Restrictions apply\.[ \t]*\n)", re.MULTILINE)
# pdfium puts a superscript on a line of its own: "p = 3.2e" / "−6" / ", d = 0.4", or "× 10" / "−6".
EXPONENT_RE = re.compile(r"(\d(?:e|\s?[×x·]\s?10))\n([−–-]?\d{1,3})(?:\n(?=[,;:.)\]]))?")
HAL_COVER_MARKS = ("HAL is a multi-disciplinary open access archive", "L’archive ouverte pluridisciplinaire HAL")


def _join_exponent(m: re.Match) -> str:
    base, exponent = m.group(1), m.group(2)
    return f"{base}{exponent}" if base.endswith("e") else f"{base}^{exponent}"


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # pdfium marks hyphens it knows to be soft (line-break hyphenation) with U+FFFE.
    text = re.sub("\ufffe\n?", "", text)
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = WATERMARK_RE.sub("", text + "\n")
    text = EXPONENT_RE.sub(_join_exponent, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_hal_cover(text: str) -> bool:
    """HAL puts a cover page with its bilingual disclaimer in front of every deposited PDF."""
    return text.startswith("HAL Id:") or any(m in text for m in HAL_COVER_MARKS)


def pdf_info(data: bytes) -> dict:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        meta = doc.get_metadata_dict()
        return {"pages": len(doc), "title": (meta.get("Title") or "").strip() or None}
    finally:
        doc.close()


def pdf_pages(data: bytes) -> list[str]:
    """The cleaned text of each page."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    pages = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            text = _clean(textpage.get_text_range())
            if i == 0 and len(doc) > 1 and _is_hal_cover(text):
                text = "(HAL cover page omitted)"
            pages.append(text)
            textpage.close()
            page.close()
    finally:
        doc.close()
    return pages


def pdf_to_text(data: bytes, pages: list[str] | None = None) -> str:
    """Text of every page, with `<!-- page N -->` markers so passages can be cited by page."""
    pages = pdf_pages(data) if pages is None else pages
    return "\n\n".join(f"<!-- page {i} -->\n\n{text}" for i, text in enumerate(pages, 1))


# --- JATS XML (PubMed Central, many OA publishers) --------------------------------------------


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _inline(el: ET.Element) -> str:
    out = [el.text or ""]
    for child in el:
        name = _local(child.tag)
        if name in ("fig", "table-wrap", "fn", "supplementary-material"):
            pass  # rendered as blocks elsewhere
        elif name == "math" or name == "inline-formula" or name == "disp-formula":
            tex = next((t for t in child.iter() if _local(t.tag) == "tex-math"), None)
            out.append(f"${(tex.text or '').strip()}$" if tex is not None else " ".join(child.itertext()))
        else:
            out.append(_inline(child))
        out.append(child.tail or "")
    return "".join(out)


def _squash(s: str) -> str:
    return " ".join(s.split())


def _caption(el: ET.Element, kind: str) -> str:
    label = _squash("".join(t.text or "" for t in el if _local(t.tag) == "label"))
    caption = next((c for c in el if _local(c.tag) == "caption"), None)
    text = _squash(" ".join(_inline(p) for p in caption)) if caption is not None else ""
    return f"**{label or kind}.** {text}".strip()


def _table(el: ET.Element) -> str:
    rows = []
    for tr in el.iter():
        if _local(tr.tag) == "tr":
            cells = [_squash(_inline(c)).replace("|", "\\|") for c in tr if _local(c.tag) in ("td", "th")]
            rows.append("| " + " | ".join(cells) + " |")
    if rows:
        width = rows[0].count(" | ") + 1
        rows.insert(1, "|" + " --- |" * width)
    return "\n".join(rows)


def _figure(el: ET.Element, kind: str, out: list[str], figs: list[FigureSpec]) -> None:
    """Caption, table, and a marker where the image goes."""
    out.append(_caption(el, kind.capitalize()))
    if kind == "table" and (table := next((t for t in el.iter() if _local(t.tag) == "table"), None)) is not None:
        out.append(_table(table))
    label = _squash("".join(t.text or "" for t in el if _local(t.tag) == "label"))
    lab = caption_label(label) or ((kind, label) if re.fullmatch(r"S?\d+", label) else None)
    if lab is None:
        lab = (kind, str(1 + sum(f.kind == kind for f in figs)))
    caption = next((c for c in el if _local(c.tag) == "caption"), None)
    text = _squash(" ".join(_inline(p) for p in caption)) if caption is not None else ""
    sources = [g.get(XLINK_HREF) for g in el.iter() if _local(g.tag) == "graphic" and g.get(XLINK_HREF)]
    figs.append(FigureSpec(kind, label_of(*lab), text, sources, el.get("id")))
    out.append(marker(len(figs) - 1))


def _blocks(el: ET.Element, level: int, out: list[str], figs: list[FigureSpec]) -> None:
    for child in el:
        name = _local(child.tag)
        if name == "sec":
            title = next((t for t in child if _local(t.tag) == "title"), None)
            if title is not None:
                out.append("#" * min(level, 6) + " " + _squash(_inline(title)))
            _blocks(child, level + 1, out, figs)
        elif name in ("title", "label"):
            continue
        elif name == "p":
            out.append(_squash(_inline(child)))
            _blocks_nested(child, level, out, figs)
        elif name == "fig":
            _figure(child, "figure", out, figs)
        elif name == "table-wrap":
            _figure(child, "table", out, figs)
        elif name == "list":
            items = [i for i in child if _local(i.tag) == "list-item"]
            out.append("\n".join("- " + _squash(_inline(i)) for i in items))
        elif name == "disp-formula":
            out.append(_inline_formula(child))
        else:
            _blocks(child, level, out, figs)


def _inline_formula(el: ET.Element) -> str:
    tex = next((t for t in el.iter() if _local(t.tag) == "tex-math"), None)
    return f"$${(tex.text or '').strip()}$$" if tex is not None else _squash(" ".join(el.itertext()))


def _blocks_nested(p: ET.Element, level: int, out: list[str], figs: list[FigureSpec]) -> None:
    """Figures and tables are often nested inside paragraphs."""
    for child in p:
        if _local(child.tag) in ("fig", "table-wrap", "list", "disp-formula"):
            _blocks(_wrap(child), level, out, figs)


def _wrap(el: ET.Element) -> ET.Element:
    w = ET.Element("wrap")
    w.append(el)
    return w


def _find(root: ET.Element, *path: str) -> ET.Element | None:
    el = root
    for name in path:
        el = next((c for c in el.iter() if _local(c.tag) == name), None)
        if el is None:
            return None
    return el


def jats_to_text(xml: bytes) -> tuple[str, dict, list[FigureSpec]]:
    """Markdown body, metadata ({title, authors}) and the figures and tables of a JATS article."""
    root = ET.fromstring(xml)
    if _local(root.tag) == "pmc-articleset":  # NCBI efetch wraps the article
        root = next((c for c in root if _local(c.tag) == "article"), root)
    meta: dict = {}
    if (t := _find(root, "article-meta", "article-title")) is not None:
        meta["title"] = _squash(_inline(t))
    authors = []
    if (am := _find(root, "article-meta")) is not None:
        for contrib in am.iter():
            if _local(contrib.tag) == "contrib" and contrib.get("contrib-type") == "author":
                given = _find(contrib, "given-names")
                surname = _find(contrib, "surname")
                name = " ".join(_squash(x.text or "") for x in (given, surname) if x is not None)
                if name:
                    authors.append(name)
    meta["authors"] = authors

    out: list[str] = []
    figs: list[FigureSpec] = []
    if (abstract := _find(root, "article-meta", "abstract")) is not None:
        out.append("## Abstract")
        _blocks(abstract, 3, out, figs)
    if (body := next((c for c in root if _local(c.tag) == "body"), None)) is not None:
        _blocks(body, 2, out, figs)
    # Figures and tables that publishers put in <floats-group> after the body.
    for group in (c for c in root if _local(c.tag) == "floats-group"):
        _blocks(group, 2, out, figs)
    if (refs := _find(root, "back", "ref-list")) is not None:
        entries = []
        for ref in refs.iter():
            if _local(ref.tag) != "ref":
                continue
            label = _squash("".join(t.text or "" for t in ref if _local(t.tag) == "label"))
            cit = next((c for c in ref if "citation" in _local(c.tag)), None)
            if cit is None:
                continue
            text = _squash(" ".join(cit.itertext()) if _local(cit.tag) == "element-citation" else _inline(cit))
            entries.append(f"{label + '. ' if label else '- '}{text}")
        if entries:
            out.append("## References\n\n" + "\n".join(entries))
    return "\n\n".join(s for s in out if s.strip()), meta, figs
