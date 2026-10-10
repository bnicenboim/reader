"""arXiv's HTML version of a paper (LaTeXML, on arxiv.org/html and ar5iv) as Markdown.

Better than PDF text for what LaTeX knows: real headings, maths as LaTeX (`$...$`), and tables
as Markdown tables. Figures and tables leave a marker where they go; their images come from the
PDF (arXiv's HTML lacks some of them, e.g. figures that were PDFs).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .figures import FigureSpec, caption_label, label_of, marker

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "nav", "header", "footer", "button", "svg"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 5}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: Node | None):
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        return next((n for n in self.iter() if n is not self and (tag is None or n.tag == tag)
                     and (cls is None or cls in n.classes)), None)


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: v or "" for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


def _squash(s: str) -> str:
    return " ".join(s.split())


class _Converter:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.out: list[str] = []
        self.figures: list[FigureSpec] = []

    # -- inline ---------------------------------------------------------------------------

    def inline(self, node) -> str:
        if isinstance(node, str):
            return node
        tag, cls = node.tag, node.classes
        if tag in SKIP or tag in ("img", "object") or "ltx_note_mark" in cls:
            return ""
        if tag == "math":
            tex = node.attrs.get("alttext", "").strip()
            return f"$${tex}$$" if node.attrs.get("display") == "block" else f"${tex}$"
        if "ltx_note" in cls and "ltx_role_footnote" in cls:
            content = node.find(cls="ltx_note_content")
            return f" (footnote: {_squash(self.inline(content))})" if content else ""
        if tag == "br":
            return " "
        text = "".join(self.inline(c) for c in node.children)
        if not text.strip():
            return text
        if tag in ("em", "i") or "ltx_font_italic" in cls:
            return f"*{text.strip()}*"
        if tag in ("b", "strong") or "ltx_font_bold" in cls:
            return f"**{text.strip()}**"
        if tag == "sup":
            return f"^{text.strip()}"
        if tag == "sub":
            return f"_{text.strip()}"
        return text

    # -- blocks ---------------------------------------------------------------------------

    def para(self, text: str) -> None:
        if text := _squash(text):
            self.out.append(text)

    def blocks(self, node: Node, level: int = 2) -> None:
        for child in node.children:
            if isinstance(child, str):
                self.para(child)
            else:
                self.block(child, level)

    def block(self, node: Node, level: int) -> None:
        tag, cls = node.tag, node.classes
        if tag in SKIP or cls & {"ltx_authors", "ltx_dates", "ltx_pagination", "ltx_page_footer",
                                 "ltx_title_document", "ltx_classification", "ltx_role_affiliation"}:
            return
        if tag in HEADINGS:
            self.out.append("#" * max(2, HEADINGS[tag]) + " " + _squash(self.inline(node)))
        elif "ltx_abstract" in cls:
            self.out.append("## Abstract")
            for child in node.children:  # without its own "Abstract" title
                if not (isinstance(child, Node) and "ltx_title_abstract" in child.classes):
                    self.block(child, level) if isinstance(child, Node) else self.para(child)
        elif "ltx_bibliography" in cls:
            self.out.append("## References")
            for item in (n for n in node.iter() if "ltx_bibitem" in n.classes):
                self.out.append("- " + _squash(self.inline(item)))
        elif tag == "figure" or "ltx_float" in cls:
            self.figure(node)
        elif "ltx_equation" in cls or "ltx_equationgroup" in cls:
            self.equation(node)
        elif tag in ("ul", "ol"):
            items = [c for c in node.children if isinstance(c, Node) and c.tag == "li"]
            self.out.append("\n".join("- " + _squash(self.inline(i)) for i in items))
        elif tag == "table" and "ltx_tabular" in cls:
            self.out.append(self.table(node))
        elif tag == "p" or "ltx_p" in cls:
            self.para(self.inline(node))
        elif "ltx_listing" in cls or tag == "pre":
            self.out.append("```\n" + self.inline(node).strip("\n") + "\n```")
        else:
            self.blocks(node, level)

    def equation(self, node: Node) -> None:
        tags = [_squash(self.inline(t)) for t in node.iter() if "ltx_tag_equation" in t.classes]
        maths = [m.attrs.get("alttext", "").strip() for m in node.iter() if m.tag == "math"]
        if maths:
            self.out.append("$$" + " \\\\ ".join(maths) + "$$" + (" " + " ".join(tags) if tags else ""))

    def table(self, node: Node) -> str:
        rows = []
        for tr in (n for n in node.iter() if n.tag == "tr"):
            cells = []
            for cell in (c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")):
                cells.append(_squash(self.inline(cell)).replace("|", "\\|"))
                cells += [""] * (int(cell.attrs.get("colspan", "1") or 1) - 1)
            if any(cells):
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
        lines.insert(1, "|" + " --- |" * width)
        return "\n".join(lines)

    def figure(self, node: Node) -> None:
        caption_node = next((c for c in node.children if isinstance(c, Node) and c.tag == "figcaption"), None)
        caption = _squash(self.inline(caption_node)) if caption_node else ""
        lab = caption_label(caption)
        is_table = "ltx_table" in node.classes or (lab is not None and lab[0] == "table")
        kind = "table" if is_table else "figure"
        if caption:
            tag = caption_node.find(cls="ltx_tag")
            tag_text = _squash(self.inline(tag)) if tag else ""
            self.out.append(f"**{tag_text.rstrip()}** {caption[len(tag_text):].strip()}" if tag_text else caption)
        for t in (n for n in node.iter() if n.tag == "table" and "ltx_tabular" in n.classes):
            if not any(p.tag == "table" and "ltx_tabular" in p.classes for p in _ancestors(t, node)):
                self.out.append(self.table(t))
        images = [urljoin(self.base_url, n.attrs["src"]) for n in node.iter()
                  if n.tag == "img" and "ltx_graphics" in n.classes and n.attrs.get("src")]
        label = label_of(*lab) if lab else None
        self.figures.append(FigureSpec(kind, label, caption, images))
        self.out.append(marker(len(self.figures) - 1))


def _ancestors(node: Node, stop: Node):
    p = node.parent
    while p is not None and p is not stop:
        yield p
        p = p.parent


def is_latexml(html: bytes) -> bool:
    head = html[:5000].lower()
    return b"ltx_document" in html[:200_000] or b"latexml" in head


def html_to_markdown(html: str, base_url: str) -> tuple[str, dict, list[FigureSpec]]:
    """Markdown body, metadata ({title}) and the figures and tables, in order."""
    root = parse(html)
    doc = root.find("article", "ltx_document") or root.find(cls="ltx_document") or root
    conv = _Converter(base_url)
    meta = {}
    if (title := doc.find(cls="ltx_title_document")) is not None:
        meta["title"] = _squash(conv.inline(title))
    conv.blocks(doc)
    text = "\n\n".join(s for s in conv.out if s.strip())
    return re.sub(r"\n{3,}", "\n\n", text), meta, conv.figures
