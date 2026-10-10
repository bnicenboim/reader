import pytest

from paperreader.identifiers import Identifier, UnrecognizedIdentifier, parse


@pytest.mark.parametrize(
    "text, kind, value",
    [
        ("10.1038/nature14539", "doi", "10.1038/nature14539"),
        ("doi:10.1038/NATURE14539", "doi", "10.1038/nature14539"),
        ("https://doi.org/10.1038/nature14539", "doi", "10.1038/nature14539"),
        ("https://dx.doi.org/10.1002/(SICI)1097-4571", "doi", "10.1002/(sici)1097-4571"),
        ("10.1000/xyz.", "doi", "10.1000/xyz"),
        ("1706.03762", "arxiv", "1706.03762"),
        ("arXiv:1706.03762v5", "arxiv", "1706.03762v5"),
        ("https://arxiv.org/abs/1706.03762", "arxiv", "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762v2.pdf", "arxiv", "1706.03762v2"),
        ("hep-th/9901001", "arxiv", "hep-th/9901001"),
        ("10.48550/arXiv.2005.02721", "arxiv", "2005.02721"),
        ("pmid:17375194", "pmid", "17375194"),
        ("https://pubmed.ncbi.nlm.nih.gov/17375194/", "pmid", "17375194"),
        ("PMC1817752", "pmcid", "PMC1817752"),
        ("pmc:1817752", "pmcid", "PMC1817752"),
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC1817752/", "pmcid", "PMC1817752"),
        ("2020.acl-main.1", "acl", "2020.acl-main.1"),
        ("P19-1001", "acl", "P19-1001"),
        ("https://aclanthology.org/2020.acl-main.1.pdf", "acl", "2020.acl-main.1"),
        ("https://openreview.net/forum?id=YicbFdNTTy", "openreview", "YicbFdNTTy"),
        ("https://dl.acm.org/doi/pdf/10.1145/3442188.3445922", "doi", "10.1145/3442188.3445922"),
        ("https://link.springer.com/content/pdf/10.1007/s10994-021-06059-7.pdf", "doi", "10.1007/s10994-021-06059-7"),
        ("https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0000308", "doi",
         "10.1371/journal.pone.0000308"),
        ("https://proceedings.mlr.press/v139/radford21a.html", "url",
         "https://proceedings.mlr.press/v139/radford21a.html"),
        ("url:https://proceedings.mlr.press/v139/radford21a.html", "url",
         "https://proceedings.mlr.press/v139/radford21a.html"),
        ("osf:xbyr3", "osf", "xbyr3"),
        ("https://osf.io/xbyr3", "osf", "xbyr3"),
        ("https://osf.io/preprints/psyarxiv/xbyr3_v1/", "osf", "xbyr3"),
        ("https://psyarxiv.com/qzbgx/download", "osf", "qzbgx"),
        ("https://osf.io/download/mqf5d/", "url", "https://osf.io/download/mqf5d/"),
    ],
)
def test_parse(text, kind, value):
    ident = parse(text)
    assert (ident.kind, ident.value) == (kind, value)


def test_url_kept_as_hint():
    ident = parse("https://dl.acm.org/doi/10.1145/3442188.3445922")
    assert ident == Identifier("doi", "10.1145/3442188.3445922", "https://dl.acm.org/doi/10.1145/3442188.3445922")


@pytest.mark.parametrize("text", ["", "attention is all you need", "12345"])
def test_unrecognized(text):
    with pytest.raises(UnrecognizedIdentifier):
        parse(text)
