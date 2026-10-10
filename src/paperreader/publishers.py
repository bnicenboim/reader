"""Where publishers keep their PDFs, by DOI prefix, and how to go through a library proxy."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from .config import Config
from .models import Candidate, Paper

# Direct PDF URL patterns for large publishers. Saves fetching the landing page first.
# Anything not listed here is found through the DOI landing page's citation_pdf_url.
PDF_TEMPLATES: dict[str, str] = {
    "10.1145": "https://dl.acm.org/doi/pdf/{doi}",  # ACM
    "10.1007": "https://link.springer.com/content/pdf/{doi}.pdf",  # Springer
    "10.1038": "https://www.nature.com/articles/{suffix}.pdf",  # Nature portfolio
    "10.1002": "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",  # Wiley
    "10.1111": "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",  # Wiley (Blackwell)
    "10.1080": "https://www.tandfonline.com/doi/pdf/{doi}",  # Taylor & Francis
    "10.1177": "https://journals.sagepub.com/doi/pdf/{doi}",  # SAGE
    "10.1073": "https://www.pnas.org/doi/pdf/{doi}",  # PNAS
    "10.1126": "https://www.science.org/doi/pdf/{doi}",  # Science
    "10.3389": "https://www.frontiersin.org/articles/{doi}/pdf",  # Frontiers
}

ELSEVIER_PREFIX = "10.1016"


def publisher_pdf_url(doi: str) -> str | None:
    prefix, _, suffix = doi.partition("/")
    template = PDF_TEMPLATES.get(prefix)
    return template.format(doi=doi, suffix=suffix) if template else None


def publisher_candidates(paper: Paper) -> list[Candidate]:
    if not paper.doi:
        return []
    out = []
    if pdf := publisher_pdf_url(paper.doi):
        out.append(Candidate(pdf, "pdf", "publisher"))
    out.append(Candidate(f"https://doi.org/{paper.doi}", "landing", "publisher"))
    return out


def elsevier_candidates(cfg: Config, paper: Paper) -> list[Candidate]:
    """Elsevier's TDM API returns full text when the request comes from an entitled network."""
    if not (cfg.elsevier_key and paper.doi and paper.doi.startswith(ELSEVIER_PREFIX + "/")):
        return []
    headers = [("X-ELS-APIKey", cfg.elsevier_key), ("Accept", "application/pdf")]
    if cfg.elsevier_insttoken:
        headers.append(("X-ELS-Insttoken", cfg.elsevier_insttoken))
    url = f"https://api.elsevier.com/content/article/doi/{quote(paper.doi, safe='/')}"
    return [Candidate(url, "pdf", "elsevier-api", headers=tuple(headers))]


def proxied(cfg: Config, url: str) -> str:
    """Route a URL through the institution's proxy (EZproxy, OpenAthens redirector, ...)."""
    template = cfg.proxy or ""
    if "{url_encoded}" in template:
        return template.replace("{url_encoded}", quote(url, safe=""))
    if "{url}" in template:
        return template.replace("{url}", url)
    return template + url


def proxy_host(cfg: Config) -> str | None:
    return urlsplit(cfg.proxy).hostname if cfg.proxy else None
