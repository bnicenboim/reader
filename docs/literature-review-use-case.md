# Notes from use in a literature review

Written 10 October 2026 after using `paper` from a Claude Code cloud session for
the literature review of a grant proposal (repository `gchrupala/warthog`). The
review keeps full texts in its own repository under bibliography keys, and an
agent reads them and checks claims against them.

What was done: 30 papers that the review's own scripts had failed to download
were run through `paper get --no-browser`. 10 succeeded; after the NCBI
E-utilities route was added, 16 did. The rest are below.

Items are ordered by how much they would have helped.

## Missing download routes

1. **bioRxiv and medRxiv API.** `www.biorxiv.org` answered with rate limiting
   on the first request. `https://api.biorxiv.org/details/biorxiv/<doi>`
   answers without trouble and gives the latest version, its date and a
   `jatsxml` URL. Use it as a repository candidate for DOIs with the prefixes
   `10.1101` and `10.64898`. Example: raviv2026, `10.64898/2026.03.28.715023`.
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

## Not needed for this use

- An MCP server: the CLI was enough for a coding agent.
- Citation-graph search: the review has its own OpenAlex and Semantic Scholar
  scripts for this. It could move into `paper` later (`paper cites`,
  `paper refs`), but it is a separate tool's job.
