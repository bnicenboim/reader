import io
import struct
import zipfile
import zlib

import pytest
from test_fetch import fetcher

from paperreader.figures import caption_label, pdf_figures, png_bytes, render_page
from paperreader.html_paper import html_to_markdown
from paperreader.identifiers import parse
from paperreader.library import Library
from paperreader.models import Paper

BODY = ("This paragraph is ordinary body text of the article, long enough to span the column and to run over "
        "several lines, as body text does in every paper that has figures and tables in it.")


@pytest.mark.parametrize("text, label", [
    ("Figure 2. Results of the model", ("figure", "2")),
    ("Fig. 3: Training", ("figure", "3")),
    ("FIGURE 1\nOverall setup", ("figure", "1")),
    ("Figure 1 The Playground Experiment", ("figure", "1")),
    ("Table 1\nDescriptive statistics", ("table", "1")),
    ("TABLE II. Hyperparameters", ("table", "2")),
    ("Supplementary Figure S4. More", ("figure", "S4")),
    ("Figure 2 shows that the model learns", None),
    ("Figures 2 and 3 show", None),
    ("The table below", None),
])
def test_caption_labels(text, label):
    assert caption_label(text) == label


def test_png_encoder():
    rows = bytes([255, 0, 0, 0, 255, 0]) + bytes([0, 0, 255, 255, 255, 255])  # 2x2 RGB
    png = png_bytes(2, 2, rows, 3, 6)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (2, 2)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\x00" + rows[:6] + b"\x00" + rows[6:]


def make_paper_pdf() -> bytes:
    """Three pages with a running head; page 2 has a figure (a drawing) and a table."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for n in range(1, 4):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 40), f"SMITH & JONES {n}", fontsize=9)
        y = 80
        if n == 1:  # the paper the tests ask for (downloads are checked against it)
            page.insert_text((72, 70), "Deep learning. doi:10.1038/nature14539", fontsize=14)
            y = 90
        if n == 2:
            page.draw_rect(pymupdf.Rect(150, 90, 450, 250), color=(0, 0, 1), fill=(0.8, 0.8, 1))
            page.insert_textbox(pymupdf.Rect(72, 260, 540, 290), "Figure 1. A blue box, for testing.", fontsize=9)
            y = 310
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 90), BODY, fontsize=11)
        y += 100
        if n == 2:
            page.insert_textbox(pymupdf.Rect(72, y, 540, y + 20), "Table 1. Numbers.", fontsize=9)
            for i, row in enumerate((("model", "accuracy"), ("ours", "0.9"), ("theirs", "0.8"))):
                page.insert_text((150, y + 40 + 14 * i), row[0], fontsize=9)
                page.insert_text((350, y + 40 + 14 * i), row[1], fontsize=9)
            page.draw_line((140, y + 28), (450, y + 28))
            y += 100
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 90), BODY, fontsize=11)
    return doc.tobytes()


def png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24])


def test_figures_and_tables_are_cropped():
    pytest.importorskip("pymupdf")
    figures = pdf_figures(make_paper_pdf())
    assert [(f.page, f.kind, f.label, f.name) for f in figures] == [
        (2, "figure", "Figure 1", "p2-figure-1.png"), (2, "table", "Table 1", "p2-table-1.png")]
    assert figures[0].caption == "Figure 1. A blue box, for testing."
    points = 72 / 200
    assert 180 < png_size(figures[0].png)[1] * points < 200  # the 160 pt box and its caption, not the running head
    assert 70 < png_size(figures[1].png)[1] * points < 90  # caption and three rows, not the body text below


def test_pages_are_rendered_without_pymupdf(monkeypatch):
    pytest.importorskip("pymupdf")
    data = make_paper_pdf()
    monkeypatch.setattr("paperreader.figures._have_pymupdf", lambda: False)
    figures = pdf_figures(data)
    assert [(f.page, f.kind, f.name) for f in figures] == [(2, "page", "page2.png")]
    assert figures[0].png == render_page(data, 2)
    assert png_size(figures[0].png)[0] == 1275  # 8.5 inches at 150 dpi


LATEXML = """<html><body><div class="ltx_page_main"><article class="ltx_document">
<h1 class="ltx_title ltx_title_document">A Model</h1>
<div class="ltx_authors">A. Author</div>
<div class="ltx_abstract"><h6 class="ltx_title ltx_title_abstract">Abstract</h6>
<p class="ltx_p">We model <math alttext="x^{2}" display="inline"><mi>x</mi></math>.</p></div>
<section class="ltx_section"><h2 class="ltx_title ltx_title_section"><span class="ltx_tag">1 </span>Method</h2>
<div class="ltx_para"><p class="ltx_p">It is <em>new</em><span class="ltx_note ltx_role_footnote">
<sup class="ltx_note_mark">1</sup><span class="ltx_note_outer"><span class="ltx_note_content">
<sup class="ltx_note_mark">1</sup>Really.</span></span></span>.</p>
<table class="ltx_equation ltx_eqn_table"><tr><td><math alttext="E=mc^{2}" display="block"></math></td>
<td><span class="ltx_tag ltx_tag_equation">(1)</span></td></tr></table></div>
<figure class="ltx_figure" id="S1.F1"><img src="2401.00001v1/x1.png" class="ltx_graphics">
<figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure">Figure 1: </span>A plot.</figcaption></figure>
<figure class="ltx_table" id="S1.T1"><figcaption class="ltx_caption">
<span class="ltx_tag ltx_tag_table">Table 1: </span>Scores.</figcaption>
<table class="ltx_tabular"><tr><th>model</th><th colspan="2">score</th></tr>
<tr><td>ours</td><td>1</td><td>2</td></tr></table></figure></section>
<section class="ltx_bibliography"><h2 class="ltx_title">References</h2><ul class="ltx_biblist">
<li class="ltx_bibitem">[1] Smith. <em>Old work</em>. 1999.</li></ul></section>
</article></div><footer>Report an issue</footer></body></html>"""


def test_arxiv_html_to_markdown():
    text, meta, figures = html_to_markdown(LATEXML, "https://arxiv.org/html/2401.00001")
    assert meta == {"title": "A Model"}
    assert text.startswith("## Abstract\n\nWe model $x^{2}$.\n\n## 1 Method\n\nIt is *new* (footnote: Really.).")
    assert "$$E=mc^{2}$$ (1)" in text
    assert "**Figure 1:** A plot.\n\n<!-- figure 0 -->" in text
    table = "| model | score |  |\n| --- | --- | --- |\n| ours | 1 | 2 |"
    assert f"**Table 1:** Scores.\n\n{table}\n\n<!-- figure 1 -->" in text
    assert text.endswith("## References\n\n- [1] Smith. *Old work*. 1999.")
    assert "A. Author" not in text and "Report an issue" not in text
    assert [(f.kind, f.label, f.sources) for f in figures] == [
        ("figure", "Figure 1", ["https://arxiv.org/html/2401.00001v1/x1.png"]), ("table", "Table 1", [])]


def test_arxiv_paper_from_html_with_figures_from_the_pdf(config, client, router):
    pytest.importorskip("pymupdf")
    pdf = make_paper_pdf()
    router.add("https://export.arxiv.org/api/query", content=b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
    router.add("https://arxiv.org/html/2401.00001", headers={"content-type": "text/html"}, content=LATEXML.encode())
    router.add("https://arxiv.org/pdf/2401.00001", headers={"content-type": "application/pdf"}, content=pdf)
    rec = fetcher(config, client).get(parse("arXiv:2401.00001"))
    text = rec.text_path.read_text()
    assert set(rec.files) == {"html", "pdf", "text", "figures"}
    assert f"![Figure 1: A plot.]({rec.key}.figures/p2-figure-1.png)" in text
    assert f"![Table 1: Scores.]({rec.key}.figures/p2-table-1.png)" in text
    assert sorted(p.name for p in rec.files["figures"].iterdir()) == ["p2-figure-1.png", "p2-table-1.png"]
    assert "https://arxiv.org/html/2401.00001v1/x1.png" not in router.urls()  # the crop was enough


JATS_WITH_FIGURE = (b"<?xml version='1.0'?><article xmlns:xlink='http://www.w3.org/1999/xlink'><front><article-meta>"
                    b"<title-group><article-title>Open data</article-title></title-group></article-meta></front>"
                    b"<body><sec><title>Results</title><p>See the figure.</p><fig id='F1'><label>Figure 1</label>"
                    b"<caption><p>Bars.</p></caption><graphic xlink:href='open-g001'/></fig></sec></body></article>")


def test_pmc_figures_from_europe_pmc(config, client, router):
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/search", json_body={"resultList": {"result": [
        {"title": "Open data", "pubYear": "2024", "pmcid": "PMC9"}]}})
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9/fullTextXML",
               headers={"content-type": "application/xml"}, content=JATS_WITH_FIGURE)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("open-g001.gif", b"GIF89a-thumbnail")
        z.writestr("open-g001.jpg", b"\xff\xd8\xff-a-larger-jpeg-image")
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9/supplementaryFiles",
               headers={"content-type": "application/zip"}, content=archive.getvalue())
    rec = fetcher(config, client).get(parse("PMC9"))
    assert f"**Figure 1.** Bars.\n\n![Figure 1: Bars.]({rec.key}.figures/open-g001.jpg)" in rec.text_path.read_text()
    assert (rec.files["figures"] / "open-g001.jpg").read_bytes().startswith(b"\xff\xd8")


def test_author_manuscript_figures_say_where_to_look(config, client, router):
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/search", json_body={"resultList": {"result": [
        {"title": "Open data", "pubYear": "2024", "pmcid": "PMC9"}]}})
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9/fullTextXML",
               headers={"content-type": "application/xml"}, content=JATS_WITH_FIGURE)
    rec = fetcher(config, client).get(parse("PMC9"))  # no zip, no browser
    assert "see https://pmc.ncbi.nlm.nih.gov/articles/PMC9/figure/F1/)" in rec.text_path.read_text()
    assert "figures" not in rec.files


@pytest.mark.parametrize("ext, mime", [("jpg", "image/jpeg"), ("webp", "image/webp")])
def test_author_manuscript_figures_from_the_pmc_page(config, client, router, ext, mime):
    import httpx

    from paperreader.http import Response

    image = f"https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ab12/9/0123456789ab/open-g001.{ext}"

    class FakeBrowser:
        urls = []
        wait = None

        def fetch(self, url, human_wait=0.0, may_ask=None):
            self.urls.append(url)
            return Response(url, 200, httpx.Headers({"content-type": "text/html"}),
                            f'<html><body><img src="{image}"></body></html>'.encode())

    config.browser_enabled = True
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/search", json_body={"resultList": {"result": [
        {"title": "Open data", "pubYear": "2024", "pmcid": "PMC9"}]}})
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9/fullTextXML",
               headers={"content-type": "application/xml"}, content=JATS_WITH_FIGURE)
    router.add(image, headers={"content-type": mime}, content=b"the-image")
    f = fetcher(config, client)
    f._browser = FakeBrowser()
    rec = f.get(parse("PMC9"))  # no zip at Europe PMC, so the article page is opened in the browser
    assert f._browser.urls == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC9/"]
    assert f"![Figure 1: Bars.]({rec.key}.figures/open-g001.{ext})" in rec.text_path.read_text()
    assert (rec.files["figures"] / f"open-g001.{ext}").read_bytes() == b"the-image"


def test_renaming_moves_the_figures(tmp_path):
    lib = Library(tmp_path)
    p = Paper(title="Skype me", authors=["Sarah Roseberry"], year=2014, doi="10.1111/cdev.12166")
    rec = lib.save(p, b"%PDF-1.4", "pdf", "![Figure 1](@@figures@@/p2-figure-1.png)", {},
                   figures={"p2-figure-1.png": b"x"})
    assert "![Figure 1](roseberry2014-skype-me.figures/p2-figure-1.png)" in rec.text_path.read_text()
    rec = lib.rename(rec, "roseberry2014")
    assert (tmp_path / "roseberry2014.figures" / "p2-figure-1.png").exists()
    assert "![Figure 1](roseberry2014.figures/p2-figure-1.png)" in rec.text_path.read_text()
    assert lib.find(Paper(doi="10.1111/cdev.12166")).key == "roseberry2014"
