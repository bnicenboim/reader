"""User configuration, read from a TOML file."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def default_config_path() -> Path:
    env = os.environ.get("PAPERREADER_CONFIG")
    if env:
        return Path(env).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "paperreader" / "config.toml"


@dataclass(frozen=True)
class Limit:
    """Politeness budget for one host."""

    interval: float  # minimum seconds between the start of two requests
    per_hour: int | None = None
    per_day: int | None = None


# APIs publish their limits; publisher sites do not, and treat bursts of downloads
# from an institutional IP as "systematic downloading", which can get the whole
# institution's access suspended. So unknown hosts get a conservative budget.
DEFAULT_LIMIT = Limit(interval=5.0, per_hour=60, per_day=300)

BUILTIN_LIMITS: dict[str, Limit] = {
    "export.arxiv.org": Limit(3.0, per_day=2000),  # arXiv API terms: 1 request / 3 s
    "arxiv.org": Limit(3.0, per_hour=200, per_day=1000),
    "ar5iv.labs.arxiv.org": Limit(3.0, per_hour=200, per_day=1000),
    "api.openalex.org": Limit(0.2, per_day=20000),
    "api.crossref.org": Limit(0.2, per_day=20000),
    "api.unpaywall.org": Limit(0.2, per_day=20000),
    "api.semanticscholar.org": Limit(1.1, per_day=5000),
    "www.ebi.ac.uk": Limit(0.2, per_day=20000),
    "eutils.ncbi.nlm.nih.gov": Limit(0.4, per_day=5000),  # E-utilities: 3 requests / s without a key
    "cdn.ncbi.nlm.nih.gov": Limit(1.0, per_hour=300, per_day=2000),  # PMC's figure images
    "api.elsevier.com": Limit(1.0, per_hour=100, per_day=500),
    "doi.org": Limit(0.5, per_day=5000),
    "api.biorxiv.org": Limit(1.0, per_day=5000),
    "api.osf.io": Limit(1.0, per_day=5000),
    "aclanthology.org": Limit(2.0, per_hour=200, per_day=1000),
}


@dataclass
class Config:
    email: str | None = None
    library_dir: Path = field(default_factory=lambda: Path.home() / "papers")
    state_dir: Path = field(default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state") / "paperreader")

    # Institutional access: URL template with {url} (raw) or {url_encoded}, e.g.
    # "https://login.ezproxy.example.edu/login?url={url}" or an OpenAthens redirector.
    proxy: str | None = None

    semantic_scholar_key: str | None = None
    openalex_key: str | None = None
    elsevier_key: str | None = None
    elsevier_insttoken: str | None = None

    browser_enabled: bool = True
    browser_mode: str = "auto"  # auto | headless | visible
    browser_cdp_url: str | None = None  # drive an already-running Chrome instead
    browser_channel: str = "chromium"  # or "chrome" / "msedge" to use an installed browser
    browser_executable: str | None = None
    browser_challenge_wait: float = 120.0  # seconds to wait for a human to finish a check
    browser_paper_wait: float = 180.0  # seconds one paper may wait for a person in all
    browser_check_memory: float = 12 * 3600.0  # seconds a check nobody completed keeps its host from asking again

    limits: dict[str, Limit] = field(default_factory=lambda: dict(BUILTIN_LIMITS))
    default_limit: Limit = DEFAULT_LIMIT

    max_pdf_bytes: int = 200 * 1024 * 1024
    timeout: float = 60.0

    path: Path | None = None

    @property
    def browser_profile(self) -> Path:
        return self.state_dir / "browser-profile"

    def limit_for(self, host: str) -> Limit:
        """Most specific configured limit: exact host, then parent domains, then the default."""
        host = host.lower()
        parts = host.split(".")
        for i in range(len(parts) - 1):
            key = ".".join(parts[i:])
            if key in self.limits:
                return self.limits[key]
        return self.default_limit


def _limit(d: dict, base: Limit) -> Limit:
    return Limit(
        interval=float(d.get("interval", base.interval)),
        per_hour=d.get("per_hour", base.per_hour),
        per_day=d.get("per_day", base.per_day),
    )


# The email and API keys can also come from the environment, e.g. secrets of a cloud environment.
ENV_VARS = {
    "email": "PAPERREADER_EMAIL",
    "openalex_key": "OPENALEX_API_KEY",
    "semantic_scholar_key": "SEMANTIC_SCHOLAR_API_KEY",
    "elsevier_key": "ELSEVIER_API_KEY",
    "elsevier_insttoken": "ELSEVIER_INSTTOKEN",
}


def load(path: Path | None = None) -> Config:
    path = path or default_config_path()
    cfg = Config(path=path)
    if path.exists():
        _load_file(cfg, path)
    for attr, var in ENV_VARS.items():
        if not getattr(cfg, attr) and os.environ.get(var):
            setattr(cfg, attr, os.environ[var])
    return cfg


def _load_file(cfg: Config, path: Path) -> None:
    with path.open("rb") as f:
        data = tomllib.load(f)

    cfg.email = data.get("email") or None
    if "library_dir" in data:
        cfg.library_dir = Path(data["library_dir"]).expanduser()
    if "state_dir" in data:
        cfg.state_dir = Path(data["state_dir"]).expanduser()

    inst = data.get("institution", {})
    cfg.proxy = inst.get("proxy") or None

    keys = data.get("keys", {})
    cfg.semantic_scholar_key = keys.get("semantic_scholar") or None
    cfg.openalex_key = keys.get("openalex") or None
    cfg.elsevier_key = keys.get("elsevier") or None
    cfg.elsevier_insttoken = keys.get("elsevier_insttoken") or None

    browser = data.get("browser", {})
    cfg.browser_enabled = bool(browser.get("enabled", True))
    cfg.browser_mode = browser.get("mode", "auto")
    if cfg.browser_mode not in ("auto", "headless", "visible"):
        raise ValueError(f"{path}: browser.mode must be auto, headless or visible")
    cfg.browser_cdp_url = browser.get("cdp_url") or None
    cfg.browser_channel = browser.get("channel") or cfg.browser_channel
    cfg.browser_executable = browser.get("executable") or None
    cfg.browser_challenge_wait = float(browser.get("challenge_wait", cfg.browser_challenge_wait))
    cfg.browser_paper_wait = float(browser.get("paper_wait", cfg.browser_paper_wait))
    if "check_memory_hours" in browser:
        cfg.browser_check_memory = float(browser["check_memory_hours"]) * 3600

    limits = dict(data.get("rate_limits", {}))
    if "default" in limits:
        cfg.default_limit = _limit(limits.pop("default"), DEFAULT_LIMIT)
    for host, d in limits.items():
        cfg.limits[host.lower()] = _limit(d, cfg.limits.get(host.lower(), cfg.default_limit))


TEMPLATE = """\
# paperreader configuration

# Used for the Unpaywall API (required there) and to identify yourself to
# Crossref/OpenAlex, which give identified clients better service.
# Can instead be set in the environment: PAPERREADER_EMAIL.
email = ""

# Where downloaded papers (PDF + extracted text + metadata) are stored.
library_dir = "~/papers"

[institution]
# Your library's proxy, if it has one. {url} is replaced by the target URL
# ({url_encoded} for the percent-encoded form). Examples:
#   EZproxy:    "https://login.ezproxy.example.edu/login?url={url}"
#   OpenAthens: "https://go.openathens.net/redirector/example.edu?url={url_encoded}"
# Log in once with `paper login`; the session lives in a private browser profile.
proxy = ""

[keys]
# Each key can instead be set in the environment: OPENALEX_API_KEY,
# SEMANTIC_SCHOLAR_API_KEY, ELSEVIER_API_KEY, ELSEVIER_INSTTOKEN.
semantic_scholar = ""   # optional; unauthenticated requests share a busy pool
openalex = ""           # optional but recommended; free
elsevier = ""           # optional; https://dev.elsevier.com, works from entitled IPs
elsevier_insttoken = "" # optional; issued by Elsevier to your institution

[browser]
# The browser is used for sites that refuse plain HTTP clients and for
# institutional sessions. Needs: pip install 'paperreader[browser]'
enabled = true
# auto: headless, but open a visible window when a site asks for a human check
mode = "auto"
# Which browser to launch: "chromium" (Playwright's), "chrome" or "msedge"
# (installed ones), or a path in `executable`.
channel = "chromium"
# executable = "/usr/bin/chromium"
# Drive your everyday Chrome (started with --remote-debugging-port=9222)
# instead of the private profile, to reuse its logins:
cdp_url = ""
# Seconds to wait for you to complete one human check, and for all checks of one paper.
# challenge_wait = 120
# paper_wait = 180
# A site whose check you did not complete opens no window for this many hours
# (its papers fail at once and go to `paper pending`); --visible tries it anyway.
# check_memory_hours = 12

# Politeness budgets per host (parent domains match subdomains).
# [rate_limits.default]
# interval = 5.0     # seconds between requests to the same host
# per_hour = 60
# per_day = 300
#
# [rate_limits."sciencedirect.com"]
# interval = 10.0
"""


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"{path} already exists; not overwriting", file=sys.stderr)
        return
    path.write_text(TEMPLATE)
