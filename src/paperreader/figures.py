"""Images of a paper's figures and tables, so an agent that can see images can look at them.

From PDFs, each figure or table is cropped around its caption with PyMuPDF (the `figures`
extra): starting at the caption, the region grows over graphics, labels and table cells until
it meets body text or another caption. Figures grow upwards first (captions sit below them),
tables downwards. Without PyMuPDF, or when no region is found, the whole page is rendered.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass

CROP_DPI = 200
PAGE_DPI = 150

CAPTION_RE = re.compile(
    r"\s*(?P<kind>Supplementary\s+(?:Figure|Table)|Extended\s+Data\s+(?:Fig\.?|Figure|Table)|Fig\.?|Figure|FIGURE|FIG\.?"
    r"|Table|TABLE)\s*(?P<num>S?\d+[A-Za-z]?|[IVXL]+)(?=[\s.:|—–-]|$)[ \t\xa0]*(?P<punct>[.:|—–-])?"
)
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50}


@dataclass
class Figure:
    page: int | None  # PDF page, 1-based
    kind: str  # figure | table | page
    label: str | None  # "Figure 3", "Table 2"; None for a rendered page
    caption: str
    png: bytes
    name: str  # file name in the paper's figures directory


@dataclass
class FigureSpec:
    """A figure or table found in JATS or HTML text, waiting for its image."""

    kind: str  # figure | table
    label: str | None  # "Figure 3"
    caption: str
    sources: list[str]  # image URLs (HTML) or graphic file names (JATS)
    ident: str | None = None  # the element's id, e.g. "F1"


# Where a figure goes in JATS or HTML text, until its image is known.
MARKER_RE = re.compile(r"<!-- figure (\d+) -->")
# Stands for the paper's figures directory until the paper has its key.
FIGDIR = "@@figures@@"


def marker(i: int) -> str:
    return f"<!-- figure {i} -->"


def image_line(alt: str, name: str) -> str:
    """A Markdown image, with the caption (shortened) as its text."""
    alt = " ".join(alt.split()).replace("[", "(").replace("]", ")")
    if len(alt) > 200:
        alt = alt[:197] + "..."
    return f"![{alt}]({FIGDIR}/{name})"


def figure_line(f: Figure) -> str:
    if f.kind == "page":
        return image_line(f"Page {f.page}" + (f" ({f.caption})" if f.caption else ""), f.name)
    return image_line(f.caption or f.label or f.name, f.name)


def with_page_figures(pages: list[str], figures: list[Figure]) -> list[str]:
    """Each page's text followed by its figures and tables."""
    out = list(pages)
    for f in figures:
        if f.page and 0 < f.page <= len(out):
            out[f.page - 1] += "\n\n" + figure_line(f)
    return out


def _roman(s: str) -> int:
    total = 0
    for a, b in zip(s, s[1:] + " ", strict=True):
        v = ROMAN[a]
        total += -v if b != " " and ROMAN.get(b, 0) > v else v
    return total


def caption_label(text: str) -> tuple[str, str] | None:
    """("figure", "3") for "Fig. 3. Results ...", ("table", "2") for "TABLE II", else None.

    A cross-reference in the body ("Figure 2 shows ...") is not a caption: a caption's number is
    followed by punctuation, by a capitalised title ("Figure 1 The Playground Experiment"), or by
    nothing on its line ("FIGURE 1", then the title on the next line).
    """
    m = CAPTION_RE.match(text)
    if not m:
        return None
    rest = text[m.end():]
    alone = not rest.strip() or rest.lstrip(" \t\r\xa0").startswith("\n")
    titled = rest.strip()[:1].isupper()
    kind = "table" if "table" in m.group("kind").lower() else "figure"
    if not (m.group("punct") or alone or titled):
        return None
    num = m.group("num")
    if re.fullmatch(r"[IVXL]+", num):
        if kind != "table":
            return None
        num = str(_roman(num))
    return kind, num.upper() if num.startswith("s") else num


def label_of(kind: str, num: str) -> str:
    return f"{kind.capitalize()} {num}"


# --- PNG without Pillow -----------------------------------------------------------------------


def png_bytes(width: int, height: int, rows: bytes, channels: int, stride: int) -> bytes:
    """Encode raw 8-bit RGB(A) rows as a PNG."""
    color = {3: 2, 4: 6}[channels]
    raw = b"".join(b"\x00" + rows[y * stride:y * stride + width * channels] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def render_page(data: bytes, page_number: int, dpi: int = PAGE_DPI) -> bytes:
    """A whole page as PNG, with pypdfium2 (always installed)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        page = doc[page_number - 1]
        bitmap = page.render(scale=dpi / 72, rev_byteorder=True)
        try:
            return png_bytes(bitmap.width, bitmap.height, bytes(bitmap.buffer), bitmap.n_channels, bitmap.stride)
        finally:
            bitmap.close()
            page.close()
    finally:
        doc.close()


# --- cropping with PyMuPDF --------------------------------------------------------------------


@dataclass
class _Item:
    x0: float
    y0: float
    x1: float
    y1: float
    kind: str  # text | image | drawing
    text: str = ""
    lines: int = 0
    size: float = 0.0  # dominant font size

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def _body_size(doc) -> float:
    """The most common font size, weighted by characters: the size of body text."""
    counts: dict[float, int] = {}
    for page in doc.pages(0, min(len(doc), 6)):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    size = round(span["size"], 1)
                    counts[size] = counts.get(size, 0) + len(span["text"].strip())
    return max(counts, key=counts.get) if counts else 10.0


def _norm(text: str) -> str:
    return re.sub(r"[\d\s]+", "", text)


def _running_heads(doc) -> set[str]:
    """Text repeated at the top or bottom of several pages (running heads, journal footers)."""
    counts: dict[str, int] = {}
    for page in doc:
        H = page.rect.height
        for _x0, y0, _x1, y1, text, *_ in page.get_text("blocks"):
            if (y1 < 0.12 * H or y0 > 0.9 * H) and (key := _norm(text)):
                counts[key] = counts.get(key, 0) + 1
    return {k for k, n in counts.items() if n >= 3}


def _items(page, heads: set[str]) -> list[_Item]:
    H = page.rect.height
    items = []
    for block in page.get_text("dict")["blocks"]:
        x0, y0, x1, y1 = block["bbox"]
        if block["type"] == 1:
            items.append(_Item(x0, y0, x1, y1, "image"))
            continue
        lines = [ln for ln in block.get("lines", []) if abs(ln["dir"][0]) > 0.99]  # skip rotated text
        if not lines:
            continue
        sizes: dict[float, int] = {}
        for ln in lines:
            for span in ln["spans"]:
                sizes[round(span["size"], 1)] = sizes.get(round(span["size"], 1), 0) + len(span["text"])
        text = "\n".join("".join(s["text"] for s in ln["spans"]) for ln in lines).strip()
        in_margin = y1 < 0.12 * H or y0 > 0.9 * H
        if in_margin and (not _norm(text) or _norm(text) in heads):
            continue  # page number or running head
        lx0 = min(ln["bbox"][0] for ln in lines)
        lx1 = max(ln["bbox"][2] for ln in lines)
        items.append(_Item(lx0, y0, lx1, y1, "text", text, len(lines), max(sizes, key=sizes.get)))
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        items.append(_Item(x0, y0, x1, y1, "image"))
    for r in page.cluster_drawings():
        items.append(_Item(r.x0, r.y0, r.x1, r.y1, "drawing"))
    W = page.rect.width
    # Page-sized rectangles are backgrounds; slivers at the very top or bottom are rules and decorations.
    return [it for it in items if it.width < 0.98 * W and it.y1 > 0.04 * H and it.y0 < 0.96 * H]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _is_body(it: _Item, body: float, span: float, captions: set[int]) -> bool:
    """A paragraph of body text: body font size, long lines, most of the column wide."""
    if it.kind != "text" or id(it) in captions:
        return False
    long_lines = len(it.text) / max(it.lines, 1) >= 30
    return abs(it.size - body) <= 0.6 and long_lines and (it.lines >= 2 or len(it.text) >= 80) \
        and it.width >= 0.55 * span


def _grow(cap: _Item, near: list[_Item], upwards: bool, stop) -> list[_Item]:
    """The items next to a caption, nearest first, up to body text, another caption or a wide gap."""
    if upwards:
        side = sorted((it for it in near if it.y0 < cap.y0 - 1 and it.y1 <= cap.y1), key=lambda it: -it.y1)
    else:
        side = sorted((it for it in near if it.y1 > cap.y1 + 1 and it.y0 >= cap.y0), key=lambda it: it.y0)
    region: list[_Item] = []
    edge = cap.y0 if upwards else cap.y1
    for it in side:
        if stop(it):
            break
        if region and (edge - it.y1 if upwards else it.y0 - edge) > 40:
            break  # a wide gap: what comes next belongs to something else
        region.append(it)
        edge = min(edge, it.y0) if upwards else max(edge, it.y1)
    return region


def _substantial(region: list[_Item], kind: str) -> bool:
    if not region:
        return False
    height = max(it.y1 for it in region) - min(it.y0 for it in region)
    return height >= 25 and (kind == "table" or any(it.kind != "text" for it in region))


def _page_crops(page, body: float, heads: set[str]) -> tuple[list[tuple], list[str]]:
    """(kind, num, caption, rect) for each caption on the page, and the captions no region was found for."""
    import pymupdf

    items = _items(page, heads)
    captions = [(it, *lab) for it in items if it.kind == "text" and (lab := caption_label(it.text))]
    if not captions:
        return [], []
    texts = [it for it in items if it.kind == "text"]
    x_min = min(it.x0 for it in texts)
    x_max = max(it.x1 for it in texts)
    mid = (x_min + x_max) / 2
    narrow = [it for it in texts if it.lines >= 2 and it.width < 0.55 * (x_max - x_min)]
    two_columns = any(it.x1 <= mid + 5 for it in narrow) and any(it.x0 >= mid - 5 for it in narrow)
    caption_ids = {id(c[0]) for c in captions}

    out, missing = [], []
    for cap, kind, num in captions:
        if not two_columns or cap.width > 0.6 * (x_max - x_min):
            sx0, sx1 = x_min, x_max
        elif (cap.x0 + cap.x1) / 2 < mid:
            sx0, sx1 = x_min, mid
        else:
            sx0, sx1 = mid, x_max
        near = [it for it in items if it is not cap and _overlap(it.x0, it.x1, sx0, sx1) >= 0.5 * max(it.width, 1)]

        def stop(it: _Item, span: float = sx1 - sx0) -> bool:
            return id(it) in caption_ids or _is_body(it, body, span, caption_ids)

        # Captions go below figures and above tables, mostly.
        region = _grow(cap, near, kind == "figure", stop)
        if not _substantial(region, kind):
            region = _grow(cap, near, kind != "figure", stop)
        if not _substantial(region, kind):
            missing.append(" ".join(cap.text.split()))
            continue
        rect = pymupdf.Rect(cap.x0, cap.y0, cap.x1, cap.y1)
        for it in region:
            rect |= pymupdf.Rect(it.x0, it.y0, it.x1, it.y1)
        rect = rect + (-4, -4, 4, 4)
        if (sx0, sx1) != (x_min, x_max):  # stay in the caption's column
            rect.x0, rect.x1 = max(rect.x0, sx0 - 4), min(rect.x1, sx1 + 4)
        rect &= page.rect
        out.append((kind, num, " ".join(cap.text.split()), rect))
    return out, missing


def _have_pymupdf() -> bool:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return False
    return True


def _page_captions(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if caption_label(line.strip())]


def pdf_figures(data: bytes, page_texts: list[str] | None = None) -> list[Figure]:
    """Images of the figures and tables in a PDF, in page order."""
    if page_texts is None:
        from .extract import pdf_pages

        page_texts = pdf_pages(data)
    if not _have_pymupdf():
        return _page_renders(data, page_texts)
    crops = _crop_figures(data)
    # A page whose text has a caption the cropper did not recognise is shown whole.
    covered = {f.page for f in crops}
    missed = [Figure(i, "page", None, "; ".join(c[:80] for c in captions), b"", f"page{i}.png")
              for i, text in enumerate(page_texts, 1) if i not in covered and (captions := _page_captions(text))]
    for f in missed:
        f.png = render_page(data, f.page)
    return sorted(crops + missed, key=lambda f: f.page or 0)


def _crop_figures(data: bytes) -> list[Figure]:
    import pymupdf

    out: list[Figure] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        body = _body_size(doc)
        heads = _running_heads(doc)
        seen: set[str] = set()
        for page in doc:
            crops, missing = _page_crops(page, body, heads)
            for kind, num, caption, rect in crops:
                label = label_of(kind, num)
                name = f"p{page.number + 1}-{kind}-{num}.png".lower()
                if name in seen:  # a table continued on the same page, say
                    name = name.replace(".png", f"-{len(seen)}.png")
                seen.add(name)
                pix = page.get_pixmap(clip=rect, dpi=CROP_DPI)
                out.append(Figure(page.number + 1, kind, label, caption, pix.tobytes("png"), name))
            if missing:  # show the whole page rather than nothing
                pix = page.get_pixmap(dpi=PAGE_DPI)
                out.append(Figure(page.number + 1, "page", None, "; ".join(c[:80] for c in missing),
                                  pix.tobytes("png"), f"page{page.number + 1}.png"))
    return out


def _page_renders(data: bytes, page_texts: list[str]) -> list[Figure]:
    """Without PyMuPDF: every page with a figure or table caption, rendered whole."""
    out = []
    for i, text in enumerate(page_texts, 1):
        if captions := _page_captions(text):
            out.append(Figure(i, "page", None, "; ".join(c[:80] for c in captions), render_page(data, i),
                              f"page{i}.png"))
    return out
