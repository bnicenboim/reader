"""Command line interface: `paper get`, `paper read`, ..."""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

import click

from . import config as config_mod
from .browser import BrowserUnavailable
from .browser import login as browser_login
from .fetch import Fetcher, FetchFailed
from .http import Cache, HTTPError, PoliteClient, RateLimited
from .identifiers import UnrecognizedIdentifier, parse
from .library import Library, Record
from .publishers import proxied
from .ratelimit import QuotaExceeded, RateLimiter
from .sources import crossref_search, hit_identifier, openalex_search


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
    """An identifier, or for free text the search hit whose title matches it closely."""
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


def _fetcher(app: App, browser: bool, visible: bool | None, quiet: bool) -> Fetcher:
    log = (lambda s: None) if quiet else _err
    return Fetcher(app.config, app.client, app.library, use_browser=browser, visible=visible, log=log)


browser_options = [
    click.option("--browser/--no-browser", default=True, help="Fall back to a real browser (default: on)."),
    click.option("--visible/--headless", default=None,
                 help="Show the browser window (default: only when a site wants a human check)."),
    click.option("-q", "--quiet", is_flag=True, help="Don't log each attempt."),
]


def with_browser_options(f):
    for opt in reversed(browser_options):
        f = opt(f)
    return f


@cli.command()
@click.argument("papers", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--force", is_flag=True, help="Download again even if the paper is in the library.")
@with_browser_options
@click.pass_obj
def get(app: App, papers, as_json, force, browser, visible, quiet):
    """Download papers into the library and extract their text."""
    fetcher = _fetcher(app, browser, visible, quiet)
    results, failed = [], False
    try:
        for text in papers:
            try:
                ident = _identify(app, text)
                if not quiet:
                    _err(f"{ident}:")
                rec = fetcher.get(ident, force=force)
                results.append({"input": text, "ok": True, **rec.to_dict()})
                if not as_json:
                    click.echo(_summary(rec))
            except FetchFailed as e:
                failed = True
                results.append({"input": text, "ok": False, "paper": e.paper.to_dict(),
                                "attempts": [a.__dict__ for a in e.attempts], "hints": e.hints})
                if not as_json:
                    _err(f"{text}: {e}")
                    for h in e.hints:
                        _err(f"  hint: {h}")
            except (click.UsageError, QuotaExceeded) as e:
                failed = True
                results.append({"input": text, "ok": False, "error": str(e)})
                if not as_json:
                    _err(f"{text}: {e}")
    finally:
        fetcher.close()
    if as_json:
        click.echo(json.dumps(results, indent=2, ensure_ascii=False))
    sys.exit(1 if failed else 0)


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
@with_browser_options
@click.pass_obj
def read(app: App, paper, pages, browser, visible, quiet):
    """Print a paper's text (fetching it first if needed)."""
    fetcher = _fetcher(app, browser, visible, quiet=True if quiet else False)
    try:
        rec = fetcher.get(_identify(app, paper))
    except FetchFailed as e:
        for h in e.hints:
            _err(f"hint: {h}")
        raise click.ClickException(str(e)) from e
    finally:
        fetcher.close()
    text = rec.text_path.read_text()
    click.echo(_select_pages(text, pages) if pages else text)


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
