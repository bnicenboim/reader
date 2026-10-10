# reader

`paper`: a command-line tool that fetches research papers you are entitled to read
and turns them into text that you or an AI agent can work with.

```console
$ paper get 10.18653/v1/2020.acl-main.1 arXiv:1706.03762 pmid:17375194
$ paper read 10.1038/nature14539 --pages 1-3
```

You give it a DOI, arXiv id, PMID/PMCID, ACL Anthology id, OpenReview id or URL.
It finds a copy, downloads it into `~/papers`, and writes next to it a Markdown
text version (with `<!-- page N -->` markers) and a JSON metadata sidecar.

## How it finds papers

It tries the cheapest and most polite route first and stops at the first that works:

1. **Your library.** Anything already downloaded is matched on *any* of its ids
   (a paper fetched by arXiv id is found again by DOI). No network needed.
2. **Open repositories reachable from the ids**: arXiv, ACL Anthology, OpenReview,
   and PubMed Central full-text XML through the Europe PMC API.
3. **Open-access copies** listed by OpenAlex, Unpaywall and Semantic Scholar
   (institutional repositories, HAL, publisher OA versions...). Versions of
   record are preferred over preprints. Landing pages are followed through their
   `citation_pdf_url` tag, as Google Scholar does.
4. **The publisher, over plain HTTP.** Works if your network has IP-based access
   (on campus, or on the VPN). Elsevier's API is used if you have a key.
5. **A real browser** (Chromium through Playwright, with its own private profile)
   for whatever refused plain HTTP. It reuses sessions you established with
   `paper login`, for example "Access through your institution" on a publisher site.
6. **Your library's proxy** (EZproxy, OpenAthens redirector), in that browser.

`paper resolve <id>` shows the candidates without downloading anything.

## Install

```console
uv tool install 'paperreader[browser] @ git+https://github.com/gchrupala/reader'
paper install-browser   # Chromium for the browser fallback; or set browser.channel = "chrome"
paper config --init     # writes ~/.config/paperreader/config.toml
```

or, from a checkout, `uv sync --all-extras` and run `uv run paper ...`.

Edit the config. The settings worth filling in:

- `email`: Unpaywall requires it; Crossref and OpenAlex give identified clients better service.
- `keys.openalex`: [free](https://help.openalex.org/api/authentication/). Without one,
  OpenAlex requests share a small daily budget with everyone on your IP address,
  and title search (10x the cost of a lookup) runs out first. Without OpenAlex,
  search falls back to Crossref.
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
The tool deals with them in plain ways only:

- Plain HTTP requests identify themselves honestly (`paperreader/0.1 (+repo; mailto:you)`).
  Some publishers serve PDFs to that but block clients that pretend to be a browser.
- The browser fallback is a real browser. In the default `mode = "auto"` it runs
  headless, and when a site shows a human check it opens a visible window and waits
  for you to complete the check. The cleared session is then reused.
- It does not solve CAPTCHAs, spoof browser fingerprints or use shadow libraries.

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

The CLI is designed to be driven by a coding agent such as Claude Code. Something
like this in your `CLAUDE.md` is enough:

```markdown
To read a paper, run `paper get <DOI|arXiv id|URL|title>`; it prints the path of a
Markdown text version, which you can read (use `paper read <id> --pages 3-5` for
parts of long papers). `paper search <keywords>` finds DOIs. If `get` fails, report
its hints to me instead of looking for other copies of the paper.
```

`--json` on `get`, `resolve`, `search` and `list` gives machine-readable output,
including every attempt and hint when a download fails.

## Commands

| command | |
| --- | --- |
| `paper get ID...` | download into the library (`--force` to refetch, `--no-browser`, `--visible`) |
| `paper read ID` | print the text, fetching first if needed (`--pages 1-3,7`) |
| `paper resolve ID` | metadata and candidate URLs, no download |
| `paper search WORDS` | find papers by title/keywords |
| `paper list` | the library |
| `paper login [URL]` | sign in, in the tool's browser profile |
| `paper usage` | requests per host against their budgets |
| `paper install-browser` | download Chromium for the browser fallback |
| `paper config [--init]` | show or create the configuration |

## Development

```console
uv sync --all-extras
uv run pytest
```

Tests use a mocked transport and never touch the network.
