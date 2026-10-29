# CLAUDE.md

## What this is

`paper` (package `paperreader`, in `src/paperreader/`) is a command-line tool that
downloads a research paper from a DOI, arXiv id, PMID/PMCID, ACL Anthology id,
OpenReview id, OSF id, URL, exact title or library key. It stores the paper in the library
(`~/papers` by default): the document (PDF, JATS XML, or arXiv's HTML plus its PDF), a Markdown
text version (with `<!-- page N -->` markers for PDFs), a JSON metadata sidecar, and a
`<key>.figures/` folder with an image of each figure and table, linked from the text.

Goal: if a paper is open access or the user has institutional access to it, an
agent should be able to fetch and read it without the user downloading it by
hand, and without putting unreasonable load on any server.

## Commands

```console
curl -LsSf https://astral.sh/uv/install.sh | sh   # only if uv is missing; installs to ~/.local/bin
uv sync --all-extras         # install, with the extras: browser (Playwright) and figures (PyMuPDF, AGPL)
uv run paper install-browser # Chromium for the browser route, once per machine (~/.cache/ms-playwright)
uv run pytest                # unit tests; mocked transport, never touch the network
uvx ruff check src tests     # lint (line length 120)
uv run paper --help
```

Tests marked `network` hit real servers and are deselected by default; `uv run pytest -m network`
runs the real papers from `docs/literature-review-use-case.md` (no browser, your config, a temporary library).

## Layout

| module | role |
| --- | --- |
| `identifiers.py` | parse user input into a normalized `Identifier` |
| `config.py` | TOML config, per-host `Limit`s, config template |
| `ratelimit.py` | per-host spacing plus hourly/daily caps, stored in SQLite and shared across processes |
| `http.py` | `PoliteClient`: rate-limits each redirect hop, honours `Retry-After`, caches API answers |
| `sources.py` | OpenAlex, Unpaywall, Semantic Scholar, arXiv, Europe PMC, NCBI E-utilities, ACL, OpenReview, bioRxiv/medRxiv, OSF, Crossref (search and publication details) |
| `publishers.py` | PDF URL templates by DOI prefix, Elsevier API, institutional proxy URL rewriting |
| `html.py` | landing-page metadata (`citation_pdf_url`, meta refresh), bot-wall, sign-in and Cloudflare-error detection |
| `browser.py` | Playwright with a persistent profile, `login`, human-check handling |
| `fetch.py` | the pipeline: candidate order, attempts, browser retries, unfinished human checks, failure reason and hints |
| `verify.py` | is a document the requested paper: DOI, arXiv id or PMCID, or most title words, on its first pages |
| `extract.py` | PDF to text (pypdfium2; drops HAL cover pages and watermarks, joins exponents) and JATS to Markdown |
| `figures.py` | figure and table images: caption-anchored crops (PyMuPDF), whole-page renders (pypdfium2), markers |
| `html_paper.py` | arXiv's LaTeXML HTML to Markdown (headings, `$LaTeX$`, tables, figure markers) |
| `sections.py` | headings of the text (Markdown, or guessed for PDFs), one section, text without references |
| `library.py` | storage, readable or user-chosen keys (`vaswani2017-attention-all-you-need`), lookup by any id or key |
| `pending.py` | papers that need a person, kept in the library as `pending.json` |
| `bibtex.py` | BibTeX entries from the stored metadata |
| `cli.py` | `get`, `read`, `figures`, `add`, `bib`, `pending`, `resolve`, `search`, `list`, `verify`, `login`, `usage`, `install-browser`, `install-skill`, `config` |

`src/paperreader/SKILL.md` is a Claude Code skill that tells agents how to use the CLI; `paper install-skill`
copies it to `~/.claude/skills/paperreader/`. Keep it in step with the commands.

## Fetch order

Cheapest and most polite first; stop at the first route that works:

1. The library, matched on any id (a paper fetched by arXiv id is found again by DOI).
2. Open repositories: arXiv (its HTML version first, from arxiv.org or ar5iv, then the PDF), ACL
   Anthology, OpenReview, PubMed Central full text via Europe PMC and NCBI E-utilities. Preprint servers through their APIs: bioRxiv/medRxiv (by DOI) and OSF
   (ids found in OpenAlex, Unpaywall or Semantic Scholar locations, or given directly).
3. Open-access copies listed by OpenAlex, Unpaywall and Semantic Scholar.
   Versions of record come before preprints.
4. The publisher over plain HTTP (works on a campus network or VPN).
5. The institution's proxy, in a real browser with a persistent profile that keeps the user's logins.
6. That browser for sites that refused scripts. It comes after the proxy because a paywalled publisher
   page fails there anyway, sometimes after a human check that waits for the user.

Lookups are lazy: Unpaywall and Semantic Scholar are only called if the earlier candidates fail.

After the text is found, figure images: crops from the PDF (for arXiv HTML and bioRxiv XML, from
the paper's PDF, named as the candidate's `companion`), Europe PMC's zip for open-access PMC
articles, or PMC's article page in the browser for author manuscripts (images then from NCBI's CDN).

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
  the session is reused afterwards. A check nobody completes is not shown again for that site: not
  for the rest of the run, and not for `browser.check_memory_hours` (12) in later runs unless they are
  `--visible` or `paper pending --retry`. One paper waits for a person at most `browser.paper_wait`
  (180 s). Closing the window or tab during a check skips the paper.
- Automated CAPTCHA solving is allowed (the user approved it) but not implemented yet. If
  added, it must go through the same per-host rate limits as everything else.
- Browser fingerprint spoofing and fake user agents are allowed but not implemented. The
  browser is only launched without automation markers (`UNMARKED_ARGS`), which is neither a
  spoofed fingerprint nor a fake user agent; headless, it still reports HeadlessChrome.
- Shadow libraries such as Sci-Hub are allowed but not implemented.

Storage:
- Every download is checked against the requested paper before it is stored (`verify.check`): its first
  pages show the DOI, arXiv id or PMCID, or most of the title's words. A URL that is already the source of
  another paper in the library is refused. `paper verify` runs the same check over the library.

## Status

Verified on real papers: arXiv, ACL Anthology, PMLR, HAL (the green open-access copy of a
paywalled Nature paper) and PubMed Central. The browser's PDF download path works.
The institutional proxy works: on 10 October 2026 an IEEE paper came through Tilburg's EZproxy
after `paper login`, with the saved session and a visible window.
Figures and tables were checked by eye on about 40 papers in the user's library (PNAS, IEEE, Royal
Society, Cognitive Science, CVPR-style arXiv, APA manuscripts, PMC author manuscripts): crops are
complete; a few pages with unusual captions are rendered whole.
There are 145 unit tests, all passing, and 24 network tests.

The handling of human checks (issue #4) was tested with a fake browser in the unit tests, and with real
headless Chromium against a local server (check not completed, mark remembered by the next process,
tab or browser closed during the wait). It has not been tried on a real site with a visible window.
On 10 October 2026, from the user's desktop: with the browser launched without automation switches (issue #9),
the checks at www.science.org, pubs.aip.org, cell.com, www.sciencedirect.com and research.tilburguniversity.edu
were all passed in the visible window, where before none was. Pages see `navigator.webdriver` false, headless and
visible. The same day doi.org was skipped for Science in a later run, with no window (issue #7), and PDFs came
through the proxy from Annual Reviews and from ScienceDirect (three papers: headless refused, then the visible
window).
The download check (issue #5) finds no mismatch among the 39 papers in the library of 10 October 2026,
and finds the one known wrong entry.

Not verified:
- The proxy route for publishers other than IEEE, Annual Reviews and ScienceDirect (AIP reached the article
  page through it).
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
- www.biorxiv.org answers scripts with HTTP 429 and Cloudflare's error 1015 (a ban, not a slow-down):
  `http.CloudflareBlocked`, not retried; the run skips plain HTTP to that host and queues its URLs for the
  browser. api.biorxiv.org answers fine and lists every version with a `jatsxml` URL. bioRxiv DOIs are
  10.1101/... and, since 2025, 10.64898/...
- OSF preprint pages (osf.io, psyarxiv.com) are JavaScript without a PDF link. `api.osf.io/v2/guids/<id>/`
  redirects to the preprint; its `primary_file` gives the file, whose `links.download` is the PDF.
- OpenAlex's `publication_year` is often the online year; bibliographies cite the issue year. Crossref's
  `published-print` gives it, so the Crossref lookup after a download replaces the year (not for
  `posted-content`). It also gives volume, issue and pages for `paper bib`.
- Europe PMC's rendered PDF (`europepmc.org/articles/PMC…?pdf=render`) refuses scripts with 403; it is a
  candidate anyway, so it lands in the browser queue for papers whose publisher refuses XML (PNAS).
  Headless, it shows Cloudflare's "Just a moment..."; in the visible window that follows, the check
  cleared by itself and the PDF arrived (from `europepmc.org/api/getPdf`).
- pypdfium2 puts superscripts on their own line ("p = 3.2e" / "−6"); `extract._clean` joins them.
- On 10 October 2026 a browser download stored Lisman & Grace 2005 (fetched 3 minutes earlier by another
  process) under Versace et al. 2018; the sidecar's URL was the Lisman PDF's. The cause is unknown. Ruled
  out on a local server with headless Chromium: Playwright gives a page only the downloads its own frames
  or its popups start, not those of other tabs; after a process is killed mid-download or with a tab
  open, the next start neither restores the tab nor requests the file again. Not tested: a visible
  window's session restore, and the Elsevier/Cell.com pages themselves (a site redirect or script that
  sends the page to the last PDF would explain it). Hence the content check before storing.
- Title words alone barely tell papers of one field apart: on the first two pages of the library's 39
  papers, other papers' titles reached 4 of 5 and 6 of 8 of their distinctive words, while every paper had
  all of its own. The check needs 85% of the words (all of them up to six) within about twice the title's
  length. The DOI settles most cases before that: 37 of the 39 print it on the first two pages.
- Cloudflare checks shown to the user in the automated Chromium were never passed (Science, AIP and Pure,
  10 October 2026, issue #9), even when completed. Launched without `--enable-automation` and with
  `--disable-blink-features=AutomationControlled` (`browser.UNMARKED_ARGS`), so that `navigator.webdriver` is
  false, the browser passed them all the same evening. If a site still refuses: the installed Chrome
  (`browser.channel = "chrome"`), the user's own Chrome (`browser.cdp_url`), then a patched driver.
- ScienceDirect answers headless Chromium (user agent HeadlessChrome) with "There was a problem providing the
  content you requested" (HTTP 403, some 770 kB into the page), also through the proxy. Like IEEE's 418 it
  counts as a bot check (`html.refuses_headless`), and the visible window gets the article page. That page has
  no `citation_pdf_url`; the PDF link is built from its `"pdfDownload"` data (`/pdfft?md5=..&pid=..`) and
  redirects to a signed pdf.sciencedirectassets.com URL that expires after 300 s.
- Annual Reviews has no `citation_pdf_url`; its PDF is the action of a POST form in the page body
  (`/deliver/fulltext/...pdf?...mimeType=pdf`, `html.DELIVER_FORM_RE`), and a GET of it returns the PDF too.
  Its PDFs carry "Downloaded from www.annualreviews.org. <institution> IP: ..." on every page.
- dspace.mit.edu (CloudFront) answers scripts with AWS WAF's CAPTCHA: HTTP 405, title "Human Verification".
  A check page with any error status is now left for the browser.
- Through the proxy, Science's page for a 1977 article offers "Check Access" and "Purchase", and its PDF URL
  returns that page: Tilburg seems to lack the archive. 10.1121/10.0040820 is an ASA meeting abstract in a
  JASA supplement, with no PDF.
- Twice on 10 October 2026 a `paper get` hung for good in its first browser page (a proxied Elsevier DOI,
  headless): Python waited on a Playwright call that never returned while the browser sat idle, with no
  download started. It did not happen in 8 repeats of that page alone, nor against a local server with a
  meta refresh into a large 403 page; the cause is not known. Since then a page load that runs past
  2 x `timeout` + the wait for a person + 30 s ends the browser (its pid comes from the profile's
  `SingletonLock` link; not on Windows, and never with `cdp_url`), and fails as `BrowserHung`.
- The browser's log of a human check names the host where the page ended up (www.science.org), not the
  requested one (doi.org); unfinished checks are kept per site in `ratelimit.sqlite` (table `checks`).
  A page that leads to such a site is skipped too (issue #7): where plain HTTP ended up after redirects
  tells within the run, and the URLs that led to the check in the browser are kept (table `check_urls`).
- PyMuPDF's layout model (`pymupdf-layout`, `pymupdf4llm`) found captions but no pictures at all in
  PNAS and Royal Society PDFs, and took 30 s on one paper; its Markdown split words and turned margin
  text into tables. So figures are cropped by geometry instead: from each caption, grow over graphics,
  labels and table cells until body text (body font size, lines of 30+ characters) or another caption;
  figures upwards first, tables downwards. Running heads are found by repetition across pages.
- arXiv's HTML lacks some figure images (PDF figures LaTeXML could not convert, e.g. Figure 1 of
  2201.02639), hence the crops from the PDF. Its image paths are relative to `/html/`.
- PMC's article pages show a proof-of-work check (`__identity_pow`) that a headless browser solves by
  itself when it waits; the images are on cdn.ncbi.nlm.nih.gov, open to plain HTTP. Author manuscripts
  in PMC's open-data bucket and Europe PMC's `supplementaryFiles` have no images (license).
- IEEE PDFs fetched through the proxy carry "Authorized licensed use limited to: ..." on every page.

## Next steps

- An MCP server so other agent apps can use the tool.
- `docs/literature-review-use-case.md` lists what a literature-review agent needed (10 October 2026);
  its items are implemented. Not verified: bioRxiv full text through the browser after a Cloudflare 1015
  (from the user's network plain HTTP got through), and section guessing on more PDF layouts.
- Fetching arXiv LaTeX source for maths-heavy papers.
