"""Command line interface: `paper get`, `paper read`, ..."""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

import click

from . import config as config_mod
from . import sections
from .bibtex import bibtex
from .browser import BrowserUnavailable
from .browser import login as browser_login
from .extract import is_pdf
from .fetch import Fetcher, FetchFailed
from .http import Cache, HTTPError, PoliteClient, RateLimited
from .identifiers import DOI_RE, UnrecognizedIdentifier, normalize_doi, parse
from .library import Library, Record
from .models import Paper
from .pending import Pending
from .publishers import proxied
from .ratelimit import QuotaExceeded, RateLimiter
from .sources import crossref_search, hit_identifier, openalex_search
from .verify import check_record


class App:
    def __init__(self, config_path: Path | None):
        self.config = config_mod.load(config_path)
        self._client: PoliteClient | None = None

    @property
    def client(self) -> PoliteClient:
        if self._client is None:
            state = self.config.state_dir
            limiter = RateLimiter(state / "ratelimit.sqlite", self.config.limit_for)
            self._client = PoliteClient(self.config, limiter, Cache(state / "cache.sqlite"))
        return self._client

    @property
    def library(self) -> Library:
        return Library(self.config.library_dir)

    @property
    def pending(self) -> Pending:
        return Pending(self.config.library_dir / "pending.json")


def _err(msg: str) -> None:
    click.echo(msg, err=True)


def _similar(a: str, b: str) -> float:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def _search(app: App, query: str, n: int) -> list[dict]:
    """OpenAlex, or Crossref when OpenAlex's budget is used up."""
    try:
        return openalex_search(app.client, app.config, query, n=n)
    except (RateLimited, QuotaExceeded, HTTPError) as e:
        _err(f"OpenAlex search unavailable ({e}); using Crossref")
        return crossref_search(app.client, app.config, query, n=n)


def _identify(app: App, text: str):
    """An identifier, a key in the library, or for free text the search hit whose title matches it closely."""
    if rec := app.library.by_key(text.strip()):
        return parse(rec.paper.ids()[0])
    try:
        return parse(text)
    except UnrecognizedIdentifier:
        pass
    hits = _search(app, text, n=5)
    best = next((h for h in hits if hit_identifier(h) and _similar(h["title"] or "", text) >= 0.9), None)
    if best:
        _err(f"matched title: {best['title']} ({best['year']}) {hit_identifier(best)}")
        return parse(hit_identifier(best))
    listing = "\n".join(f"  {hit_identifier(h)}  {h['title']} ({h['year']})" for h in hits if hit_identifier(h))
    raise click.UsageError(
        f"{text!r} is not an identifier and no title matched it exactly."
        + (f" Closest matches:\n{listing}" if listing else "")
    )


def _summary(rec: Record) -> str:
    p = rec.paper
    authors = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")
    lines = [rec.key, f"  {p.title}", f"  {authors} ({p.year or 'n.d.'})"]
    for kind, path in rec.files.items():
        lines.append(f"  {kind + ':':6}{path}")
    src = rec.source
    lines.append(f"  via {src.get('source')} ({src.get('via')}): {src.get('url')}")
    return "\n".join(lines)


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), help="Config file to use.")
@click.pass_context
def cli(ctx, config_path):
    """Fetch research papers politely and turn them into readable text.

    Papers are identified by DOI, arXiv id, PMID, PMCID, ACL Anthology id,
    OpenReview id or URL, e.g. 10.18653/v1/2020.acl-main.1, arXiv:1706.03762.
    """
    try:
        ctx.obj = App(config_path)
    except (OSError, ValueError) as e:
        raise click.ClickException(f"config: {e}") from e


def _fetcher(app: App, browser: bool, visible: bool | None, quiet: bool, recheck: bool = False) -> Fetcher:
    log = (lambda s: None) if quiet else _err
    return Fetcher(app.config, app.client, app.library, use_browser=browser, visible=visible, log=log,
                   recheck=recheck)


browser_options = [
    click.option("--browser/--no-browser", default=True, help="Fall back to a real browser (default: on)."),
    click.option("--visible/--headless", default=None,
                 help="Show the browser window (default: only when a site wants a human check). --visible also "
                      "asks again at sites whose check was not completed earlier."),
    click.option("-q", "--quiet", is_flag=True, help="Don't log each attempt."),
]


def with_browser_options(f):
    for opt in reversed(browser_options):
        f = opt(f)
    return f


def _read_list(stream) -> list[tuple[str | None, str]]:
    """Lines of `key<TAB>identifier`, or just `identifier`; blank lines and # comments are skipped."""
    items = []
    for line in stream:
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, tab, ident = line.partition("\t")
        items.append((key.strip() or None, ident.strip()) if tab else (None, line.strip()))
    return items


EXPORT_FIELDS = {"key": "k", "libkey": "k", "year": 2000, "doi": "d", "first_author": "a"}


def _export(rec: Record, key: str | None, directory: Path, name: str) -> Path:
    p = rec.paper
    fields = {"key": key or rec.key, "libkey": rec.key, "year": p.year or "", "doi": (p.doi or "").replace("/", "_"),
              "first_author": p.authors[0] if p.authors else ""}
    dest = directory / name.format(**fields)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(rec.text_path.read_bytes())
    return dest


def _fetch_all(app: App, fetcher: Fetcher, items, *, force=False, as_json=False, quiet=False,
               export: Path | None = None, name: str = "{key}.md") -> bool:
    """Get each (key, identifier); report as `paper get` does. Returns whether all succeeded."""
    single = len(items) == 1 and not as_json
    results, rows, noted = [], [], False
    try:
        for key, text in items:
            label = key or text
            try:
                ident = _identify(app, text)
                if not quiet:
                    _err(f"{ident}:")
                rec = fetcher.get(ident, force=force, key=key)
                app.pending.remove(rec.paper)
                result = {"input": text, "ok": True, **rec.to_dict()}
                if export:
                    result["exported"] = str(_export(rec, key, export, name))
                results.append(result)
                src = rec.source
                rows.append(("ok", key or rec.key, ", ".join(filter(None, (src.get("source"), src.get("version"))))))
                if single:
                    click.echo(_summary(rec) + (f"\n  exported: {result['exported']}" if export else ""))
            except FetchFailed as e:
                app.pending.add(e.paper, str(ident), e.reason, e.open_url, e.hints, key=key)
                results.append({"input": text, "ok": False, "paper": e.paper.to_dict(), "reason": e.reason,
                                "open": e.open_url, "attempts": [a.__dict__ for a in e.attempts], "hints": e.hints})
                rows.append(("FAIL", label, e.reason))
                noted = True
                if single:
                    _err(f"{text}: {e}\n  why:  {e.reason}" + (f"\n  open: {e.open_url}" if e.open_url else ""))
                    for h in e.hints:
                        _err(f"  hint: {h}")
                    _err("  (listed in `paper pending`)")
            except (click.UsageError, QuotaExceeded, ValueError) as e:
                msg = e.format_message() if isinstance(e, click.UsageError) else str(e)
                results.append({"input": text, "ok": False, "error": msg})
                rows.append(("FAIL", label, msg.splitlines()[0]))
                if single:
                    _err(f"{text}: {msg}")
    finally:
        fetcher.close()
    if as_json:
        click.echo(json.dumps(results, indent=2, ensure_ascii=False))
    elif not single:
        width = min(40, max(len(r[1]) for r in rows))
        click.echo("\n".join(f"{status:4}  {label:{width}}  {detail}" for status, label, detail in rows))
        if noted:
            click.echo("(failed downloads are listed in `paper pending`)")
    return all(r["ok"] for r in results)


@cli.command()
@click.argument("papers", nargs=-1)
@click.option("--from", "from_file", type=click.File("r"),
              help="Read papers from a file (- for stdin): `key<TAB>identifier` or `identifier` per line.")
@click.option("--key", help="Store the paper under this key (one paper only), e.g. its bibliography key.")
@click.option("--export", type=click.Path(file_okay=False, path_type=Path),
              help="Also copy each paper's text into this directory.")
@click.option("--name", default="{key}.md", show_default=True,
              help="File name for --export; fields: {key}, {libkey}, {year}, {doi}, {first_author}.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--force", is_flag=True, help="Download again even if the paper is in the library.")
@with_browser_options
@click.pass_obj
def get(app: App, papers, from_file, key, export, name, as_json, force, browser, visible, quiet):
    """Download papers into the library and extract their text.

    With several papers, ends with one line per paper: ok and the source, or why it failed.
    Failures are kept in `paper pending`.
    """
    items = [(None, p) for p in papers] + (_read_list(from_file) if from_file else [])
    if not items:
        raise click.UsageError("give papers to get, or --from FILE")
    if key:
        if len(items) != 1:
            raise click.UsageError("--key is for a single paper; give keys per line with --from")
        items = [(key, items[0][1])]
    try:
        name.format(**EXPORT_FIELDS)
    except (KeyError, IndexError, ValueError) as e:
        raise click.UsageError(f"--name: unknown field {e}") from e
    ok = _fetch_all(app, _fetcher(app, browser, visible, quiet), items, force=force, as_json=as_json, quiet=quiet,
                    export=export, name=name)
    sys.exit(0 if ok else 1)


IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def _absolute_figures(app: App, text: str) -> str:
    """Image links relative to the library, made absolute so an agent can open them."""
    root = app.config.library_dir.resolve()
    return IMAGE_RE.sub(lambda m: f"![{m.group(1)}]({root / m.group(2)})", text)


def _figure_list(text: str) -> list[tuple[int | None, str, str]]:
    """(page, caption, path) of each image in the text."""
    out, page = [], None
    for line in text.split("\n"):
        if m := sections.PAGE_RE.fullmatch(line.strip()):
            page = int(m.group(1))
        out += [(page, m.group(1), m.group(2)) for m in IMAGE_RE.finditer(line)]
    return out


def _print_figures(text: str) -> None:
    figures = _figure_list(text)
    for page, caption, path in figures:
        click.echo(f"{'p' + str(page) if page else '':5} {caption}\n      {path}")
    if not figures:
        click.echo("no figure or table images")


def _select_pages(text: str, pages: str) -> str:
    wanted: set[int] = set()
    for part in pages.split(","):
        a, _, b = part.partition("-")
        wanted.update(range(int(a), int(b or a) + 1))
    chunks = re.split(r"(?=<!-- page \d+ -->)", text)
    keep = [chunks[0]] + [c for c in chunks[1:] if int(re.match(r"<!-- page (\d+)", c).group(1)) in wanted]
    return "".join(keep)


@cli.command()
@click.argument("paper")
@click.option("--pages", help="Only these pages, e.g. 1-3,7 (PDF sources).")
@click.option("--sections", "list_sections", is_flag=True, help="List the section headings, with their pages.")
@click.option("--section", "wanted", multiple=True,
              help="Only this section, e.g. Methods (repeatable). Matches the start of a heading.")
@click.option("--no-refs", is_flag=True, help="Leave out the reference list.")
@click.option("--figures", "list_figures", is_flag=True, help="List the figure and table images, with their paths.")
@with_browser_options
@click.pass_obj
def read(app: App, paper, pages, list_sections, wanted, no_refs, list_figures, browser, visible, quiet):
    """Print a paper's text (fetching it first if needed).

    Figures and tables are images next to the text, linked where they appear: open them to
    look at them. Headings of PDF text are guessed (numbered lines, the usual section names),
    so --sections shows what --section can find.
    """
    fetcher = _fetcher(app, browser, visible, quiet=True if quiet else False)
    try:
        rec = fetcher.get(_identify(app, paper))
    except FetchFailed as e:
        for h in e.hints:
            _err(f"hint: {h}")
        raise click.ClickException(str(e)) from e
    finally:
        fetcher.close()
    text = _absolute_figures(app, rec.text_path.read_text())
    if pages:
        text = _select_pages(text, pages)
    if list_figures:
        _print_figures(text)
        return
    if list_sections:
        for h in sections.headings(text):
            click.echo(f"{'p' + str(h.page) if h.page else '':5} {'  ' * (h.level - 1)}{h.title}")
        return
    if no_refs:
        text = sections.without_references(text)
    if wanted:
        text, missing = sections.extract(text, list(wanted))
        if missing:
            _err(f"no section matching {', '.join(missing)}; see `paper read {paper} --sections`")
            if not text:
                sys.exit(1)
    click.echo(text)


@cli.command()
@click.argument("paper")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def resolve(app: App, paper, as_json):
    """Show metadata and candidate locations without downloading."""
    fetcher = _fetcher(app, False, None, quiet=True)
    p, cands = fetcher.resolve(_identify(app, paper))
    if as_json:
        click.echo(json.dumps({"paper": p.to_dict(), "candidates": [
            {"url": c.url, "kind": c.kind, "source": c.source, "via": c.via, "version": c.version}
            for c in cands]}, indent=2, ensure_ascii=False))
        return
    click.echo(f"{p.title}\n{', '.join(p.authors)} ({p.year})  {p.venue or ''}")
    click.echo("ids: " + "  ".join(p.ids()))
    for c in cands:
        click.echo(f"  {c.source:16} {c.via:7} {c.kind:8} {c.url}" + (f"  [{c.version}]" if c.version else ""))


@cli.command()
@click.argument("query", nargs=-1, required=True)
@click.option("-n", default=10, help="Number of results.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def search(app: App, query, n, as_json):
    """Search OpenAlex by title/keywords."""
    hits = _search(app, " ".join(query), n)
    if as_json:
        click.echo(json.dumps(hits, indent=2, ensure_ascii=False))
        return
    for h in hits:
        authors = ", ".join(h["authors"][:3]) + (" et al." if len(h["authors"]) > 3 else "")
        ident = hit_identifier(h) or f"(no DOI; OpenAlex {h['openalex']})"
        oa = " [open access]" if h["oa"] else ""
        click.echo(f"{ident}\n  {h['title']} ({h['year']})\n  {authors}; {h['venue'] or ''}{oa}")


@cli.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def list_(app: App, as_json):
    """List the papers in the library."""
    recs = app.library.all()
    if as_json:
        click.echo(json.dumps([r.to_dict() for r in recs], indent=2, ensure_ascii=False))
        return
    for r in recs:
        click.echo(f"{r.key:50} {r.paper.year or '':5} {r.paper.title}")


def _id_in_text(text: str) -> str | None:
    """A DOI or arXiv id on the first pages of a paper, for `paper add` without an identifier."""
    head = re.split(r"<!-- page 3 -->", text)[0]
    if m := re.search(r"arXiv:\s*(\d{4}\.\d{4,5})", head):
        return f"arXiv:{m.group(1)}"
    if m := DOI_RE.search(head):
        return f"doi:{normalize_doi(m.group(0).rstrip('⟩>]'))}"
    return None


@cli.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("paper", required=False)
@click.option("--key", help="Store the paper under this key.")
@click.option("--force", is_flag=True, help="Replace the copy already in the library.")
@click.pass_obj
def add(app: App, file: Path, paper, key, force):
    """Add a PDF (or JATS XML) you downloaded yourself, with its metadata.

    PAPER identifies it (DOI, arXiv id, ...). Without it, the first DOI or arXiv id
    on the PDF's first pages is used.
    """
    from .extract import pdf_to_text

    content = file.read_bytes()
    if not is_pdf(content) and b"<article" not in content[:5000]:
        raise click.UsageError(f"{file} is not a PDF or JATS XML file")
    if not paper:
        paper = _id_in_text(pdf_to_text(content)) if is_pdf(content) else None
        if not paper:
            raise click.UsageError("no DOI or arXiv id found in the file; give one: paper add FILE ID")
        _err(f"using {paper}, found in the file")
    ident = _identify(app, paper)
    if (rec := app.library.find(Paper.from_identifier(ident))) and not force:
        raise click.ClickException(f"already in the library as {rec.key}; --force replaces it")
    fetcher = _fetcher(app, False, None, quiet=True)
    try:
        rec = fetcher.add(ident, content, str(file.resolve()), key=key)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    finally:
        fetcher.close()
    app.pending.remove(rec.paper)
    click.echo(_summary(rec))


@cli.command()
@click.argument("papers", nargs=-1)
@click.option("--all", "everything", is_flag=True, help="Every paper in the library stored before figures existed.")
@click.option("--rebuild", is_flag=True, help="Extract the text and figures again, even if done before.")
@with_browser_options
@click.pass_obj
def figures(app: App, papers, everything, rebuild, browser, visible, quiet):
    """List a paper's figure and table images, extracting them first for papers stored earlier.

    Extraction also cleans the text again (cover pages, watermarks). PDFs need no network;
    figures of open-access PMC articles come from Europe PMC, and those of PMC author
    manuscripts from PMC's site, in the browser.
    """
    if not papers and not everything:
        raise click.UsageError("give papers, or --all")
    fetcher = _fetcher(app, browser, visible, quiet)
    try:
        if everything:
            for rec in app.library.all():
                if rebuild or "figures" not in rec.source:
                    rec = fetcher.rebuild(rec)
                    click.echo(f"{rec.key}: {rec.source['figures']} image(s)")
            return
        for text in papers:
            rec = app.library.by_key(text.strip()) or app.library.find(Paper.from_identifier(_identify(app, text)))
            if rec is None:
                raise click.ClickException(f"{text} is not in the library; `paper get` it first")
            if rebuild or "figures" not in rec.source:
                rec = fetcher.rebuild(rec)
            if len(papers) > 1:
                click.echo(f"== {rec.key}")
            _print_figures(_absolute_figures(app, rec.text_path.read_text()))
    finally:
        fetcher.close()


@cli.command()
@click.argument("papers", nargs=-1, required=True)
@click.option("--key", help="Citation key (one paper only). Default: the library key when you give one, "
                            "else surname, year and title word (roseberry2014skype).")
@click.pass_obj
def bib(app: App, papers, key):
    """Print BibTeX entries for papers, from the metadata in the library (looked up if missing)."""
    if key and len(papers) != 1:
        raise click.UsageError("--key is for a single paper")
    fetcher = _fetcher(app, False, None, quiet=True)
    try:
        for text in papers:
            ident = _identify(app, text)
            rec = app.library.by_key(text.strip()) or app.library.find(Paper.from_identifier(ident))
            paper = rec.paper if rec else None
            if paper is None or (paper.doi and not paper.pub_type):
                described = fetcher.describe(ident)
                if paper is not None:  # stored before publication details were collected: add them
                    described.update(**{k: v for k, v in paper.to_dict().items() if k != "year"})
                    rec = app.library.update_paper(rec, described)
                paper = described
            click.echo(bibtex(paper, key or (text.strip() if rec and rec.key == text.strip() else None)) + "\n")
    finally:
        fetcher.close()


@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.option("--tsv", is_flag=True, help="`key<TAB>identifier` lines, for `paper get --from -` elsewhere.")
@click.option("--retry", is_flag=True, help="Try them all again now (a browser window opens for checks).")
@click.option("--clear", is_flag=True, help="Forget them.")
@click.pass_obj
def pending(app: App, as_json, tsv, retry, clear):
    """Papers that need a person: a human check, a sign-in, or a download by hand.

    `paper get` adds every paper it could not fetch and removes it once it has it.
    """
    entries = app.pending.all()
    if clear:
        app.pending.clear()
        click.echo(f"forgot {len(entries)} pending paper(s)")
        return
    if retry:
        if not entries:
            click.echo("nothing pending")
            return
        items = [(e.get("key"), e["id"]) for e in entries]
        sys.exit(0 if _fetch_all(app, _fetcher(app, True, None, False, recheck=True), items) else 1)
    if as_json:
        click.echo(json.dumps(entries, indent=2, ensure_ascii=False))
        return
    for e in entries:
        if tsv:
            click.echo(f"{e.get('key') or ''}\t{e['id']}" if e.get("key") else e["id"])
            continue
        click.echo(f"{e.get('key') or e['id']}  {e.get('title') or ''}")
        click.echo(f"  why:  {e.get('reason')}")
        if e.get("open"):
            click.echo(f"  open: {e['open']}")
    if not entries and not tsv:
        click.echo("nothing pending")


@cli.command()
@click.argument("papers", nargs=-1)
@click.option("--remove", is_flag=True, help="Move papers that do not match into the library's mismatched/ folder.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_obj
def verify(app: App, papers, remove, as_json):
    """Check that each stored document is the paper it is stored as (all papers by default).

    Its first pages should show the DOI, or most of the title's words: the check `paper get` makes
    before storing a download. Changes nothing unless --remove is given.
    """
    recs = []
    for text in papers:
        rec = app.library.by_key(text.strip())
        if rec is None:
            try:
                rec = app.library.find(Paper.from_identifier(parse(text)))
            except UnrecognizedIdentifier:
                pass
        if rec is None:
            raise click.UsageError(f"{text!r} is not in the library")
        recs.append(rec)
    results, counts = [], {True: 0, False: 0, None: 0}
    for rec in recs or app.library.all():
        verdict = check_record(rec)
        counts[verdict.ok] += 1
        result = {"key": rec.key, "title": rec.paper.title, "ok": verdict.ok, "detail": verdict.detail}
        if verdict.ok is False and remove:
            result["moved_to"] = str(app.library.set_aside(rec))
        results.append(result)
        if as_json or verdict.ok:
            continue
        click.echo(f"{'MISMATCH' if verdict.ok is False else '?':8}  {rec.key}  {rec.paper.title or ''}")
        click.echo(f"          {verdict.detail}")
        if "moved_to" in result:
            click.echo(f"          moved to {result['moved_to']}; `paper get {rec.paper.ids()[0]}` fetches it again")
    if as_json:
        click.echo(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        click.echo(f"{len(results)} paper(s) checked: {counts[True]} match, {counts[False]} mismatched, "
                   f"{counts[None]} could not be checked")
    sys.exit(1 if counts[False] else 0)


@cli.command()
@click.argument("url", required=False)
@click.pass_obj
def login(app: App, url):
    """Open the browser so you can sign in (library proxy or a publisher site).

    The session is stored in paperreader's private browser profile and reused
    by later downloads. Without URL, opens your institution's proxy.
    """
    if not url:
        if not app.config.proxy:
            raise click.UsageError("give the URL of the site to sign in to, or set institution.proxy in the config")
        url = proxied(app.config, "https://doi.org/")
    try:
        browser_login(app.config, url)
    except BrowserUnavailable as e:
        raise click.ClickException(str(e)) from e


@cli.command("install-browser")
def install_browser():
    """Download the Chromium build that the browser fallback uses."""
    import subprocess

    sys.exit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]))


@cli.command()
@click.pass_obj
def usage(app: App):
    """Show requests per host in the last hour and day, against their budgets."""
    for host, hour, day in app.client.limiter.usage():
        lim = app.config.limit_for(host)
        click.echo(f"{host:40} {hour:4}/{lim.per_hour or '-'} per hour  {day:5}/{lim.per_day or '-'} per day")


@cli.command("config")
@click.option("--init", is_flag=True, help="Write a commented config template.")
@click.pass_obj
def config_cmd(app: App, init):
    """Show the configuration (or create it with --init)."""
    cfg = app.config
    path = cfg.path or config_mod.default_config_path()
    if init:
        config_mod.write_template(path)
        click.echo(f"wrote {path}")
        return
    secret = lambda v: "set" if v else "-"  # noqa: E731
    click.echo(f"config file:   {path}{'' if path.exists() else ' (missing; create with --init)'}")
    click.echo(f"email:         {cfg.email or '- (needed for Unpaywall)'}")
    click.echo(f"library:       {cfg.library_dir}")
    click.echo(f"state:         {cfg.state_dir}")
    click.echo(f"proxy:         {cfg.proxy or '-'}")
    click.echo(f"browser:       {'on' if cfg.browser_enabled else 'off'}, mode={cfg.browser_mode}"
               + (f", cdp={cfg.browser_cdp_url}" if cfg.browser_cdp_url else ""))
    click.echo(f"keys:          semantic_scholar={secret(cfg.semantic_scholar_key)} "
               f"openalex={secret(cfg.openalex_key)} elsevier={secret(cfg.elsevier_key)}")
    d = cfg.default_limit
    click.echo(f"default limit: {d.interval}s between requests, {d.per_hour}/hour, {d.per_day}/day per host")


def main() -> None:
    try:
        cli()
    except (RateLimited, QuotaExceeded, HTTPError) as e:
        _err(f"error: {e}")
        sys.exit(2)
