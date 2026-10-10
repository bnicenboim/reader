from conftest import make_pdf

from paperreader.browser import load_session_cookies, save_session_cookies
from paperreader.config import Config
from paperreader.extract import is_pdf, jats_to_text, pdf_info, pdf_to_text
from paperreader.html import looks_like_challenge, looks_like_sign_in, parse_page
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
        <fig><label>Figure 1</label><caption><p>A plot.</p></caption></fig>
        <table-wrap><label>Table 1</label><caption><p>Numbers.</p></caption>
          <table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table></table-wrap>
      </sec></body>
      <back><ref-list>
        <ref><label>1</label><mixed-citation>Smith J. Old work. 1999.</mixed-citation></ref>
      </ref-list></back>
    </article>"""
    text, meta = jats_to_text(xml)
    assert meta == {"title": "A study", "authors": ["Jane Doe"]}
    assert "## Abstract\n\nWe find things." in text
    assert "## Methods\n\nSee 1." in text
    assert "**Figure 1.** A plot." in text
    assert "| a | b |\n| --- | --- |\n| 1 | 2 |" in text
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


def test_challenge_detection():
    assert looks_like_challenge("<html></html>", "Just a moment...")
    assert looks_like_challenge("<script>window._cf_chl_opt={}</script>", None)
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
