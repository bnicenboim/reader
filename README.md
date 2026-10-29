# paperreader

paperreader (command `paper`): a command-line tool that fetches research papers you are entitled to read
and turns them into text that you or an AI agent can work with.

```console
$ paper get 10.18653/v1/2020.acl-main.1 arXiv:1706.03762 pmid:17375194
$ paper read 10.1038/nature14539 --pages 1-3
```

You give it a DOI, arXiv id, PMID/PMCID, ACL Anthology id, OpenReview id, OSF id, URL or exact title.
It finds a copy, downloads it into `~/papers`, and writes next to it a Markdown
text version (with `<!-- page N -->` markers for PDFs), a JSON metadata sidecar, and
a folder with an image of each figure and table, linked from the text where it appears.

## How it finds papers

It tries the cheapest and most polite route first and stops at the first that works:

1. **Your library.** Anything already downloaded is matched on *any* of its ids
   (a paper fetched by arXiv id is found again by DOI). No network needed.
2. **Open repositories reachable from the ids**: arXiv, ACL Anthology, OpenReview,
   and PubMed Central full-text XML through the Europe PMC API and NCBI E-utilities
   (which also serve author manuscripts). Preprint servers through their APIs:
   bioRxiv and medRxiv, and OSF (PsyArXiv, SocArXiv, ...), whose pages have no PDF link.
3. **Open-access copies** listed by OpenAlex, Unpaywall and Semantic Scholar
   (institutional repositories, HAL, publisher OA versions...). Versions of
   record are preferred over preprints. Landing pages are followed through their
   `citation_pdf_url` tag, as Google Scholar does.
4. **The publisher, over plain HTTP.** Works if your network has IP-based access
   (on campus, or on the VPN). Elsevier's API is used if you have a key.
5. **Your library's proxy** (EZproxy, OpenAthens redirector), in a real browser
   (Chromium through Playwright, with its own private profile).
6. **That browser for whatever refused plain HTTP.** It reuses sessions you
   established with `paper login`, for example "Access through your institution"
   on a publisher site.

`paper resolve <id>` shows the candidates without downloading anything.

## What the text looks like

The best source for reading is used where there is one:

- **arXiv papers** come from arXiv's HTML version (arxiv.org/html, or ar5iv for older
  papers): real section headings, maths as LaTeX (`$\mathrm{softmax}(QK^T)$`), and tables
  as Markdown tables. The PDF is downloaded too, for the figures.
- **PubMed Central and bioRxiv** papers come as JATS XML: headings, tables, captions.
- **Everything else** is text from the PDF, page by page, cleaned of cover pages and
  watermarks. Its headings are guessed for `paper read --sections`.

**Figures and tables are images**, so an agent that can see images can look at them:

- From PDFs, each figure and table is cropped together with its caption. When a caption's
  region is unclear, the whole page is rendered instead. Cropping uses PyMuPDF, an optional
  extra (`figures`), which is licensed AGPL-3.0. Without it, pages with figures or tables
  are rendered whole.
- For arXiv's HTML and bioRxiv's XML, the crops come from the paper's PDF and are matched
  by label ("Figure 3").
- For open-access PMC articles, the original images come from Europe PMC. For PMC author
  manuscripts they come from PMC's site, which the browser opens. Without the browser, the
  text says where to see them.

`paper read ID --figures` lists the images with their paths. `paper read` prints the text with
absolute image paths. `paper figures --all` adds figures to papers downloaded before
this existed, and cleans their text again.

## Install

```console
uv tool install 'paperreader[browser,figures] @ git+https://github.com/gchrupala/reader'
paper install-browser   # Chromium for the browser fallback; or set browser.channel = "chrome"
paper config --init     # writes ~/.config/paperreader/config.toml
```

or, from a checkout, `uv sync --all-extras` and run `uv run paper ...`.

Edit the config. The settings worth filling in:

- `email`: Unpaywall requires it; Crossref and OpenAlex give identified clients better service.
  It can also come from the `PAPERREADER_EMAIL` environment variable.
- `keys.openalex`: [free](https://help.openalex.org/api/authentication/). Without one,
  OpenAlex requests share a small daily budget with everyone on your IP address,
  and title search (10x the cost of a lookup) runs out first. Without OpenAlex,
  search falls back to Crossref. API keys can also be set as environment variables
  (`OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `ELSEVIER_API_KEY`, `ELSEVIER_INSTTOKEN`).
- `institution.proxy`: your library's proxy URL template, e.g.
  `https://login.ezproxy.example.edu/login?url={url}`. Your library's website
  documents it, usually as a "bookmarklet" or "proxy prefix".

## Institutional access

Run `paper login` once. A browser window opens on your proxy; sign in with your
institution's SSO, then close the window. The session is kept in a private browser
profile (`~/.local/state/paperreader/browser-profile`) and reused by later
downloads until it expires; run `paper login` again then.

For publishers that support "Access through your institution" (Shibboleth,
SeamlessAccess), `paper login https://dl.acm.org` (or any publisher site) lets you
sign in there instead.

To reuse the logins of your everyday Chrome instead, start it with
`--remote-debugging-port=9222` and set `browser.cdp_url = "http://localhost:9222"`.

## Bot walls

Many publisher sites sit behind Cloudflare or similar checks that reject scripts.
The tool deals with them like this:

- Plain HTTP requests identify themselves honestly (`paperreader/0.1 (+repo; mailto:you)`).
  Some publishers serve PDFs to that but block clients that pretend to be a browser.
- The browser fallback is a real browser. In the default `mode = "auto"` it runs
  headless, and when a site shows a human check it opens a visible window and waits
  for you to complete the check. The cleared session is then reused.
- A check you do not complete is not shown again: that site's other pages are skipped
  for the rest of the run, and for 12 hours (`check_memory_hours`) later runs open no
  window for it; its papers fail at once and go to `paper pending`. `paper get --visible`
  and `paper pending --retry` ask again. Closing the window or the tab during a check
  skips the paper. One paper waits for you at most 180 s in all (`paper_wait`).
- Cloudflare's error 1015 (a rate-limit ban, often given to scripts on the first request)
  is reported as "blocked by Cloudflare": waiting does not help, so it is not retried, and
  the URL is left for the browser.
- The browser leaves out the switches that tell sites it is automated (`navigator.webdriver`,
  the "controlled by automated test software" bar), since Cloudflare's checks did not pass
  while it showed them, even when completed by hand. It does not disguise itself further:
  headless, its user agent says HeadlessChrome. If a site's check still does not pass, try
  `browser.channel = "chrome"` (your installed Chrome) or `browser.cdp_url` (your everyday Chrome).
- It does not solve CAPTCHAs or use shadow libraries such as Sci-Hub. Both are allowed
  but not implemented.

## Politeness

- **Per-host spacing and budgets.** Every request, including each redirect hop and
  each page the browser opens, waits for a slot from a per-host budget: by default at
  least 5 s between requests to the same host, at most 60 per hour and 300 per day.
  APIs get the rates they publish (arXiv: one request per 3 s). The budget lives in
  a SQLite file, so parallel `paper` processes (as agents like to run) share it.
  Override per host in `[rate_limits]`; `paper usage` shows consumption.
- **This matters for institutional access.** Publishers watch for "systematic
  downloading" and can suspend access for a whole institution's IP range. The
  defaults keep you well within what a person reading papers would do; be careful
  raising them for publisher hosts.
- **`Retry-After` is honoured**, and 429s back off for the whole host across processes.
- **API answers are cached** for a week (misses included), and the headless browser
  skips images, fonts and media.

## Using it from an agent

The CLI is designed to be driven by a coding agent such as Claude Code. This repository is
also a Claude Code plugin marketplace with a `paperreader` skill (`plugin/skills/paperreader/SKILL.md`)
that tells Claude when and how to use the CLI, in every session:

```console
/plugin marketplace add gchrupala/reader
/plugin install paperreader@paperreader
```

The plugin does not install the CLI itself; do that as in [Install](#install).
Instead of the plugin, something like this in your `CLAUDE.md` is enough:

```markdown
To read a paper, run `paper get <DOI|arXiv id|URL|title>`; it prints the path of a
Markdown text version, which you can read (use `paper read <id> --section Methods`
or `--no-refs` for parts of long papers). Figures and tables are images linked from
the text; open them to look at them (`paper read <id> --figures` lists them).
`paper search <keywords>` finds DOIs.
If `get` fails, report its reason to me instead of looking for other copies.
```

`--json` on `get`, `resolve`, `search` and `list` gives machine-readable output,
including every attempt and hint when a download fails.

**Batches and your own keys.** A project that cites papers by its own keys can
fetch them in one go and get the texts under those keys:

```console
$ cat keys.tsv
roseberry2014	10.1111/cdev.12166
zellers2022	arXiv:2201.02639
$ paper get --from keys.tsv --export fulltext --name '{key}.txt'
ok    roseberry2014  ncbi-eutils
ok    zellers2022    arxiv, submittedVersion
```

The keys become the papers' keys in the library, so `paper read roseberry2014` and
`paper bib roseberry2014` work too. `paper bib` prints BibTeX from the metadata
(Crossref gives the volume, issue, pages, and the issue year that bibliographies cite).

**Without a person at the browser** (a cloud container, `--no-browser`), human checks
and sign-ins cannot be done. Every paper `get` could not fetch goes on a list,
`paper pending`, with the reason and the page to open. At your own machine,
`paper pending --retry` tries them again with a browser, and
`paper add FILE [ID]` registers a PDF you downloaded yourself.

**Checked downloads.** Before a download is stored, its first pages must show the
paper's DOI (or arXiv id or PMCID) or most of its title's words; otherwise it is rejected
("the download does not match the requested paper") and the next copy is tried. A URL
that another paper in the library was downloaded from is refused. `paper verify` runs
the same check over the library and reports mismatches; `--remove` moves them into
`mismatched/` so that `paper get` fetches them again.

**Reading parts of a paper.** `paper read ID --sections` lists the headings
(Markdown headings for JATS sources, guessed for PDFs), `--section Results` prints
one section (repeatable), and `--no-refs` leaves out the reference list. PDF text
is cleaned of HAL cover pages and "Downloaded from ..." watermarks.

### Claude Code on the web

Cloud sessions start in a fresh container, so the CLI and the plugin have to be installed
each time. In the environment's settings (the cloud environment menu in the session's
title bar, then Edit), put this in **Setup script**; it then runs before every session in
that environment, whatever the repository:

```bash
#!/bin/bash
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install 'paperreader[figures] @ git+https://github.com/gchrupala/reader'
claude plugin marketplace add gchrupala/reader
claude plugin install paperreader@paperreader
```

In the same settings:

- **Environment variables**: `OPENALEX_API_KEY` and `PAPERREADER_EMAIL`. The setup script
  cannot read them, but `paper` can when it runs.
- **Network access**: the default (limited) level blocks publishers and most of the APIs.
  Choose full access, or add the hosts you need under allowed domains.

A cloud session has no person at a browser and no campus network, so it gets open-access
papers only; run `paper get` with `--no-browser`. The library, `paper pending` included,
disappears with the container, so keep the texts with `--export DIR` in the repository you
are working on, and save the failures with `paper pending --tsv > missing.tsv` to fetch
them at your own machine with `paper get --from missing.tsv`.

## Commands

| command | |
| --- | --- |
| `paper get ID...` | download into the library (`--from FILE`, `--key`, `--export DIR`, `--force`, `--no-browser`, `--visible`) |
| `paper read ID` | print the text, fetching first if needed (`--pages 1-3,7`, `--sections`, `--section NAME`, `--no-refs`, `--figures`) |
| `paper figures ID...` | list a paper's figure and table images; extract them for papers stored earlier (`--all`, `--rebuild`) |
| `paper add FILE [ID]` | register a PDF you downloaded yourself, with its metadata |
| `paper bib ID...` | BibTeX entries |
| `paper pending` | papers that need a person (`--retry`, `--tsv`, `--json`, `--clear`) |
| `paper resolve ID` | metadata and candidate URLs, no download |
| `paper search WORDS` | find papers by title/keywords |
| `paper list` | the library |
| `paper verify [ID...]` | check that stored documents are the papers they are stored as (`--remove`, `--json`) |
| `paper login [URL]` | sign in, in the tool's browser profile |
| `paper usage` | requests per host against their budgets |
| `paper install-browser` | download Chromium for the browser fallback |
| `paper config [--init]` | show or create the configuration |

## Development

```console
uv sync --all-extras
uv run pytest
```

Tests use a mocked transport and never touch the network. `uv run pytest -m network`
runs the real papers from `docs/literature-review-use-case.md` against real servers.
