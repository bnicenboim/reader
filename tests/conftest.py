from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from paperreader.config import Config, Limit
from paperreader.http import Cache, PoliteClient
from paperreader.ratelimit import RateLimiter


def make_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with Helvetica text, built by hand."""
    content = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class Router:
    """httpx mock: maps URLs (without query, or with) to responses and records requests."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, response=None, *, json_body=None, status=200, headers=None, content=b""):
        if response is None:
            if json_body is not None:
                content = json.dumps(json_body).encode()
                headers = {"content-type": "application/json", **(headers or {})}
            response = (status, headers or {}, content)
        self.routes[url] = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        route = self.routes.get(url) or self.routes.get(url.split("?")[0])
        if route is None:
            return httpx.Response(404, content=b"not found")
        if callable(route):
            return route(request)
        status, headers, content = route
        return httpx.Response(status, headers=headers, content=content)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config(library_dir=tmp_path / "papers", state_dir=tmp_path / "state", browser_enabled=False)
    cfg.limits = {}
    cfg.default_limit = Limit(interval=1.0, per_hour=100, per_day=1000)
    return cfg


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def client(config, router, clock) -> PoliteClient:
    limiter = RateLimiter(config.state_dir / "rl.sqlite", config.limit_for, clock=clock, sleep=clock.sleep)
    return PoliteClient(
        config, limiter, Cache(config.state_dir / "cache.sqlite"), transport=httpx.MockTransport(router)
    )
