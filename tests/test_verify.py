from conftest import make_pdf

from paperreader.models import Paper
from paperreader.verify import check, head_text, title_words

VERSACE = Paper(title="Priors in Animal and Artificial Intelligence: Where Does Learning Begin?",
                authors=["Elisabetta Versace"], doi="10.1016/j.tics.2018.07.005")

# The first page of the PDF that was stored under Versace et al. (two columns, as pdfium extracts them).
LISMAN_PAGE = """Neuron, Vol. 46, 703–713, June 2, 2005, Copyright ©2005 by Elsevier Inc.
DOI 10.1016/j.neuron.2005.05.002
The Hippocampal-VTA Loop: Review
Controlling the Entry of Information
into Long-Term Memory
John E. Lisman1,* and Anthony A. Grace2 perspective: viewed in an integrated manner, these developments strongly
suggest that the hippocampus 1Department of Biology Brandeis University and VTA form a functional loop designed to
detect nov415 South Street elty and to use this novelty signal to control the entry of behaviorally significant
information into the hippocampal store of long-term memory. In this article we develop the concept that the
hippocampus and the midbrain dopaminergic neurons of the ventral tegmental area (VTA) form a functional loop.
In such experiments there is a motor component that signals the animal's response, but the firing is not related
to this motor component; learning and memory begin where novelty is detected."""

# A first page with ligatures (U+FB01), a running head between the title lines and a title word
# hyphenated across lines.
VERSACE_PAGE = """Trends in Cognitive Sciences
Opinion
Priors in Animal and Artiﬁcial
TICS 1812 No. of Pages 12
Intel-
ligence: Where Does Learning Begin?
Elisabetta Versace,1,* Antone Martinho III,2 Alex Kacelnik,2 and Giorgio Vallortigara3
A major goal for the next generation of AI is to build machines that are able to reason and cope with novel
tasks, environments, and situations in a manner that approximates that of humans or animals. Here, we show
how insights from animal cognition can help us to identify what prior knowledge is needed (ﬁrst steps)..."""


def test_title_words_are_folded_and_distinctive():
    assert title_words("Infant–Caregiver <i>Interaction</i>: The Child&#x2019;s View of 1000 Days") == [
        "infant", "caregiver", "interaction", "child", "view", "1000", "days"]


def test_title_survives_ligatures_hyphenation_and_a_running_head():
    paper = Paper(title="Priors in Animal and Artificial Intelligence: Where Does Learning Begin?",
                  authors=["Elisabetta Versace"])  # no DOI: the title has to do it
    assert check(paper, VERSACE_PAGE).detail == "7 of 7 title words on the first pages"


def test_accents_and_punctuation_do_not_matter():
    paper = Paper(title="Évolution des priors — ‘innés’ ou appris ? Le cas des poussins", doi="10.1/x")
    text = "Evolution des priors : << innes >> ou appris? Le cas des poussins\n" + "Résumé. " * 30
    assert check(paper, text).ok is True


def test_another_paper_is_a_mismatch():
    verdict = check(VERSACE, LISMAN_PAGE)
    assert verdict.ok is False
    assert verdict.detail.endswith("and no DOI")


def test_a_paper_in_the_same_field_does_not_pass_on_shared_words():
    # Schatz et al. 2021 shares most words with the title of Lavechin et al. 2025.
    lavechin = Paper(title="Simulating Early Phonetic and Word Learning Without Linguistic Categories",
                     doi="10.1111/desc.13606")
    schatz = ("Early phonetic learning without phonetic categories: Insights from large-scale simulations on "
              "realistic input. Thomas Schatz, Naomi H. Feldman, Sharon Goldwater, Xuan-Nga Cao, Emmanuel "
              "Dupoux. Before they even speak, infants become attuned to the sounds of the language(s) they hear, "
              "processing native phonetic contrasts more easily than nonnative ones. Word learning ...")
    assert check(lavechin, schatz).ok is False


def test_pmc_front_matter_without_a_doi_matches_on_the_title():
    paper = Paper(title="Talking to Children Matters: Early Language Experience Strengthens Processing "
                        "and Builds Vocabulary", pmid="1", doi="10.1177/0956797613488145")
    xml = (b"<?xml version='1.0'?><pmc-articleset><article><front><journal-meta><journal-title>Psychol Sci"
           b"</journal-title></journal-meta><article-meta><article-id pub-id-type='pmid'>1</article-id>"
           b"<title-group><article-title>Talking to Children Matters: Early Language Experience Strengthens "
           b"Processing and Builds Vocabulary</article-title></title-group><abstract><p>Infants differ "
           b"substantially in their rates of language growth, and slow growth predicts later academic "
           b"difficulties.</p></abstract></article-meta></front><body><p>Text.</p></body></article>"
           b"</pmc-articleset>")
    text = head_text(xml, "xml")
    assert "Psychol Sci" in text and "Text." not in text
    assert check(paper, text).ok is True


def test_doi_broken_across_lines_is_found():
    text = "Some Other Title Entirely\nhttps://doi.org/10.1016/j.tics.\n2018.07.005\n" + "Body text. " * 20
    assert check(VERSACE, text).detail == "DOI on the first pages"


def test_arxiv_stamp_counts_as_an_id():
    paper = Paper(title="A title that the preprint changed later", doi="10.1/x", arxiv="1706.03762")
    text = "Attention Is All You Need\narXiv:1706.03762v7 [cs.CL] 2 Aug 2023\n" + "Body text. " * 20
    assert check(paper, text).ok is True


def test_short_titles_need_every_word():
    weisleder = Paper(title="Talking to Children Matters")
    text = "Children's talking time and vocabulary in toddlers. " * 5  # "matters" is missing
    assert check(weisleder, text).ok is False


def test_nothing_to_compare_is_accepted():
    assert check(Paper(title="Deep learning"), "").ok is None  # a scan without a text layer
    assert check(Paper(url="https://x.example/a.pdf"), LISMAN_PAGE).ok is None


def test_a_doi_without_a_title_must_be_on_the_page():
    assert check(Paper(doi="10.1016/j.tics.2018.07.005"), LISMAN_PAGE).ok is False
    assert check(Paper(doi="10.1016/j.neuron.2005.05.002"), LISMAN_PAGE).ok is True


def test_head_text_of_a_pdf():
    pdf = make_pdf(["Priors in Animal and Artificial", "Intelligence: Where Does Learning Begin?"])
    assert "Where Does Learning Begin?" in head_text(pdf, "pdf")
    assert head_text(b"%PDF-1.4 broken", "pdf") == ""
