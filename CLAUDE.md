# CLAUDE.md

## What this is

`paper` (package `paperreader`, in `src/paperreader/`) is a command-line tool that
downloads a research paper from a DOI, arXiv id, PMID/PMCID, ACL Anthology id,
OpenReview id, URL or exact title. It stores the paper in the library
(`~/papers` by default) as three files: the PDF (or JATS XML), a Markdown text
version with `<!-- page N -->` markers, and a JSON metadata sidecar.

Goal: if a paper is open access or the user has institutional access to it, an
agent should be able to fetch and read it without the user downloading it by
hand, and without putting unreasonable load on any server.

## Commands

```console
curl -LsSf https://astral.sh/uv/install.sh | sh   # only if uv is missing; installs to ~/.local/bin
uv sync --all-extras         # install, including the Playwright browser extra
uv run paper install-browser # Chromium for the browser route, once per machine (~/.cache/ms-playwright)
uv run pytest                # unit tests; mocked transport, never touch the network
uvx ruff check src tests     # lint (line length 120)
uv run paper --help
```

Tests marked `network` hit real servers and are deselected by default.

## Layout

| module | role |
| --- | --- |
| `identifiers.py` | parse user input into a normalized `Identifier` |
| `config.py` | TOML config, per-host `Limit`s, config template |
| `ratelimit.py` | per-host spacing plus hourly/daily caps, stored in SQLite and shared across processes |
| `http.py` | `PoliteClient`: rate-limits each redirect hop, honours `Retry-After`, caches API answers |
| `sources.py` | OpenAlex, Unpaywall, Semantic Scholar, arXiv, Europe PMC, NCBI E-utilities, ACL, OpenReview, Crossref search |
| `publishers.py` | PDF URL templates by DOI prefix, Elsevier API, institutional proxy URL rewriting |
| `html.py` | landing-page metadata (`citation_pdf_url`, meta refresh) and bot-wall detection |
| `browser.py` | Playwright with a persistent profile, `login`, human-check handling |
| `fetch.py` | the pipeline: candidate order, attempts, browser retries, hints on failure |
| `extract.py` | PDF to text (pypdfium2) and JATS to Markdown |
| `library.py` | storage, readable keys (`vaswani2017-attention-all-you-need`), lookup by any id |
| `cli.py` | `get`, `read`, `resolve`, `search`, `list`, `login`, `usage`, `install-browser`, `config` |

## Fetch order

Cheapest and most polite first; stop at the first route that works:

1. The library, matched on any id (a paper fetched by arXiv id is found again by DOI).
2. Open repositories: arXiv, ACL Anthology, OpenReview, PubMed Central full text via Europe PMC
   and NCBI E-utilities.
3. Open-access copies listed by OpenAlex, Unpaywall and Semantic Scholar.
   Versions of record come before preprints.
4. The publisher over plain HTTP (works on a campus network or VPN).
5. The institution's proxy, in a real browser with a persistent profile that keeps the user's logins.
6. That browser for sites that refused scripts. It comes after the proxy because a paywalled publisher
   page fails there anyway, sometimes after a human check that waits for the user.

Lookups are lazy: Unpaywall and Semantic Scholar are only called if the earlier candidates fail.

## Rules to preserve

Politeness:
- Every request goes through `RateLimiter.acquire`, including each redirect hop and each
  page the browser opens. Do not add a code path that bypasses it.
- Defaults: at least 5 s between requests to the same host, at most 60 per hour and 300
  per day. APIs get their published rates (arXiv: one request per 3 s). Be careful
  raising publisher limits: publishers can cut off a whole university's access over
  bulk downloading.
- `paper usage` shows consumption against the budgets.

Dealing with blocking:
- Plain HTTP requests identify themselves honestly (`paperreader/<version> (+repo; mailto:...)`).
- When a site shows a human check, a visible browser window opens and the user completes it;
  the session is reused afterwards.
- Automated CAPTCHA solving is allowed (the user approved it) but not implemented yet. If
  added, it must go through the same per-host rate limits as everything else.
- Do not add browser fingerprint spoofing, fake browser user agents, or shadow libraries
  such as Sci-Hub.

## Status

Verified on real papers: arXiv, ACL Anthology, PMLR, HAL (the green open-access copy of a
paywalled Nature paper) and PubMed Central. The browser's PDF download path works.
The institutional proxy works: on 10 October 2026 an IEEE paper came through Tilburg's EZproxy
after `paper login`, with the saved session and a visible window.
There are 68 unit tests, all passing.

Not verified:
- The proxy route for publishers other than IEEE (AIP reached the article page through it).
- ScienceDirect blocked the development server's IP, even in the browser.
- ACM and OpenReview show a Cloudflare check whose verification server was blocked by the
  development sandbox's network, so they failed there. They may work from a normal
  machine and network.

## Setup the user needs

`paper config --init` writes `~/.config/paperreader/config.toml`. Then:
- `email`: required by Unpaywall.
- `keys.openalex`: free. Without it, OpenAlex shares a small daily allowance with everyone
  on the same IP address; once it runs out, title search falls back to Crossref, which
  matches poorly (it did not find "Attention is all you need"). The key can also come from
  the `OPENALEX_API_KEY` environment variable (how cloud sessions get it).
- `institution.proxy`: the library's proxy URL template, then `paper login` once to sign in.
  Tilburg University uses OCLC-hosted EZproxy: `https://tilburguniversity.idm.oclc.org/login?url={url}`,
  which signs in through SURFconext.

## Things learned the hard way

- Headless Chromium shows PDFs in its viewer instead of downloading them. `browser.py`
  sets `plugins.always_open_pdf_externally` in the profile's Preferences, with an in-page
  `fetch()` as a fallback.
- Cloudflare injects `/cdn-cgi/challenge-platform/...` into ordinary pages; it is not a
  bot wall by itself. `_cf_chl_opt` and titles like "Just a moment..." are.
- Springer served the PDF to the honest user agent but blocked a fake browser one.
- pypdfium2 marks soft line-break hyphens with U+FFFE; `extract._clean` removes them.
- Elsevier's DOI landing goes through linkinghub with a `<meta http-equiv="refresh">`;
  the pipeline follows it.
- Elsevier's API returns a first-page preview when not entitled; a one-page result counts
  as a failure.
- OpenAlex charges title search 10x a lookup against the shared free allowance; the key is
  sent as an `Authorization: Bearer` header so it stays out of URLs and cache keys.
- Semantic Scholar often returns 429 without a key.
- PMC article pages (ncbi.nlm.nih.gov/pmc, pmc.ncbi.nlm.nih.gov) show a bot check to scripts, so PMC
  locations from OpenAlex and Unpaywall are turned into a PMCID instead of being fetched. Europe PMC's
  `fullTextXML` serves only the open-access subset (HTTP 500 otherwise). NCBI `efetch` (`db=pmc`) also
  serves author manuscripts, wrapped in `<pmc-articleset>`; for publishers that refuse XML downloads
  (PNAS, for one) it returns the front matter only, which counts as a failure.
- Institutional sign-in passes through identity providers (EZproxy, SURFconext, Microsoft, ADFS).
  `html.looks_like_sign_in` spots those pages; the browser waits on them, and if the chain stops there
  (Microsoft's "Pick an account" needs a click) the page counts as a check and a visible window opens.
- Microsoft's sign-in page has a `<noscript>` refresh to `/jsdisabled`; the page parser ignores
  everything inside `<noscript>`.
- EZproxy and the identity providers keep the sign-in in session cookies, which Chromium drops on exit.
  `browser.py` saves them to `session-cookies.json` in the state directory (also while `paper login`
  is open) and restores them at the next start.
- A visible Chromium (153) crashes with SIGTRAP when a download starts if its history lists an earlier
  download. `_forget_downloads` clears the download tables of the profile's `History` before each launch.
- IEEE Xplore answers headless Chromium with HTTP 418, which counts as a bot check (visible retry). Its
  pages have no `citation_pdf_url`; the PDF is `/stampPDF/getPDF.jsp?tp=&arnumber=N`, with N from the
  `/document/N` URL.

## Next steps

- An MCP server so other agent apps can use the tool.
- `docs/literature-review-use-case.md`: missing routes (bioRxiv API, OSF preprints) and features
  an agent doing a literature review needed, from use on 10 October 2026.
- Fetching arXiv LaTeX source for maths-heavy papers.
