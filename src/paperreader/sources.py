"""Metadata and open-access lookups. Each function enriches a Paper and returns candidates."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from .config import Config
from .html import PageInfo, parse_page
from .http import PoliteClient
from .identifiers import ACL_DOI_PREFIX, ARXIV_NEW_RE, ARXIV_OLD_RE, PMCID_RE, arxiv_base, osf_from_url
from .models import Candidate, Paper

OPENALEX = "https://api.openalex.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
CROSSREF = "https://api.crossref.org/works"
S2 = "https://api.semanticscholar.org/graph/v1/paper"
ARXIV_API = "https://export.arxiv.org/api/query"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
BIORXIV_API = "https://api.biorxiv.org"
OSF_API = "https://api.osf.io/v2"

# bioRxiv and medRxiv DOIs: 10.1101/2020.03.28.013508, 10.1101/123456 (older), 10.64898/... (since 2025).
# Cold Spring Harbor's journals share 10.1101 but have letters in theirs (10.1101/gad.1234).
BIORXIV_DOI_RE = re.compile(r"10\.(?:1101|64898)/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{6,}")


def arxiv_from_url(url: str | None) -> str | None:
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?/?$", url or "")
    if m and (ARXIV_NEW_RE.fullmatch(m.group(1)) or ARXIV_OLD_RE.fullmatch(m.group(1))):
        return arxiv_base(m.group(1))
    return None


def pmcid_from(value: str | None) -> str | None:
    if not value:
        return None
    m = PMCID_RE.search(value)
    if m:
        return m.group(0).upper()
    m = re.search(r"(\d+)/?$", value)
    return f"PMC{m.group(1)}" if m else None


def pmc_from_url(url: str | None) -> str | None:
    """The PMCID of a PubMed Central or Europe PMC article URL."""
    m = re.search(r"(?:ncbi\.nlm\.nih\.gov/pmc/articles|pmc\.ncbi\.nlm\.nih\.gov/articles|europepmc\.org/articles)"
                  r"/(?:PMC)?(\d+)", url or "", re.IGNORECASE)
    return f"PMC{m.group(1)}" if m else None


def _strip_url(value: str | None, prefix: str) -> str | None:
    if not value:
        return None
    return value.split(prefix, 1)[-1].strip("/")


def derive_ids(paper: Paper) -> None:
    """Ids implied by other ids, e.g. ACL Anthology DOIs."""
    if paper.doi and paper.doi.startswith(ACL_DOI_PREFIX) and not paper.acl:
        paper.acl = paper.doi[len(ACL_DOI_PREFIX):]


# --- direct repository candidates ------------------------------------------------------------


def repository_candidates(paper: Paper) -> list[Candidate]:
    """Full text at well-known open repositories, from ids alone (no lookup needed)."""
    out = []
    if paper.arxiv:
        # The HTML version has real headings, LaTeX maths and tables; figure images come from the PDF.
        # arxiv.org has it for most papers, ar5iv (arXiv Labs) for older ones.
        pdf = f"https://arxiv.org/pdf/{paper.arxiv}"
        for url in (f"https://arxiv.org/html/{paper.arxiv}", f"https://ar5iv.labs.arxiv.org/html/{paper.arxiv}"):
            out.append(Candidate(url, "html", "arxiv", version="submittedVersion", companion=pdf))
        out.append(Candidate(pdf, "pdf", "arxiv", version="submittedVersion"))
    if paper.acl:
        out.append(Candidate(f"https://aclanthology.org/{paper.acl}.pdf", "pdf", "acl-anthology",
                             version="publishedVersion"))
    if paper.openreview:
        out.append(Candidate(f"https://openreview.net/pdf?id={paper.openreview}", "pdf", "openreview"))
    if paper.pmcid:
        out.append(Candidate(f"{EUROPEPMC}/{paper.pmcid}/fullTextXML", "jats", "europepmc", version="publishedVersion"))
        # Europe PMC's API only serves the open-access subset. NCBI's efetch also serves author manuscripts
        # (NIH public access), which the PMC website hides behind a bot check.
        out.append(Candidate(f"{EUTILS}/efetch.fcgi?db=pmc&id={paper.pmcid[3:]}", "jats", "ncbi-eutils"))
        # For publishers that refuse XML downloads (PNAS) both give the front matter only. Europe PMC's rendered
        # PDF refuses scripts (403), which puts it in the queue for the browser.
        out.append(Candidate(f"https://europepmc.org/articles/{paper.pmcid}?pdf=render", "pdf", "europepmc-pdf"))
    return out


# --- preprint servers (one API call each, only for their own ids) ---------------------------------


def biorxiv(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    """bioRxiv and medRxiv: the latest version's JATS XML and PDF, from the API.

    The full text is on www.biorxiv.org, which is quick to answer scripts with Cloudflare's error 1015.
    """
    if not (paper.doi and BIORXIV_DOI_RE.fullmatch(paper.doi)):
        return []
    for server in ("biorxiv", "medrxiv"):
        data = client.get_json(f"{BIORXIV_API}/details/{server}/{paper.doi}")
        if versions := (data or {}).get("collection") or []:
            break
    else:
        return []
    latest = versions[-1]
    date = latest.get("date") or ""
    paper.update(
        title=latest.get("title"),
        authors=[a.strip() for a in (latest.get("authors") or "").split(";") if a.strip()],
        year=int(date[:4]) if date[:4].isdigit() else None,
        venue=latest.get("server") or server,
        pub_type="posted-content",
    )
    version = f"v{latest['version']}" if latest.get("version") else ""
    pdf = f"https://www.{server}.org/content/{paper.doi}{version}.full.pdf"
    out = []
    if latest.get("jatsxml"):  # the XML's figures are separate images; take them from the PDF
        out.append(Candidate(latest["jatsxml"], "jats", server, version="submittedVersion", companion=pdf))
    out.append(Candidate(pdf, "pdf", server, version="submittedVersion"))
    return out


def osf(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    """OSF preprints (PsyArXiv, SocArXiv, ...): the preprint's primary file.

    Their pages are JavaScript applications without a PDF link; the API gives the download URL.
    """
    if not paper.osf:
        return []
    data = client.get_json(f"{OSF_API}/guids/{paper.osf}/")  # redirects to the preprint, file or project
    d = (data or {}).get("data") or {}
    if d.get("type") == "files":
        url = (d.get("links") or {}).get("download")
        return [Candidate(url, "pdf", "osf")] if url else []
    if d.get("type") != "preprints":
        return []
    a, links = d.get("attributes") or {}, d.get("links") or {}
    published = a.get("date_published") or ""
    paper.update(
        title=a.get("title"),
        year=int(published[:4]) if published[:4].isdigit() else None,
        # The journal article's DOI when there is one, else the preprint's.
        doi=_strip_url(links.get("doi") or links.get("preprint_doi"), "doi.org/"),
    )
    href = ((((d.get("relationships") or {}).get("primary_file") or {}).get("links") or {}).get("related") or {}).get(
        "href")
    if not href:
        return []
    f = (client.get_json(href) or {}).get("data") or {}
    url = (f.get("links") or {}).get("download")
    return [Candidate(url, "pdf", "osf")] if url else []


# --- OpenAlex ---------------------------------------------------------------------------------

def _openalex_auth(cfg: Config) -> dict | None:
    # Without a (free) key, requests share a small daily budget with everyone on the same IP.
    return {"Authorization": f"Bearer {cfg.openalex_key}"} if cfg.openalex_key else None


OPENALEX_FIELDS = "id,doi,display_name,publication_year,ids,authorships,primary_location,best_oa_location,locations"


def openalex(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    if paper.doi:
        key = "doi:" + quote(paper.doi, safe="/")
    elif paper.pmid:
        key = f"pmid:{paper.pmid}"
    elif paper.pmcid:
        key = f"pmcid:{paper.pmcid}"
    else:
        return []
    params = {"select": OPENALEX_FIELDS}
    if cfg.email:
        params["mailto"] = cfg.email
    w = client.get_json(f"{OPENALEX}/{key}", params, headers=_openalex_auth(cfg))
    if not w:
        return []
    ids = w.get("ids") or {}
    primary = w.get("primary_location") or {}
    paper.update(
        title=w.get("display_name"),
        authors=[(a.get("author") or {}).get("display_name") for a in w.get("authorships") or [] if a.get("author")],
        year=w.get("publication_year"),
        venue=(primary.get("source") or {}).get("display_name"),
        doi=_strip_url(w.get("doi"), "doi.org/"),
        pmid=_strip_url(ids.get("pmid"), "pubmed.ncbi.nlm.nih.gov/"),
        pmcid=pmcid_from(ids.get("pmcid")),
    )
    pdfs, landings = [], []
    locations = [w.get("best_oa_location")] + list(w.get("locations") or [])
    for loc in locations:
        if not loc or not loc.get("is_oa"):
            continue
        landing, pdf = loc.get("landing_page_url"), loc.get("pdf_url")
        if aid := arxiv_from_url(landing) or arxiv_from_url(pdf):
            paper.update(arxiv=aid)
            continue  # covered by repository_candidates
        if pmc := pmc_from_url(landing) or pmc_from_url(pdf):
            paper.update(pmcid=pmc)
            continue  # PMC pages sit behind a bot check; covered by repository_candidates
        if osf_id := osf_from_url(landing) or osf_from_url(pdf):
            paper.update(osf=osf_id)
            continue  # JavaScript pages without a PDF link; the OSF API gives the file
        if pdf:
            pdfs.append(Candidate(pdf, "pdf", "openalex", version=loc.get("version")))
        elif landing and "pubmed.ncbi.nlm.nih.gov" not in landing:
            landings.append(Candidate(landing, "landing", "openalex", version=loc.get("version")))
    return pdfs + landings


def openalex_search(client: PoliteClient, cfg: Config, query: str, n: int = 10) -> list[dict]:
    params = {"search": query, "per-page": str(n),
              "select": "id,doi,type,display_name,publication_year,authorships,primary_location,locations,open_access"}
    if cfg.email:
        params["mailto"] = cfg.email
    data = client.get_json(OPENALEX, params, headers=_openalex_auth(cfg), ttl=86400) or {}
    out = []
    for w in data.get("results", []):
        authors = [(a.get("author") or {}).get("display_name") for a in w.get("authorships") or []]
        arxiv = next((a for loc in w.get("locations") or []
                      if (a := arxiv_from_url(loc.get("landing_page_url")) or arxiv_from_url(loc.get("pdf_url")))),
                     None)
        out.append(
            {
                "title": w.get("display_name"),
                "type": w.get("type"),
                "arxiv": arxiv,
                "year": w.get("publication_year"),
                "authors": [a for a in authors if a],
                "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "doi": _strip_url(w.get("doi"), "doi.org/"),
                "openalex": _strip_url(w.get("id"), "openalex.org/"),
                "oa": (w.get("open_access") or {}).get("is_oa"),
            }
        )
    return out


def hit_identifier(hit: dict) -> str | None:
    """The identifier to fetch a search hit by.

    For a preprint on arXiv, the arXiv id rather than the DOI: OpenAlex sometimes gives a
    preprint the DOI of a later repost (e.g. 10.65215/... for "Attention Is All You Need").
    """
    if hit.get("arxiv") and (hit.get("type") == "preprint" or not hit.get("doi")):
        return f"arXiv:{hit['arxiv']}"
    return f"doi:{hit['doi']}" if hit.get("doi") else None


def crossref_search(client: PoliteClient, cfg: Config, query: str, n: int = 10) -> list[dict]:
    params = {"query.bibliographic": query, "rows": str(n),
              "select": "DOI,title,author,issued,container-title"}
    if cfg.email:
        params["mailto"] = cfg.email
    data = client.get_json(CROSSREF, params, ttl=86400) or {}
    out = []
    for it in (data.get("message") or {}).get("items", []):
        authors = [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in it.get("author") or []]
        year = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        out.append({
            "title": (it.get("title") or [None])[0],
            "year": year,
            "authors": [a for a in authors if a],
            "venue": (it.get("container-title") or [None])[0],
            "doi": (it.get("DOI") or "").lower() or None,
            "openalex": None,
            "oa": None,
        })
    return out


# --- Unpaywall --------------------------------------------------------------------------------


def unpaywall(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    if not (paper.doi and cfg.email):
        return []
    data = client.get_json(f"{UNPAYWALL}/{quote(paper.doi, safe='/')}", {"email": cfg.email})
    if not data:
        return []
    paper.update(title=data.get("title"), year=data.get("year"), venue=data.get("journal_name"))
    pdfs, landings = [], []
    locations = [data.get("best_oa_location")] + list(data.get("oa_locations") or [])
    for loc in locations:
        if not loc:
            continue
        pdf, landing = loc.get("url_for_pdf"), loc.get("url_for_landing_page")
        if aid := arxiv_from_url(landing) or arxiv_from_url(pdf):
            paper.update(arxiv=aid)
            continue
        if pmc := pmc_from_url(landing) or pmc_from_url(pdf):
            paper.update(pmcid=pmc)
            continue  # PMC pages sit behind a bot check; covered by repository_candidates
        if osf_id := osf_from_url(landing) or osf_from_url(pdf):
            paper.update(osf=osf_id)
            continue
        if pdf:
            pdfs.append(Candidate(pdf, "pdf", "unpaywall", version=loc.get("version")))
        elif landing:
            landings.append(Candidate(landing, "landing", "unpaywall", version=loc.get("version")))
    return pdfs + landings


# --- Semantic Scholar -------------------------------------------------------------------------


def semantic_scholar(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    ids = (("DOI", paper.doi), ("ARXIV", paper.arxiv), ("ACL", paper.acl),
           ("PMID", paper.pmid), ("PMCID", paper.pmcid and paper.pmcid[3:]))
    prefix, value = next(((p, v) for p, v in ids if v), (None, None))
    if not value:
        return []
    headers = {"x-api-key": cfg.semantic_scholar_key} if cfg.semantic_scholar_key else None
    data = client.get_json(
        f"{S2}/{prefix}:{quote(arxiv_base(value) if prefix == 'ARXIV' else value, safe='/')}",
        {"fields": "title,year,venue,authors,externalIds,openAccessPdf"},
        headers=headers,
    )
    if not data:
        return []
    ext = data.get("externalIds") or {}
    paper.update(
        title=data.get("title"),
        year=data.get("year"),
        venue=data.get("venue"),
        authors=[a.get("name") for a in data.get("authors") or [] if a.get("name")],
        doi=ext.get("DOI"),
        arxiv=ext.get("ArXiv"),
        acl=ext.get("ACL"),
        pmid=ext.get("PubMed"),
        pmcid=f"PMC{ext['PubMedCentral']}" if ext.get("PubMedCentral") else None,
    )
    url = (data.get("openAccessPdf") or {}).get("url")
    if osf_id := osf_from_url(url):
        paper.update(osf=osf_id)
        return []
    if pmc := pmc_from_url(url):
        paper.update(pmcid=pmc)
        return []  # PMC pages sit behind a bot check; covered by repository_candidates
    if url and not arxiv_from_url(url):
        return [Candidate(url, "pdf", "semantic-scholar")]
    return []


# --- Crossref ---------------------------------------------------------------------------------


def _date_year(d: dict | None) -> int | None:
    parts = ((d or {}).get("date-parts") or [[None]])[0]
    return parts[0] if parts and isinstance(parts[0], int) else None


def crossref(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    """Publication details for citations: type, volume, issue, pages, publisher, and the issue year.

    The issue year replaces the year from other sources, which is often the year the article
    appeared online (Child Development 85(3), 2014, was online in 2013). Bibliographies cite the issue.
    """
    if not paper.doi:
        return []
    params = {"mailto": cfg.email} if cfg.email else None
    data = client.get_json(f"{CROSSREF}/{quote(paper.doi, safe='/')}", params)
    m = (data or {}).get("message") or {}
    if not m:
        return []
    authors = [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in m.get("author") or []]
    paper.update(
        title=" ".join(((m.get("title") or [""])[0] or "").split()),
        authors=[a for a in authors if a],
        venue=(m.get("container-title") or [None])[0],
        pub_type=m.get("type"),
        volume=m.get("volume"),
        issue=m.get("issue"),
        pages=m.get("page"),
        publisher=m.get("publisher"),
    )
    if m.get("type") != "posted-content":  # a preprint's "issue" year means nothing
        issue = (m.get("journal-issue") or {}).get("published-print")
        year = _date_year(m.get("published-print")) or _date_year(issue) or _date_year(m.get("issued"))
        if year:
            paper.year = year
    return []


# --- arXiv ------------------------------------------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def _arxiv_entry(client: PoliteClient, arxiv_id: str) -> ET.Element | None:
    status, body = client.get_cached(ARXIV_API, {"id_list": arxiv_base(arxiv_id)}, ttl=30 * 86400)
    if status != 200:
        return None
    entry = ET.fromstring(body).find(f"{ATOM}entry")
    if entry is None or entry.find(f"{ATOM}title") is None:
        return None
    return entry


def _arxiv_published_year(entry: ET.Element) -> int | None:
    published = entry.findtext(f"{ATOM}published") or ""
    return int(published[:4]) if published[:4].isdigit() else None


def arxiv_metadata(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    if not paper.arxiv or (entry := _arxiv_entry(client, paper.arxiv)) is None:
        return []
    paper.update(
        title=" ".join((entry.findtext(f"{ATOM}title") or "").split()),
        authors=[a.findtext(f"{ATOM}name") for a in entry.findall(f"{ATOM}author")],
        year=_arxiv_published_year(entry),
        venue=entry.findtext(f"{ARXIV_NS}journal_ref") or "arXiv",
        doi=entry.findtext(f"{ARXIV_NS}doi"),
    )
    return []


def arxiv_year(client: PoliteClient, arxiv_id: str) -> int | None:
    """Year of the first arXiv version."""
    entry = _arxiv_entry(client, arxiv_id)
    return _arxiv_published_year(entry) if entry is not None else None


# --- Europe PMC -------------------------------------------------------------------------------


def europepmc_metadata(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    if paper.pmcid:
        query = f"PMCID:{paper.pmcid}"
    elif paper.pmid:
        query = f"EXT_ID:{paper.pmid} AND SRC:MED"
    else:
        return []
    data = client.get_json(f"{EUROPEPMC}/search", {"query": query, "format": "json", "resultType": "core"})
    results = ((data or {}).get("resultList") or {}).get("result") or []
    if not results:
        return []
    r = results[0]
    authors = [" ".join(filter(None, (a.get("firstName"), a.get("lastName")))) or a.get("fullName")
               for a in (r.get("authorList") or {}).get("author") or []]
    paper.update(
        title=(r.get("title") or "").rstrip("."),
        authors=[a for a in authors if a],
        year=int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
        venue=((r.get("journalInfo") or {}).get("journal") or {}).get("title"),
        doi=r.get("doi"),
        pmid=r.get("pmid"),
        pmcid=r.get("pmcid"),
    )
    return []


# --- ACL Anthology ---------------------------------------------------------------------------


def acl_metadata(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    if not paper.acl:
        return []
    r = client.get(f"https://aclanthology.org/{paper.acl}/")
    if r.ok:
        update_from_page(paper, parse_page(r.text, r.url))
    return []


# --- OpenReview -------------------------------------------------------------------------------


def openreview_metadata(client: PoliteClient, cfg: Config, paper: Paper) -> list[Candidate]:
    if not paper.openreview:
        return []
    data = client.get_json("https://api2.openreview.net/notes", {"id": paper.openreview})
    notes = (data or {}).get("notes") or []
    if not notes:
        return []
    content = notes[0].get("content") or {}

    def val(k):
        v = content.get(k)
        return v.get("value") if isinstance(v, dict) else v

    paper.update(title=val("title"), authors=val("authors") or [], venue=val("venue"))
    return []


# --- landing page metadata --------------------------------------------------------------------


def update_from_page(paper: Paper, page: PageInfo) -> None:
    """Fill in metadata from Highwire/Dublin Core tags on a landing page."""
    year = None
    for name in ("citation_publication_date", "citation_date", "citation_online_date", "citation_year", "dc.date"):
        if (v := page.first(name)) and (m := re.search(r"(1[89]|20)\d\d", v)):
            year = int(m.group(0))
            break
    paper.update(
        title=page.first("citation_title") or page.first("dc.title"),
        authors=page.meta.get("citation_author") or page.meta.get("dc.creator") or [],
        year=year,
        venue=page.first("citation_journal_title") or page.first("citation_conference_title")
        or page.first("citation_inbook_title"),
        doi=page.doi,
        arxiv=page.first("citation_arxiv_id"),
    )
