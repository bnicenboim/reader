"""Real papers from docs/literature-review-use-case.md. They hit real servers: `uv run pytest -m network`.

They run without the browser, as an agent in a cloud container would, with your config (email, keys)
and the shared rate-limit state, into a temporary library. Open-access copies move, so a failure
here is a reason to look, not proof of a bug.
"""

import pytest

from paperreader.config import load
from paperreader.fetch import Fetcher, FetchFailed
from paperreader.http import Cache, PoliteClient
from paperreader.identifiers import parse
from paperreader.library import Library
from paperreader.ratelimit import RateLimiter

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def fetcher(tmp_path_factory):
    cfg = load()
    cfg.library_dir = tmp_path_factory.mktemp("papers")
    limiter = RateLimiter(cfg.state_dir / "ratelimit.sqlite", cfg.limit_for)
    client = PoliteClient(cfg, limiter, Cache(cfg.state_dir / "cache.sqlite"))
    f = Fetcher(cfg, client, Library(cfg.library_dir), use_browser=False, log=print)
    yield f
    f.close()


@pytest.mark.parametrize("ident, sources", [
    ("10.1177/0956797613488145", {"ncbi-eutils"}),  # Weisleder & Fernald 2013: author manuscript PMC5510534
    ("10.1111/desc.13515", {"ncbi-eutils", "europepmc"}),  # Kosie & Lew-Williams 2024, PMC11333185
    ("10.1162/opmi_a_00172", {"europepmc"}),  # Nencheva et al. 2024, PMC11627589
    ("10.1111/desc.70101", {"europepmc"}),  # Bazhydai et al. 2025, PMC12662157
    ("10.1016/j.cognition.2021.104779", {"openalex", "unpaywall", "osf"}),  # Tsuji et al. 2021: HAL or OSF copy
    ("10.1109/tmm.2003.811618", {"semantic-scholar", "openalex", "unpaywall"}),  # Roy 2003: the author's MIT page
    ("10.3390/languages11050099", {"openalex", "unpaywall"}),  # Ashrafzadeh et al. 2026: Swansea repository
    ("arXiv:2201.02639", {"arxiv"}),  # Zellers et al. 2022
])
def test_regressions(fetcher, ident, sources):
    rec = fetcher.get(parse(ident), force=True)
    assert rec.source["source"] in sources


def test_jats_text_has_section_headings(fetcher):
    rec = fetcher.get(parse("10.1162/opmi_a_00172"))
    assert "\n## " in rec.text_path.read_text()


@pytest.mark.parametrize("ident, osf, sources", [
    ("10.1037/dev0001630", "xbyr3", {"osf", "ncbi-eutils"}),  # Casey et al. 2023: PMC has it too, and comes first
    ("10.1016/j.tics.2023.08.007", "qzbgx", {"osf"}),  # Frank 2023
])
def test_osf_preprints(fetcher, ident, osf, sources):
    rec = fetcher.get(parse(ident), force=True)
    assert rec.paper.osf == osf
    assert rec.source["source"] in sources


def test_biorxiv(fetcher):
    """Raviv et al. 2026: the API gives version 2; www.biorxiv.org may block plain HTTP with Cloudflare's 1015."""
    try:
        rec = fetcher.get(parse("10.64898/2026.03.28.715023"), force=True)
    except FetchFailed as e:
        assert e.paper.title.startswith("The First 1000 Days")
        assert e.reason.startswith("blocked by Cloudflare")
    else:
        assert rec.source["source"] == "biorxiv"


@pytest.mark.parametrize("ident, pmcid", [
    ("10.1073/pnas.1611621115", "PMC6176610"),  # Lytle et al. 2018
    ("10.1073/pnas.1532872100", "PMC166444"),  # Kuhl et al. 2003
])
def test_pmc_without_xml_needs_the_browser(fetcher, ident, pmcid):
    with pytest.raises(FetchFailed) as e:
        fetcher.get(parse(ident), force=True)
    assert e.value.paper.pmcid == pmcid
    outcomes = {(a.source, a.outcome, a.detail) for a in e.value.attempts}
    assert ("ncbi-eutils", "failed", "XML without the full text") in outcomes
    assert any(source == "europepmc-pdf" and outcome == "blocked" for source, outcome, _ in outcomes)


def test_hal_cover_page_and_exponents(fetcher):
    text = fetcher.get(parse("10.1111/cogs.12946"), force=True).text_path.read_text()  # Ludusan et al. 2021
    assert "HAL Authorization" not in text
    assert "p = 3.2e−6, η2 = .453]" in text


def test_watermarks(fetcher):
    text = fetcher.get(parse("10.1098/rstb.2024.0374"), force=True).text_path.read_text()  # Nikolaus & Fourtassi
    assert "Downloaded from" not in text and "HAL Id:" not in text


def test_issue_year_from_crossref(fetcher):
    assert fetcher.describe(parse("10.1111/cdev.12166")).year == 2014  # Roseberry et al., Child Development 85(3)


@pytest.mark.parametrize("ident", [
    "10.1121/10.0040820",  # AIP: a conference abstract
    "10.1016/j.cub.2024.12.052",  # Elsevier
    "10.1109/icassp49357.2023.10096250",  # IEEE
    "10.1073/pnas.1419773112",  # PNAS
])
def test_failures_say_why_and_where_to_go(fetcher, ident):
    try:
        fetcher.get(parse(ident), force=True)
    except FetchFailed as e:
        assert e.reason and e.open_url


def test_arxiv_html_with_figures_from_the_pdf(fetcher):
    rec = fetcher.get(parse("arXiv:1706.03762"), force=True)  # Vaswani et al. 2017
    text = rec.text_path.read_text()
    assert {"html", "pdf", "figures"} <= set(rec.files)
    assert "## 3 Model Architecture" in text and "\\mathrm{softmax}" in text
    assert "![Figure 1: The Transformer - model architecture.]" in text


def test_pmc_open_access_figures(fetcher):
    rec = fetcher.get(parse("10.1162/opmi_a_00172"), force=True)  # Nencheva et al. 2024, from Europe PMC's zip
    assert rec.source["figures"] >= 3


def test_pdf_figures_are_cropped(fetcher):
    rec = fetcher.get(parse("10.1111/cogs.12946"), force=True)  # Ludusan et al. 2021, the HAL PDF
    names = sorted(p.name for p in rec.files["figures"].iterdir())
    assert names[:2] == ["p10-figure-3.png", "p12-figure-4.png"] and "p5-figure-1.png" in names
