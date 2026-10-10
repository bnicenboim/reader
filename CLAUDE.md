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
uv sync --all-extras         # install, including the Playwright browser extra
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
| `sources.py` | OpenAlex, Unpaywall, Semantic Scholar, arXiv, Europe PMC, ACL, OpenReview, Crossref search |
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
2. Open repositories: arXiv, ACL Anthology, OpenReview, PubMed Central full text via Europe PMC.
3. Open-access copies listed by OpenAlex, Unpaywall and Semantic Scholar.
   Versions of record come before preprints.
4. The publisher over plain HTTP (works on a campus network or VPN).
5. A real browser with a persistent profile that keeps the user's logins, for sites that refuse scripts.
6. The institution's proxy, in that browser.

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
- Do not add CAPTCHA solving, browser fingerprint spoofing, fake browser user agents, or
  shadow libraries such as Sci-Hub.

## Status

Verified on real papers: arXiv, ACL Anthology, PMLR, HAL (the green open-access copy of a
paywalled Nature paper) and PubMed Central. The browser's PDF download path works.
There are 56 unit tests, all passing.

Not verified:
- Access through an institutional proxy has never been run.
- ScienceDirect blocked the development server's IP, even in the browser.
- ACM and OpenReview show a Cloudflare check whose verification server was blocked by the
  development sandbox's network, so they failed there. They may work from a normal
  machine and network.

## Setup the user needs

`paper config --init` writes `~/.config/paperreader/config.toml`. Then:
- `email`: required by Unpaywall.
- `keys.openalex`: free. Without it, OpenAlex shares a small daily allowance with everyone
  on the same IP address; once it runs out, title search falls back to Crossref, which
  matches poorly (it did not find "Attention is all you need").
- `institution.proxy`: the library's proxy URL template, then `paper login` once to sign in.
  Tilburg University's proxy URL format is still unknown; ask the user rather than guessing.

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

## Next steps

- An MCP server so other agent apps can use the tool.
- Fetching arXiv LaTeX source for maths-heavy papers.
