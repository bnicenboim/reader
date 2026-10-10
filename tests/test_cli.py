import json

import pytest
from click.testing import CliRunner
from conftest import make_pdf

from paperreader import cli as cli_mod

PDF = make_pdf(["Deep learning", "Yann LeCun", "doi:10.1038/nature14539"])
OPENALEX = "https://api.openalex.org/works/doi:10.1038/nature14539"
WORK = {
    "display_name": "Deep learning", "publication_year": 2015, "doi": "https://doi.org/10.1038/nature14539",
    "authorships": [{"author": {"display_name": "Yann LeCun"}}],
    "primary_location": {"source": {"display_name": "Nature"}},
    "locations": [{"is_oa": True, "pdf_url": "https://repository.example/x.pdf"}],
}


@pytest.fixture
def run(config, client, monkeypatch):
    monkeypatch.setattr(cli_mod.config_mod, "load", lambda path=None: config)
    monkeypatch.setattr(cli_mod.App, "client", property(lambda self: client))

    def invoke(*args, input=None):
        return CliRunner().invoke(cli_mod.cli, list(args), input=input, catch_exceptions=False)

    return invoke


def test_batch_with_keys_export_summary_and_pending(run, router, tmp_path):
    router.add(OPENALEX, json_body=WORK)
    router.add("https://repository.example/x.pdf", headers={"content-type": "application/pdf"}, content=PDF)
    router.add("https://doi.org/10.1234/paywalled", status=403)
    keys = tmp_path / "keys.tsv"
    keys.write_text("# key\tid\nlecun2015\t10.1038/nature14539\nsmith2020\t10.1234/paywalled\n")

    r = run("get", "-q", "--from", str(keys), "--export", str(tmp_path / "fulltext"), "--name", "{key}.txt")

    assert r.exit_code == 1
    lines = r.output.splitlines()
    assert lines[0].split() == ["ok", "lecun2015", "openalex"]
    assert lines[1].split(None, 2) == ["FAIL", "smith2020",
                                       "no open copy; the publisher refused plain HTTP (needs the browser)"]
    assert (tmp_path / "fulltext" / "lecun2015.txt").read_text().startswith("# Deep learning")
    assert run("pending", "--tsv").output == "smith2020\tdoi:10.1234/paywalled\n"

    # The library key works wherever an identifier does.
    assert run("read", "lecun2015", "--pages", "1").output.startswith("# Deep learning")


def test_bib_uses_crossref_details(run, router):
    router.add(OPENALEX, json_body=WORK)
    router.add("https://api.crossref.org/works/10.1038/nature14539", json_body={"message": {
        "type": "journal-article", "volume": "521", "issue": "7553", "page": "436-444",
        "published-print": {"date-parts": [[2015, 5, 28]]}}})
    out = run("bib", "10.1038/nature14539").output
    assert out.startswith("@article{lecun2015deep,")
    assert "journal = {Nature}" in out and "pages = {436--444}" in out


def test_add_a_pdf_downloaded_by_hand(run, router, config, tmp_path):
    router.add(OPENALEX, json_body=WORK)
    pdf = tmp_path / "download.pdf"
    pdf.write_bytes(PDF)
    r = run("add", str(pdf))
    assert "using doi:10.1038/nature14539" in r.output
    rec = json.loads((config.library_dir / "lecun2015-deep-learning.json").read_text())
    assert rec["source"]["via"] == "by hand" and rec["paper"]["title"] == "Deep learning"
    assert run("add", str(pdf)).exit_code == 1  # already there; --force replaces it
