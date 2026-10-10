# Notes from use in a literature review

Written 10 October 2026 after using `paper` from a Claude Code cloud session for
the literature review of a grant proposal (repository `gchrupala/warthog`). The
review keeps full texts in its own repository under bibliography keys, and an
agent reads them and checks claims against them.

What was done: 30 papers that the review's own scripts had failed to download
were run through `paper get --no-browser`. 10 succeeded; after the NCBI
E-utilities route was added, 16 did. The rest are below.

Items are ordered by how much they would have helped.

## Status

All twelve items were implemented on 10 October 2026. The test cases below are in
`tests/test_network.py` (`uv run pytest -m network`); all 21 pass from a desktop on the
Tilburg University network, without the browser.

1. bioRxiv/medRxiv: `sources.biorxiv` uses the API for the latest version's JATS XML and
   PDF. Cloudflare's 1015 is reported as "blocked by Cloudflare at <host>", is not retried,
   and leaves the host's URLs to the browser. From the desktop, plain HTTP got the XML.
2. OSF: links from OpenAlex, Unpaywall and Semantic Scholar become an OSF id; the OSF API
   gives the file. `osf:<id>` and OSF URLs are identifiers too.
3. PMC without XML: Europe PMC's rendered PDF is a candidate; it refuses plain HTTP and
   goes to the browser, where the PDF arrived after a Cloudflare check cleared by itself.
   With the library proxy configured, the proxy is tried first and got Lytle 2018 from PNAS.
4. `paper pending` lists every paper `get` could not fetch, with the reason and the page to
   open; `--retry`, `--tsv`, `--json`, `--clear`.
5. `paper add FILE [ID]`; without an id, the first DOI or arXiv id on the first pages.
6. `PAPERREADER_EMAIL`.
7. `paper get --key KEY`, keys per line in `--from`, and `--export DIR --name '{key}.txt'`.
   Library keys work wherever an identifier does. The year now comes from Crossref's issue
   date (roseberry2014).
8. `paper get --from FILE` (`key<TAB>identifier` or `identifier` per line, `-` for stdin)
   ends with one line per paper.
9. `paper bib ID...`, with type, volume, issue and pages from Crossref.
10. HAL cover pages are dropped (the page marker stays) and "Downloaded from ..." watermark
    lines removed.
11. `paper read --sections`, `--section NAME` (repeatable) and `--no-refs`. PDF headings are
    guessed: numbered headings in sequence, Roman numerals, the usual section names.
12. Exponents split onto their own line are joined: "p = 3.2e−6".

Items 13 and 14 (GitHub issues #4 and #5) were addressed later on 10 October 2026:

13. A human check that is shown and not completed marks its site. The paper's other pages
    on that site are skipped, later papers in the same run open no window for it, and for
    12 hours (`browser.check_memory_hours`) later runs that are not `--visible` skip it too;
    the paper fails at once and goes to `paper pending`. `--visible` and `paper pending
    --retry` ask again, and a passed check clears the mark. One paper waits for a person at
    most 180 s (`browser.paper_wait`). Closing the window or the tab during a check skips
    the paper; before, it counted as a failed page and the next page opened another check.
    The log names the site where the check is shown (www.science.org, not doi.org). Tested
    with a fake browser and with headless Chromium on a local server, not on Science itself.
    Not checked: why the checks did not pass. The "page without a PDF link" notes now give
    the page's title, so the next run shows whether those pages were unrecognised checks.
14. Before a download is stored, its first two pages must show the DOI (or arXiv id or
    PMCID) or 85% of the title's distinctive words close together; otherwise it is rejected
    with "the download does not match the requested paper" and the next candidate is tried.
    A URL that is the source of another library entry is refused. `paper verify` checks the
    library (read-only unless `--remove`); on the library of 10 October 2026 it finds the
    Versace entry and nothing else. The cause of the wrong download was not found. With
    headless Chromium on a local server: a page receives only downloads started by its own
    frames or its popups; a process killed mid-download or with a tab open leaves no tab to
    restore and no download that the next start resumes. A visible window's session restore
    and the Elsevier and Cell.com pages themselves were not tested.

## Missing download routes

1. **bioRxiv and medRxiv.** `www.biorxiv.org` answered with rate limiting
   on the first request. `https://api.biorxiv.org/details/biorxiv/<doi>`
   answers without trouble and gives the latest version, its date and a
   `jatsxml` URL. That URL is on `www.biorxiv.org` too, though, and returned
   HTTP 429 with Cloudflare error 1015 to a single request. Europe PMC lists the
   preprint (PPR1213252) without full text. So the API helps with metadata and
   versions, but the full text probably needs the browser route. A 429 carrying
   error 1015 should be reported as "blocked by Cloudflare", not as ordinary
   rate limiting, because waiting does not fix it. Example: raviv2026,
   `10.64898/2026.03.28.715023`.
2. **OSF preprints (PsyArXiv, SocArXiv and others).** OpenAlex and Semantic
   Scholar give `osf.io/<id>` or `psyarxiv.com/<id>/download` links. These are
   JavaScript pages, so the tool reports "page without a PDF link".
   `https://api.osf.io/v2/preprints/<id>/` gives the primary file, whose
   download link is the PDF. Examples: casey2023 (`osf.io/xbyr3`) and frank2023
   (`psyarxiv.com/qzbgx`).
3. **PMC articles whose publisher refuses XML** (PNAS, for one). Both
   E-utilities and Europe PMC return the front matter only, and Europe PMC's
   rendered PDF (`europepmc.org/articles/PMC…?pdf=render`) returns 403 to
   scripts. These are free to read, so a browser route to the PMC or Europe PMC
   PDF is the remaining option. That route is untested from a server. Examples:
   cartmill2013, kuhl2003, lytle2018.

## Working without a person at the browser

4. **A mode for headless agents.** In a cloud container there is no display, so
   human checks cannot be completed and institutional sign-in is impossible.
   What helps instead is a list of what needs a person, kept until the person
   is at their own machine. For example, `paper get` could add failures to a
   queue, and `paper pending` would list them with the reason and the URL to
   open.
5. **`paper add FILE --doi ID`.** Registers a PDF the user downloaded by hand:
   it extracts the text and fetches the metadata like `get` does. The review
   currently does this with `pdftotext` and loses the metadata header and the
   page markers.
6. **Email from the environment.** API keys can come from environment
   variables but `email` cannot, so Unpaywall is skipped in a fresh container.
   A `PAPERREADER_EMAIL` variable would fix this, set as a secret of the cloud
   environment. The config, cache and rate-limit database live under the home
   directory and are lost when the container is reclaimed. The design allows
   for this, but it means that published rate limits are the only protection
   across sessions.

## Fitting the output into a project

7. **Choosing the key and the location.** The review stores texts as
   `fulltext/<bibkey>.txt`, and the library's keys
   (`roseberry2013-skype-me-socially-contingent`) had to be mapped by hand.
   Options: `paper get ID --key roseberry2014`, or
   `paper export ID... --dir fulltext --name {key}.txt`. The year also differs
   between the two keys: OpenAlex gives the year of online publication (2013),
   while the bibliography uses the issue year (2014).
8. **Batch input with a summary.** Something like `paper get --from keys.tsv`,
   where each line is a key and an id, followed by a one-line result per paper
   (ok, the source and version, or the failure reason). `--json` covers part of
   this, but a short table is what an agent reports back to the user.
9. **BibTeX.** `paper bib ID` would print an entry from the metadata already
   collected (Crossref for pages, venue and DOI). This review looked up
   zellers2022 on Crossref by hand to add it to the bibliography.

## Text quality

10. **The HAL cover page.** Text from HAL PDFs begins with the archive's cover
    page, including its French and English disclaimer. Drop the first page when
    it matches HAL's template.
11. **Reading parts of a paper.** Agents mostly need the abstract, methods,
    results and limitations, not the references. JATS output already has
    `##` headings. For PDFs, a heuristic for numbered or capitalised section
    headings would allow `paper read ID --sections` (a list of headings) and
    `paper read ID --section Results`. A `--no-refs` flag alone would already
    save tokens: the review cut references with `awk`.
12. **Superscripts in PDF text.** Exponents are split onto their own lines
    (`p = 3.2e` / `−6`). This is minor and only matters when numbers are
    quoted.

## Running with a person at the browser

Observed on 10 October 2026 in a local session with a display, `mode = "auto"`
and the institutional proxy configured. An agent ran one `paper get --json ID`
per paper over a list of 50 papers, in sequence.

13. **Repeated human checks for one paper.** For Marler & Peters 1977
    (`10.1126/science.198.4316.519`) the user saw many Cloudflare challenges
    and none of them led to the paper. The log:

    ```text
    blocked  direct  publisher    https://www.science.org/doi/pdf/10.1126/science.198.4316.519  (HTTP 403)
    blocked  direct  publisher    https://doi.org/10.1126/science.198.4316.519  (HTTP 403)
    no-pdf   browser institution  <proxy>?url=https://www.science.org/doi/pdf/10.1126/science.198.4316.519  (page without a PDF link)
    no-pdf   browser institution  <proxy>?url=https://doi.org/10.1126/science.198.4316.519  (page without a PDF link)
    no-pdf   browser publisher    https://www.science.org/doi/pdf/10.1126/science.198.4316.519  (page without a PDF link)
    complete the human check of doi.org in the browser window (waiting up to 120 s)
    blocked  browser publisher    https://doi.org/10.1126/science.198.4316.519  (bot check)
    ```

    In `Fetcher._fetch`, the first challenge sets `self.visible = True` for the
    rest of the run. From then on every browser candidate waits up to
    `browser_challenge_wait` (120 s) for a check. Nothing records that a check
    on a host was shown and not passed, so the next candidate on the same site
    shows a new one. Not checked: whether Cloudflare keeps re-challenging
    because it recognises the automated Chromium (which would explain why
    none passed), and whether the three "page without a PDF link" pages were
    challenges that `looks_like_challenge` did not recognise.

    What would have helped:
    - After one check that is not passed, mark the host as needing a person
      and skip its remaining candidates. Keep the mark for a while in the
      state database, so the next `paper get` does not ask again.
    - A cap on the total time one paper may wait for a person.
    - Closing the window should mean "skip this paper", not "wait out the
      timeout".
    - In a batch, ask at most once, or collect the papers that need a check
      and list them at the end (see item 4).

    Workaround used: `--headless`. Bot checks then fail at once, and the hint
    says to rerun with `--visible`.

14. **A PDF saved under the wrong paper.** The library entry for Versace et
    al. 2018 (`10.1016/j.tics.2018.07.005`, saved at 12:06:36Z) contains the
    PDF of Lisman & Grace 2005 (`10.1016/j.neuron.2005.05.002`). Its sidecar
    gives the source as `publisher`, via the browser, with the URL
    `https://www.cell.com/neuron/pdf/S0896-6273(05)00397-1.pdf`. That is the
    Lisman PDF, which a separate `paper get` process had downloaded through
    the browser three minutes earlier (12:03:25Z, source `openalex`). The
    Versace process was killed by the agent about a minute after the save.
    The cause was not determined. Both processes used the same persistent
    browser profile, and the download handler accepts the first download
    event on the page. Because the wrong file is now in the library, later
    `paper get 10.1016/j.tics.2018.07.005` calls return it without any
    network request.

    What would have helped:
    - Before saving, check that the first page of the text contains the DOI
      or most of the title words. If not, reject the file with "the download
      does not match the requested paper" and try the next candidate.
    - Refuse a download whose URL is already the source of another library
      entry.
    - A `paper verify` command that runs the same check over the library.

    A check over the 15 papers downloaded in this run found only this one
    mismatch. For PubMed Central texts the DOI is often missing from the body,
    so the title words are the more useful test.

## Test cases

Observed between 9 and 10 October 2026 from a cloud container, with no email
or API keys configured and `--no-browser`. Open-access copies move, so these
belong in `network`-marked tests. The expected source is what worked, or would
work, at the time.

Regressions: these work now.

| id | paper | expected |
| --- | --- | --- |
| `10.1177/0956797613488145` | Weisleder & Fernald 2013 | `ncbi-eutils`, PMC5510534 (author manuscript; Europe PMC returns 500) |
| `10.1111/desc.13515` | Kosie & Lew-Williams 2024 | `ncbi-eutils`, PMC11333185 |
| `10.1162/opmi_a_00172` | Nencheva et al. 2024 | `europepmc` JATS, PMC11627589; text has `##` section headings |
| `10.1111/desc.70101` | Bazhydai et al. 2025 | `europepmc` JATS, PMC12662157 |
| `10.1016/j.cognition.2021.104779` | Tsuji et al. 2021 | Elsevier refuses (403); OpenAlex gives the HAL copy `hal-03373586` |
| `10.1109/tmm.2003.811618` | Roy 2003 | IEEE refuses; Semantic Scholar gives the author's MIT page |
| `10.3390/languages11050099` | Ashrafzadeh et al. 2026 | the Swansea repository copy, via OpenAlex |
| `arXiv:2201.02639` | Zellers et al. 2022 | arXiv |

New routes (items 1-3):

| id | paper | now | expected after the change |
| --- | --- | --- | --- |
| `10.1037/dev0001630` | Casey et al. 2023 | OpenAlex `osf.io/xbyr3`: page without a PDF link | OSF API: preprint `xbyr3`, then file `619c91bfa83c2000b70e78c2`, PDF |
| `10.1016/j.tics.2023.08.007` | Frank 2023 | Semantic Scholar `psyarxiv.com/qzbgx/download`: page without a PDF link | OSF API: preprint `qzbgx`, then `osf.io/download/cdyzf/` (checked: 200, 68 kB) |
| `10.64898/2026.03.28.715023` | Raviv et al. 2026 | "www.biorxiv.org is rate limiting us" | API metadata (version 2); full text fails with a "blocked by Cloudflare" message |
| `10.1073/pnas.1611621115` | Lytle et al. 2018 | efetch "XML without the full text"; no PMC id from OpenAlex `ids`, only from the location URL | still fails without a browser; the PMC id PMC6176610 must be found from the location URL (fixed in this branch) |
| `10.1073/pnas.1532872100` | Kuhl et al. 2003 | as above, PMC166444 | as above |

Text quality (items 10-12):

| id | check |
| --- | --- |
| `10.1111/cogs.12946` (Ludusan et al. 2021, HAL `hal-03080098v1`) | text starts with the HAL cover page ("HAL Id", "HAL Authorization"); the exponent in "p = 3.2e" / "−6" is split onto two lines (results of Experiment 1) |
| `10.1098/rstb.2024.0374` (Nikolaus & Fourtassi 2026, HAL) | publisher PDF on HAL: the cover page, plus the line "Downloaded from http://royalsocietypublishing.org/... by guest" on every page |
| `10.1111/cdev.12166` (Roseberry et al.) | the key says 2013 (online), the issue year is 2014 (*Child Development* 85(3)) |

Still unsolved without a person (item 4): `10.1121/10.0040820` (AIP, 403),
`10.1016/j.cub.2024.12.052` (Elsevier, 403), `10.1109/icassp49357.2023.10096250`
(IEEE, page without a PDF link), `10.1073/pnas.1419773112` (PNAS, 403).

## Not needed for this use

- An MCP server: the CLI was enough for a coding agent.
- Citation-graph search: the review has its own OpenAlex and Semantic Scholar
  scripts for this. It could move into `paper` later (`paper cites`,
  `paper refs`), but it is a separate tool's job.
