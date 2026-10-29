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


def test_verify_reports_mismatches_and_changes_nothing_without_remove(run, config):
    from paperreader.library import Library
    from paperreader.models import Paper

    lib = Library(config.library_dir)
    good_pdf = make_pdf(["Deep learning", "Yann LeCun, Yoshua Bengio and Geoffrey Hinton",
                         "Deep learning allows computational models that are composed of multiple processing layers",
                         "to learn representations of data with multiple levels of abstraction."])
    good = lib.save(Paper(title="Deep learning", authors=["Yann LeCun"], year=2015, doi="10.1038/nature14539"),
                    good_pdf, "pdf", "text", {"source": "openalex", "url": "https://repository.example/x.pdf"})
    wrong_pdf = make_pdf(["The Hippocampal-VTA Loop: Controlling the Entry of Information into Long-Term Memory",
                          "John E. Lisman and Anthony A. Grace", "DOI 10.1016/j.neuron.2005.05.002",
                          "In this article we develop the concept that the hippocampus and the midbrain"])
    bad = lib.save(Paper(title="Priors in Animal and Artificial Intelligence: Where Does Learning Begin?",
                         authors=["Elisabetta Versace"], year=2018, doi="10.1016/j.tics.2018.07.005"),
                   wrong_pdf, "pdf", "text", {"source": "publisher", "url": "https://www.cell.com/x.pdf"})
    files = sorted(p.name for p in config.library_dir.iterdir())

    r = run("verify")
    assert r.exit_code == 1
    assert f"MISMATCH  {bad.key}" in r.output and good.key not in r.output
    assert r.output.splitlines()[-1] == "2 paper(s) checked: 1 match, 1 mismatched, 0 could not be checked"
    assert sorted(p.name for p in config.library_dir.iterdir()) == files  # read-only

    assert json.loads(run("verify", "--json", good.key).output)[0]["ok"] is True
    assert run("verify", "doi:10.1038/nature14539").exit_code == 0

    r = run("verify", "--remove")
    assert f"moved to {config.library_dir / 'mismatched'}" in r.output
    assert [rec.key for rec in lib.all()] == [good.key]
    assert (config.library_dir / "mismatched" / f"{bad.key}.pdf").exists()


def test_read_shows_figures_with_absolute_paths(run, router, config):
    pytest.importorskip("pymupdf")
    from test_figures import make_paper_pdf

    router.add(OPENALEX, json_body=WORK)
    router.add("https://repository.example/x.pdf", headers={"content-type": "application/pdf"},
               content=make_paper_pdf())
    run("get", "-q", "10.1038/nature14539")
    figdir = (config.library_dir / "lecun2015-deep-learning.figures").resolve()
    out = run("read", "lecun2015-deep-learning", "--figures").output
    assert out == (f"p2    Figure 1. A blue box, for testing.\n      {figdir}/p2-figure-1.png\n"
                   f"p2    Table 1. Numbers.\n      {figdir}/p2-table-1.png\n")
    text = run("read", "10.1038/nature14539").output
    assert f"![Figure 1. A blue box, for testing.]({figdir}/p2-figure-1.png)" in text
    # Papers stored before figures existed get them with `paper figures`.
    assert run("figures", "lecun2015-deep-learning", "--rebuild", "--no-browser").output == out


def test_install_skill(run, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    result = run("install-skill")
    path = tmp_path / "claude" / "skills" / "paperreader" / "SKILL.md"
    assert result.output.strip() == str(path)
    assert path.read_text().startswith("---\nname: paperreader\n")
