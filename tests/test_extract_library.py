import pytest
from conftest import make_pdf

from paperreader.browser import load_session_cookies, save_session_cookies
from paperreader.config import Config
from paperreader.extract import is_pdf, jats_to_text, pdf_info, pdf_to_text
from paperreader.html import looks_like_challenge, looks_like_sign_in, parse_page, refuses_headless
from paperreader.library import Library, base_key
from paperreader.models import Paper
from paperreader.publishers import proxied, publisher_pdf_url


def test_pdf_text_has_page_markers():
    pdf = make_pdf(["Hello world", "Second line"])
    assert is_pdf(pdf)
    text = pdf_to_text(pdf)
    assert text.startswith("<!-- page 1 -->")
    assert "Hello world" in text and "Second line" in text
    assert pdf_info(pdf)["pages"] == 1


def test_jats_conversion():
    xml = b"""<?xml version="1.0"?>
    <article xmlns:mml="http://www.w3.org/1998/Math/MathML">
      <front><article-meta>
        <title-group><article-title>A <italic>study</italic></article-title></title-group>
        <contrib-group><contrib contrib-type="author">
          <name><surname>Doe</surname><given-names>Jane</given-names></name>
        </contrib></contrib-group>
        <abstract><p>We find things.</p></abstract>
      </article-meta></front>
      <body><sec><title>Methods</title><p>See <xref ref-type="bibr">1</xref>.</p>
        <fig id="F1"><label>Figure 1</label><caption><p>A plot.</p></caption>
          <graphic xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="nihms-1-f0001.jpg"/></fig>
        <table-wrap><label>Table 1</label><caption><p>Numbers.</p></caption>
          <table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table></table-wrap>
      </sec></body>
      <back><ref-list>
        <ref><label>1</label><mixed-citation>Smith J. Old work. 1999.</mixed-citation></ref>
      </ref-list></back>
    </article>"""
    text, meta, figures = jats_to_text(xml)
    assert meta == {"title": "A study", "authors": ["Jane Doe"]}
    assert "## Abstract\n\nWe find things." in text
    assert "## Methods\n\nSee 1." in text
    assert "**Figure 1.** A plot.\n\n<!-- figure 0 -->" in text
    assert "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n<!-- figure 1 -->" in text
    assert [(f.kind, f.label, f.caption, f.sources, f.ident) for f in figures] == [
        ("figure", "Figure 1", "A plot.", ["nihms-1-f0001.jpg"], "F1"), ("table", "Table 1", "Numbers.", [], None)]
    assert "## References\n\n1. Smith J. Old work. 1999." in text


def test_page_metadata():
    html = """<html><head><title>Paper</title>
      <meta name="citation_title" content="Deep learning">
      <meta name="citation_author" content="LeCun, Yann"><meta name="citation_author" content="Bengio, Yoshua">
      <meta name="citation_doi" content="doi:10.1038/nature14539">
      <meta name="citation_pdf_url" content="/articles/nature14539.pdf"></head><body></body></html>"""
    page = parse_page(html, "https://www.nature.com/articles/nature14539")
    assert page.pdf_url == "https://www.nature.com/articles/nature14539.pdf"
    assert page.doi == "10.1038/nature14539"
    assert page.meta["citation_author"] == ["LeCun, Yann", "Bengio, Yoshua"]


def test_sciencedirect_pdf_link_comes_from_the_page_data():
    html = ('<html><head><title>A paper - ScienceDirect</title></head><body><script type="application/json">'
            '{"article":{"pdfDownload":{"isPdfFullText":false,"urlMetadata":{"queryParams":{"md5":"15be88","pid":'
            '"1-s2.0-S1364661318301657-main.pdf"},"pii":"S1364661318301657","pdfExtension":"/pdfft",'
            '"path":"science/article/pii"}},"other":{}}}</script></body></html>')
    page = parse_page(html, "https://www-sciencedirect-com.proxy.example/science/article/pii/S1364661318301657")
    assert page.pdf_url == ("https://www-sciencedirect-com.proxy.example/science/article/pii/S1364661318301657"
                            "/pdfft?md5=15be88&pid=1-s2.0-S1364661318301657-main.pdf")
    assert parse_page(html.replace('"pdfExtension"', '"other"'), page.url).pdf_url is None


def test_refusals_of_headless_chromium():
    sciencedirect = ("<style>" + "x" * 300_000 + "</style><h1>There was a problem providing the content you "
                     "requested</h1><p>User Agent: Mozilla/5.0 ... HeadlessChrome/153.0.0.0</p>")
    assert refuses_headless(403, sciencedirect)
    assert refuses_headless(418, "")  # IEEE Xplore
    assert not refuses_headless(403, "<h1>Forbidden</h1>")
    assert not refuses_headless(200, sciencedirect)


def test_challenge_detection():
    assert looks_like_challenge("<html></html>", "Just a moment...")
    assert looks_like_challenge("<script>window._cf_chl_opt={}</script>", None)
    assert looks_like_challenge("<html></html>", "Human Verification")  # AWS WAF
    # Cloudflare injects this script into ordinary pages too; it is not a wall by itself.
    script = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    assert not looks_like_challenge(script, "Deep learning")


def test_noscript_refresh_is_ignored():
    # Microsoft's sign-in page, as a browser with JavaScript renders it.
    html = """<html><head><title>Sign in to your account</title>
      <noscript><meta http-equiv="Refresh" content="0; URL=https://login.microsoftonline.com/jsdisabled" /></noscript>
      </head></html>"""
    page = parse_page(html, "https://login.microsoftonline.com/tenant/saml2")
    assert page.refresh_url is None
    assert page.title == "Sign in to your account"


def test_sign_in_detection():
    proxy = "tilburguniversity.idm.oclc.org"
    assert looks_like_sign_in("https://login.microsoftonline.com/7a55/saml2?SAMLRequest=x", proxy)
    assert looks_like_sign_in("https://engine.surfconext.nl/authentication/idp/single-sign-on", proxy)
    assert looks_like_sign_in("https://adfs.campus.uvt.nl/adfs/ls/?SAMLRequest=x", proxy)
    assert looks_like_sign_in(f"https://{proxy}/login?url=https://doi.org/10.1121/x", proxy)
    # Proxied content and publisher pages are not sign-in pages.
    assert not looks_like_sign_in("https://pubs-aip-org.tilburguniversity.idm.oclc.org/jasa/article/x", proxy)
    assert not looks_like_sign_in("https://doi.org/10.1121/10.0040820", proxy)
    assert not looks_like_sign_in("https://www.example.edu/login", None)


def test_session_cookies_survive_and_expire(config, monkeypatch):
    cookies = [
        {"name": "ezproxy", "value": "s1", "domain": ".idm.oclc.org", "path": "/", "expires": -1},
        {"name": "persistent", "value": "p", "domain": ".example.org", "path": "/", "expires": 2e9},
    ]
    save_session_cookies(config, cookies)
    path = config.state_dir / "session-cookies.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert [c["name"] for c in load_session_cookies(config)] == ["ezproxy"]  # Chromium keeps the persistent one
    monkeypatch.setattr("paperreader.browser.time.time", lambda: path.stat().st_mtime + 2 * 24 * 3600)
    assert load_session_cookies(config) == []


def test_keys_are_readable_ascii():
    p = Paper(title="Learning to Understand Child-directed Speech", authors=["Grzegorz Chrupała"], year=2020)
    assert base_key(p) == "chrupala2020-learning-understand-child-directed"
    assert base_key(Paper(title="On X", authors=["Łukasiewicz, Jan"], year=1920)) == "lukasiewicz1920-x"
    assert base_key(Paper(doi="10.1/abc")) == "doi-10-1-abc"


def test_library_save_and_find(tmp_path):
    lib = Library(tmp_path)
    p = Paper(title="Deep learning", authors=["Yann LeCun"], year=2015, doi="10.1038/nature14539")
    rec = lib.save(p, b"%PDF-1.4 fake", "pdf", "body", {"source": "test", "url": "u"})
    assert rec.files["pdf"].exists() and rec.text_path.read_text().endswith("body\n")
    assert lib.find(Paper(doi="10.1038/NATURE14539")).key == rec.key
    assert lib.find(Paper(arxiv="1111.11111")) is None
    # Same paper again keeps its key; a different paper with the same name gets a suffix.
    assert lib.save(p, b"%PDF-1.4", "pdf", "b", {}).key == rec.key
    other = Paper(title="Deep learning", authors=["Yann LeCun"], year=2015, doi="10.1/other")
    assert lib.save(other, b"%PDF-1.4", "pdf", "b", {}).key == rec.key + "-2"


def test_publisher_urls_and_proxy():
    assert publisher_pdf_url("10.1038/nature14539") == "https://www.nature.com/articles/nature14539.pdf"
    assert publisher_pdf_url("10.9999/unknown") is None
    cfg = Config(proxy="https://login.ezproxy.example.edu/login?url={url}")
    assert proxied(cfg, "https://doi.org/10.1/x") == "https://login.ezproxy.example.edu/login?url=https://doi.org/10.1/x"
    cfg = Config(proxy="https://go.openathens.net/redirector/example.edu?url={url_encoded}")
    assert proxied(cfg, "https://doi.org/10.1/x").endswith("?url=https%3A%2F%2Fdoi.org%2F10.1%2Fx")


def test_download_history_is_forgotten(tmp_path):
    import sqlite3

    from paperreader.browser import _forget_downloads

    history = tmp_path / "Default" / "History"
    history.parent.mkdir()
    db = sqlite3.connect(history)
    for table in ("downloads", "downloads_url_chains", "downloads_slices"):
        db.execute(f"CREATE TABLE {table} (id INTEGER)")
        db.execute(f"INSERT INTO {table} VALUES (1)")
    db.execute("CREATE TABLE urls (id INTEGER)")
    db.execute("INSERT INTO urls VALUES (1)")
    db.commit()
    db.close()
    _forget_downloads(tmp_path)
    db = sqlite3.connect(history)
    assert db.execute("SELECT COUNT(*) FROM downloads").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM urls").fetchone() == (1,)  # browsing history is kept
    _forget_downloads(tmp_path / "missing")  # no profile yet: nothing to do


def test_pdf_text_cleaning():
    from paperreader.extract import _clean, _is_hal_cover

    raw = ("it showed effects [F(2, 42) = 17.37, p = 3.2e\r\n−6\r\n, η2 = .453] and more\r\n"
           "Downloaded from http://royalsocietypublishing.org/rstb/article-pdf/doi/10.1098/x.pdf\r\nby guest\r\n"
           "a rate of 2.5 × 10\r\n−3\r\nper second\r\nAuthorized licensed use limited to: Tilburg University. "
           "Downloaded on October 10,2026 at 11:48:02 UTC from IEEE Xplore. Restrictions apply. ")
    assert _clean(raw) == "it showed effects [F(2, 42) = 17.37, p = 3.2e−6, η2 = .453] and more\n" \
                          "a rate of 2.5 × 10^−3\nper second"
    assert _is_hal_cover("HAL Id: hal-03080098\nhttps://hal.science/hal-03080098v1\nSubmitted on 17 Dec 2020")
    assert not _is_hal_cover("Does infant-directed speech help phonetic learning?")


PDF_TEXT = """# A paper

Authors: A. Author

---

<!-- page 1 -->

1 Tokyo Institute of Technology
Abstract
We study things.
1. Introduction
3 participants were excluded because of noise.
2. Method
2.1. Participants
Twelve children.
<!-- page 2 -->

3. Results
It worked.
References
Smith, J. (2020). A study. Journal 1, 1-2.
<!-- page 3 -->

Appendix A. Extra results
More numbers.
"""


def test_sections_of_pdf_text():
    from paperreader import sections

    titles = [(h.level, h.title, h.page) for h in sections.headings(PDF_TEXT)]
    assert titles == [(1, "Abstract", 1), (1, "1. Introduction", 1), (1, "2. Method", 1), (2, "2.1. Participants", 1),
                      (1, "3. Results", 2), (1, "References", 2), (1, "Appendix A. Extra results", 3)]
    text, missing = sections.extract(PDF_TEXT, ["method", "Discussion"])
    assert text == "<!-- page 1 -->\n\n2. Method\n2.1. Participants\nTwelve children.\n<!-- page 2 -->"
    assert missing == ["Discussion"]
    short = sections.without_references(PDF_TEXT)
    assert "Smith, J." not in short and "(references omitted)" in short
    assert "<!-- page 3 -->\n\nAppendix A. Extra results" in short


def test_sections_of_jats_text():
    from paperreader import sections

    text = "# T\n\n---\n\n## Abstract\n\nShort.\n\n## Method\n\n### Participants\n\nKids.\n\n## References\n\n1. X."
    assert [(h.level, h.name) for h in sections.headings(text)] == [
        (1, "abstract"), (1, "method"), (2, "participants"), (1, "references")]
    assert sections.extract(text, ["Participants"])[0] == "### Participants\n\nKids."
    short = sections.without_references(text)
    assert short.endswith("## Method\n\n### Participants\n\nKids.\n\n(references omitted)\n")


def test_library_keys(tmp_path):
    lib = Library(tmp_path)
    p = Paper(title="Skype me", authors=["Sarah Roseberry"], year=2013, doi="10.1111/cdev.12166")
    rec = lib.save(p, b"%PDF-1.4 fake", "pdf", "body", {"source": "test"})
    assert rec.key == "roseberry2013-skype-me"
    rec = lib.rename(rec, "roseberry2014")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["roseberry2014.json", "roseberry2014.md", "roseberry2014.pdf"]
    assert lib.by_key("roseberry2014").paper.doi == "10.1111/cdev.12166"
    other = Paper(title="Other", authors=["B"], year=2020, doi="10.1/other")
    with pytest.raises(ValueError, match="taken"):
        lib.save(other, b"%PDF-1.4", "pdf", "x", {}, key="roseberry2014")
    with pytest.raises(ValueError, match="not a usable key"):
        lib.rename(rec, "../escape")
    # Saving the same paper under a new key moves it.
    rec = lib.save(p, b"%PDF-1.4 fake", "pdf", "body", {"source": "test"}, key="roseberry2014b")
    assert lib.by_key("roseberry2014") is None and rec.files["pdf"].exists()


def test_pending_list(tmp_path):
    from paperreader.pending import Pending

    pending = Pending(tmp_path / "pending.json")
    p = Paper(title="Paywalled", doi="10.1/x")
    pending.add(p, "doi:10.1/x", "no open copy", "https://doi.org/10.1/x", [], key="smith2020")
    pending.add(p, "doi:10.1/x", "sign-in needed", None, [])  # replaces the earlier entry, keeping its key (#8)
    assert [(e["id"], e["key"], e["reason"]) for e in pending.all()] == [("doi:10.1/x", "smith2020", "sign-in needed")]
    # A link to the paper keeps the DOI as the id to retry; a key of its own replaces the old one.
    pending.add(p, "url:https://example.org/x.pdf", "HTTP 404", None, [], key="smith2020a")
    assert [(e["id"], e["key"]) for e in pending.all()] == [("doi:10.1/x", "smith2020a")]
    pending.remove(Paper(doi="10.1/x", pmid="1"))
    assert pending.all() == []


def test_bibtex_entries():
    from paperreader.bibtex import bibtex

    p = Paper(title="Skype Me! Socially Contingent Interactions Help Toddlers Learn Language",
              authors=["Sarah Roseberry", "Kathy Hirsh-Pasek"], year=2014, venue="Child Development",
              doi="10.1111/cdev.12166", pub_type="journal-article", volume="85", issue="3", pages="956-970")
    assert bibtex(p) == """@article{roseberry2014skype,
  author = {Sarah Roseberry and Kathy Hirsh-Pasek},
  title = {Skype Me! Socially Contingent Interactions Help Toddlers Learn Language},
  journal = {Child Development},
  year = {2014},
  volume = {85},
  number = {3},
  pages = {956--970},
  doi = {10.1111/cdev.12166}
}"""
    preprint = Paper(title="Attention & you", authors=["Vaswani, A."], year=2017, arxiv="1706.03762", venue="arXiv")
    assert bibtex(preprint, key="vaswani2017") == """@misc{vaswani2017,
  author = {Vaswani, A.},
  title = {Attention \\& you},
  year = {2017},
  eprint = {1706.03762},
  archiveprefix = {arXiv}
}"""
