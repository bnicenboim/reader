import pytest
from conftest import make_pdf

from paperreader.fetch import Fetcher, FetchFailed
from paperreader.identifiers import parse
from paperreader.library import Library

PDF = make_pdf(["Deep learning", "Yann LeCun"])
OPENALEX = "https://api.openalex.org/works/doi:10.1038/nature14539"


def openalex_work(**kw):
    w = {
        "display_name": "Deep learning",
        "publication_year": 2015,
        "doi": "https://doi.org/10.1038/nature14539",
        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/26017442"},
        "authorships": [{"author": {"display_name": "Yann LeCun"}}, {"author": {"display_name": "Yoshua Bengio"}}],
        "primary_location": {"source": {"display_name": "Nature"}},
        "best_oa_location": None,
        "locations": [],
    }
    w.update(kw)
    return w


def fetcher(config, client, **kw):
    return Fetcher(config, client, Library(config.library_dir), log=lambda s: None, **kw)


def test_open_access_copy_found_through_openalex(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "https://hal.science/hal-1", "version": "submittedVersion"},
    ]))
    router.add("https://hal.science/hal-1", headers={"content-type": "text/html"},
               content=b'<html><head><meta name="citation_pdf_url" content="/hal-1/document"></head></html>')
    router.add("https://hal.science/hal-1/document", headers={"content-type": "application/pdf"}, content=PDF)

    rec = fetcher(config, client).get(parse("10.1038/nature14539"))

    assert rec.key == "lecun2015-deep-learning"
    assert rec.files["pdf"].read_bytes() == PDF
    text = rec.text_path.read_text()
    assert text.startswith("# Deep learning")
    assert "Yann LeCun" in text and "<!-- page 1 -->" in text
    assert rec.source["url"] == "https://hal.science/hal-1/document"


def test_arxiv_location_becomes_arxiv_candidate(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "http://arxiv.org/abs/1234.56789", "pdf_url": "https://arxiv.org/pdf/1234.56789"},
    ]))
    router.add("https://arxiv.org/pdf/1234.56789", headers={"content-type": "application/pdf"}, content=PDF)
    rec = fetcher(config, client).get(parse("10.1038/nature14539"))
    assert rec.paper.arxiv == "1234.56789"
    assert rec.source["source"] == "arxiv"


def test_library_hit_needs_no_network(config, client, router):
    router.add("https://arxiv.org/pdf/1234.56789", headers={"content-type": "application/pdf"}, content=PDF)
    f = fetcher(config, client)
    f.get(parse("arXiv:1234.56789"))
    n = len(router.requests)
    rec = f.get(parse("https://arxiv.org/abs/1234.56789v2"))
    assert len(router.requests) == n
    assert rec.paper.arxiv == "1234.56789"


def test_library_matches_on_any_identifier(config, client, router):
    router.add("https://arxiv.org/pdf/1234.56789", headers={"content-type": "application/pdf"}, content=PDF)
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "http://arxiv.org/abs/1234.56789"},
    ]))
    f = fetcher(config, client)
    f.get(parse("arXiv:1234.56789"))
    rec = f.get(parse("10.1038/nature14539"))  # OpenAlex reveals the arXiv id -> already have it
    assert router.urls().count("https://arxiv.org/pdf/1234.56789") == 1
    assert len(Library(config.library_dir).all()) == 1
    assert rec.paper.arxiv == "1234.56789"


def test_paywalled_paper_fails_with_hints(config, client, router):
    router.add(OPENALEX, json_body=openalex_work())
    router.add("https://www.nature.com/articles/nature14539.pdf", status=403, content=b"<html>Forbidden</html>")
    router.add("https://doi.org/10.1038/nature14539", status=302,
               headers={"location": "https://www.nature.com/articles/nature14539"})
    router.add("https://www.nature.com/articles/nature14539", headers={"content-type": "text/html"},
               content=b"<html><head><title>Deep learning | Nature</title></head><body>Buy article</body></html>")

    with pytest.raises(FetchFailed) as e:
        fetcher(config, client).get(parse("10.1038/nature14539"))

    outcomes = {(a.url, a.outcome) for a in e.value.attempts}
    assert ("https://www.nature.com/articles/nature14539.pdf", "blocked") in outcomes
    assert ("https://doi.org/10.1038/nature14539", "no-pdf") in outcomes
    assert any("institution.proxy" in h for h in e.value.hints)
    assert any("browser fallback is off" in h for h in e.value.hints)


def test_proxy_comes_before_the_publisher_in_the_browser(config, client, router):
    import httpx

    from paperreader.http import Response

    class FakeBrowser:
        urls = []

        def fetch(self, url, human_wait=0.0):
            self.urls.append(url)
            ok = url.startswith("https://proxy.example.edu/")
            return Response(url, 200 if ok else 403, httpx.Headers({"content-type": "application/pdf"}),
                            PDF if ok else b"")

    config.browser_enabled = True
    config.proxy = "https://proxy.example.edu/login?url={url}"
    router.add(OPENALEX, json_body=openalex_work())
    router.add("https://www.nature.com/articles/nature14539.pdf", status=403)
    router.add("https://doi.org/10.1038/nature14539", status=403)
    f = fetcher(config, client)
    f._browser = FakeBrowser()
    rec = f.get(parse("10.1038/nature14539"))
    assert f._browser.urls == ["https://proxy.example.edu/login?url=https://www.nature.com/articles/nature14539.pdf"]
    assert rec.source["via"] == "browser"


def test_bot_wall_is_recognized(config, client, router):
    router.add("https://openreview.net/pdf?id=abc", headers={"content-type": "text/html"},
               content=b"<html><head><title>Just a moment...</title></head></html>")
    with pytest.raises(FetchFailed) as e:
        fetcher(config, client).get(parse("openreview:abc"))
    assert [a.detail for a in e.value.attempts if a.via == "direct"] == ["bot check"]


def test_meta_refresh_is_followed(config, client, router):
    router.add(OPENALEX, json_body=openalex_work())
    router.add("https://www.nature.com/articles/nature14539.pdf", status=404)
    router.add("https://doi.org/10.1038/nature14539", headers={"content-type": "text/html"},
               content=b"<html><head><meta HTTP-EQUIV=\"REFRESH\" content=\"2; url='/real'\"></head></html>")
    router.add("https://doi.org/real", headers={"content-type": "text/html"},
               content=b'<html><head><meta name="citation_pdf_url" content="https://cdn.example/x.pdf"></head></html>')
    router.add("https://cdn.example/x.pdf", headers={"content-type": "application/pdf"}, content=PDF)
    rec = fetcher(config, client).get(parse("10.1038/nature14539"))
    assert rec.source["url"] == "https://cdn.example/x.pdf"


def test_ieee_pdf_is_found_by_article_number(config, client, router):
    router.add("https://doi.org/10.1109/icassp.2023.123", status=302,
               headers={"location": "https://ieeexplore.ieee.org/document/123"})
    router.add("https://ieeexplore.ieee.org/document/123", headers={"content-type": "text/html"},
               content=b"<html><head><title>A paper | IEEE Conference Publication</title></head></html>")
    router.add("https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=123",
               headers={"content-type": "application/pdf"}, content=PDF)
    rec = fetcher(config, client).get(parse("10.1109/icassp.2023.123"))
    assert rec.source["url"] == "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=123"


def test_pmc_full_text_xml(config, client, router):
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/search", json_body={"resultList": {"result": [{
        "title": "Sharing data.", "pubYear": "2007", "pmid": "1", "pmcid": "PMC9", "doi": "10.1371/x",
        "authorList": {"author": [{"firstName": "Heather A", "lastName": "Piwowar"}]},
    }]}})
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9/fullTextXML",
               headers={"content-type": "application/xml"},
               content=b"<?xml version='1.0'?><article><front><article-meta><title-group><article-title>Sharing data"
                       b"</article-title></title-group></article-meta></front><body><sec><title>Intro</title>"
                       b"<p>Sharing <italic>helps</italic>.</p></sec></body></article>")
    rec = fetcher(config, client).get(parse("pmid:1"))
    assert rec.key == "piwowar2007-sharing-data"
    assert "xml" in rec.files
    assert "## Intro\n\nSharing helps." in rec.text_path.read_text()


JATS_ARTICLE = (b"<article><front><article-meta><title-group><article-title>Talking to children matters"
                b"</article-title></title-group></article-meta></front><body><sec><title>Method</title>"
                b"<p>Recordings.</p></sec></body></article>")


def test_pmc_author_manuscript_through_ncbi_efetch(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/5510534",
         "version": "acceptedVersion"},
    ]))
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC5510534/fullTextXML", status=500)
    router.add("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
               headers={"content-type": "text/xml"},
               content=b"<?xml version='1.0'?><pmc-articleset>" + JATS_ARTICLE + b"</pmc-articleset>")

    rec = fetcher(config, client).get(parse("10.1038/nature14539"))

    assert rec.paper.pmcid == "PMC5510534"
    assert rec.source["source"] == "ncbi-eutils"
    assert "## Method\n\nRecordings." in rec.text_path.read_text()
    assert "https://www.ncbi.nlm.nih.gov/pmc/articles/5510534" not in router.urls()


def test_efetch_without_full_text_is_a_failure(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "pdf_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC6176610/pdf/x.pdf"},
    ]))
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC6176610/fullTextXML", status=500)
    router.add("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", headers={"content-type": "text/xml"},
               content=b"<?xml version='1.0'?><pmc-articleset><article><front/><!--The publisher of this article "
                       b"does not allow downloading of the full text in XML form.--></article></pmc-articleset>")
    router.add("https://www.nature.com/articles/nature14539.pdf", status=403)
    router.add("https://doi.org/10.1038/nature14539", status=403)

    with pytest.raises(FetchFailed) as e:
        fetcher(config, client).get(parse("10.1038/nature14539"))

    assert ("ncbi-eutils", "failed", "XML without the full text") in {
        (a.source, a.outcome, a.detail) for a in e.value.attempts}


def test_resolve_lists_candidates_without_downloading(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "https://hal.science/hal-1", "version": "submittedVersion"},
    ]))
    config.proxy = "https://login.proxy.example.edu/login?url={url}"
    paper, cands = fetcher(config, client).resolve(parse("10.1038/nature14539"))
    urls = [c.url for c in cands]
    assert urls[0] == "https://hal.science/hal-1"
    assert "https://www.nature.com/articles/nature14539.pdf" in urls
    assert "https://login.proxy.example.edu/login?url=https://doi.org/10.1038/nature14539" in urls
    assert all("hal.science" not in u for u in router.urls())


ARXIV_ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Attention Is All You Need</title><published>2017-06-12T17:57:34Z</published>
<author><name>Ashish Vaswani</name></author></entry></feed>"""


def test_year_of_an_arxiv_copy_comes_from_arxiv(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(publication_year=2025, locations=[
        {"is_oa": True, "landing_page_url": "http://arxiv.org/abs/1706.03762"},
    ]))
    router.add("https://export.arxiv.org/api/query", content=ARXIV_ATOM)
    router.add("https://arxiv.org/pdf/1706.03762", headers={"content-type": "application/pdf"}, content=PDF)
    rec = fetcher(config, client).get(parse("10.1038/nature14539"))
    assert rec.paper.year == 2017
    assert rec.key == "lecun2017-deep-learning"


def test_year_of_a_publisher_copy_is_kept(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "http://arxiv.org/abs/1706.03762"},
        {"is_oa": True, "pdf_url": "https://www.nature.com/x.pdf", "version": "publishedVersion"},
    ]))
    router.add("https://export.arxiv.org/api/query", content=ARXIV_ATOM)
    router.add("https://www.nature.com/x.pdf", headers={"content-type": "application/pdf"}, content=PDF)
    rec = fetcher(config, client).get(parse("10.1038/nature14539"))
    assert rec.paper.year == 2015
    assert not any("export.arxiv.org" in u for u in router.urls())


def test_search_prefers_arxiv_id_for_preprints(config, client, router):
    from paperreader.sources import hit_identifier, openalex_search

    router.add("https://api.openalex.org/works", json_body={"results": [
        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.65215/2q58a426", "type": "preprint",
         "display_name": "Attention Is All You Need", "publication_year": 2025,
         "locations": [{"landing_page_url": "https://doi.org/10.65215/2q58a426"},
                       {"landing_page_url": "http://arxiv.org/abs/1706.03762"}]},
        {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1038/nature14539", "type": "article",
         "display_name": "Deep learning", "locations": [{"landing_page_url": "http://arxiv.org/abs/1111.11111"}]},
        {"id": "https://openalex.org/W3", "doi": None, "type": "preprint", "display_name": "No DOI",
         "locations": [{"pdf_url": "https://arxiv.org/pdf/2222.22222"}]},
        {"id": "https://openalex.org/W4", "doi": None, "type": "article", "display_name": "Nothing", "locations": []},
    ]})
    hits = openalex_search(client, config, "attention")
    assert [hit_identifier(h) for h in hits] == ["arXiv:1706.03762", "doi:10.1038/nature14539",
                                                 "arXiv:2222.22222", None]


BIORXIV_DOI = "10.64898/2026.03.28.715023"
BIORXIV_API = f"https://api.biorxiv.org/details/biorxiv/{BIORXIV_DOI}"
BIORXIV_VERSIONS = {"collection": [
    {"title": "The Richness of Experience", "authors": "Raviv, H.; Chen, B.", "date": "2026-03-31", "version": "1",
     "jatsxml": "https://www.biorxiv.org/content/early/2026/03/31/v1.source.xml", "server": "bioRxiv"},
    {"title": "The First 1000 Days", "authors": "Raviv, H.; Chen, B.", "date": "2026-04-27", "version": "2",
     "jatsxml": "https://www.biorxiv.org/content/early/2026/04/27/v2.source.xml", "server": "bioRxiv"},
]}


def test_biorxiv_full_text_from_the_api(config, client, router):
    router.add(BIORXIV_API, json_body=BIORXIV_VERSIONS)
    router.add("https://www.biorxiv.org/content/early/2026/04/27/v2.source.xml",
               headers={"content-type": "application/xml"}, content=b"<?xml version='1.0'?>" + JATS_ARTICLE)
    rec = fetcher(config, client).get(parse(BIORXIV_DOI))
    assert rec.source["source"] == "biorxiv"
    assert (rec.paper.title, rec.paper.year, rec.paper.venue) == ("The First 1000 Days", 2026, "bioRxiv")
    assert rec.key == "raviv2026-first-1000-days"


def test_cloudflare_block_is_reported_and_not_repeated(config, client, router):
    router.add(BIORXIV_API, json_body=BIORXIV_VERSIONS)
    router.add("https://www.biorxiv.org/content/early/2026/04/27/v2.source.xml", status=429,
               headers={"server": "cloudflare"}, content=b"error code: 1015")
    with pytest.raises(FetchFailed) as e:
        fetcher(config, client).get(parse(BIORXIV_DOI))
    assert e.value.reason == "blocked by Cloudflare at www.biorxiv.org"
    assert e.value.open_url == "https://www.biorxiv.org/content/early/2026/04/27/v2.source.xml"
    biorxiv = [u for u in router.urls() if u.startswith("https://www.biorxiv.org/")]
    assert len(biorxiv) == 1  # the PDF on the same host is left for the browser
    assert ("skipped", "blocked by Cloudflare at www.biorxiv.org") in {(a.outcome, a.detail) for a in e.value.attempts}


def test_osf_preprint_through_the_api(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(locations=[
        {"is_oa": True, "landing_page_url": "https://osf.io/xbyr3", "version": "acceptedVersion"},
    ]))
    router.add("https://api.osf.io/v2/guids/xbyr3/", status=302,
               headers={"location": "https://api.osf.io/v2/preprints/xbyr3_v1/"})
    router.add("https://api.osf.io/v2/preprints/xbyr3_v1/", json_body={"data": {
        "type": "preprints", "attributes": {"title": "Moving beyond nouns", "date_published": "2021-11-23T07:10:51"},
        "relationships": {"primary_file": {"links": {"related": {"href": "https://api.osf.io/v2/files/619c/"}}}},
        "links": {"doi": "https://doi.org/10.1037/dev0001630"}}})
    router.add("https://api.osf.io/v2/files/619c/", json_body={"data": {
        "type": "files", "links": {"download": "https://osf.io/download/mqf5d/"}}})
    router.add("https://osf.io/download/mqf5d/", headers={"content-type": "application/pdf"}, content=PDF)
    rec = fetcher(config, client).get(parse("10.1038/nature14539"))
    assert rec.source["url"] == "https://osf.io/download/mqf5d/"
    assert rec.paper.osf == "xbyr3"
    assert "https://osf.io/xbyr3" not in router.urls()


def test_europe_pmc_pdf_is_left_for_the_browser(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(ids={"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/6176610"}))
    router.add("https://www.ebi.ac.uk/europepmc/webservices/rest/PMC6176610/fullTextXML", status=500)
    router.add("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", headers={"content-type": "text/xml"},
               content=b"<?xml version='1.0'?><pmc-articleset><article><front/></article></pmc-articleset>")
    router.add("https://europepmc.org/articles/PMC6176610", status=403)
    with pytest.raises(FetchFailed) as e:
        fetcher(config, client).get(parse("10.1038/nature14539"))
    assert ("europepmc-pdf", "blocked") in {(a.source, a.outcome) for a in e.value.attempts}
    assert e.value.reason == "europepmc.org refused plain HTTP (needs the browser)"
    assert e.value.open_url == "https://europepmc.org/articles/PMC6176610?pdf=render"


def test_issue_year_from_crossref(config, client, router):
    router.add(OPENALEX, json_body=openalex_work(publication_year=2013, locations=[
        {"is_oa": True, "pdf_url": "https://repository.example/x.pdf"}]))
    router.add("https://repository.example/x.pdf", headers={"content-type": "application/pdf"}, content=PDF)
    router.add("https://api.crossref.org/works/10.1038/nature14539", json_body={"message": {
        "type": "journal-article", "volume": "85", "issue": "3", "page": "956-970",
        "published-online": {"date-parts": [[2013, 12, 1]]}, "published-print": {"date-parts": [[2014, 5, 1]]}}})
    rec = fetcher(config, client).get(parse("10.1038/nature14539"))
    assert rec.paper.year == 2014
    assert (rec.paper.volume, rec.paper.issue, rec.paper.pages) == ("85", "3", "956-970")
    assert rec.key == "lecun2014-deep-learning"
