import httpx
import pytest

from paperreader.config import Config, Limit
from paperreader.http import Cache, CloudflareBlocked, PoliteClient, RateLimited, retry_after_seconds
from paperreader.ratelimit import QuotaExceeded, RateLimiter


def limiter(tmp_path, clock, limit):
    return RateLimiter(tmp_path / "rl.sqlite", lambda host: limit, clock=clock, sleep=clock.sleep)


def test_requests_to_a_host_are_spaced(tmp_path, clock):
    rl = limiter(tmp_path, clock, Limit(interval=3.0))
    start = clock.t
    for _ in range(3):
        rl.acquire("arxiv.org")
    assert clock.t - start == pytest.approx(6.0)
    # Other hosts are independent.
    assert rl.acquire("example.org") == 0


def test_budget_is_shared_between_processes(tmp_path, clock):
    a = limiter(tmp_path, clock, Limit(interval=5.0))
    b = limiter(tmp_path, clock, Limit(interval=5.0))
    a.acquire("h")
    assert b.acquire("h") == pytest.approx(5.0)


def test_hourly_cap(tmp_path, clock):
    rl = limiter(tmp_path, clock, Limit(interval=0.0, per_hour=2))
    rl.acquire("h")
    rl.acquire("h")
    with pytest.raises(QuotaExceeded) as e:
        rl.acquire("h")
    assert e.value.window == "hour"
    clock.t += 3601
    rl.acquire("h")


def test_penalize_pushes_next_slot(tmp_path, clock):
    rl = limiter(tmp_path, clock, Limit(interval=1.0))
    rl.penalize("h", 30)
    assert rl.acquire("h") == pytest.approx(30)


def test_unfinished_checks_are_remembered_for_a_while(tmp_path, clock):
    a = limiter(tmp_path, clock, Limit(interval=1.0))
    a.mark_check("Science.org", "https://www.science.org/doi/pdf/10.1/x")
    b = limiter(tmp_path, clock, Limit(interval=1.0))  # another process
    assert b.unfinished_check("science.org", within=3600) == "https://www.science.org/doi/pdf/10.1/x"
    clock.t += 3601
    assert b.unfinished_check("science.org", within=3600) is None
    a.mark_check("science.org", "u")
    b.clear_checks("science.org")
    assert a.unfinished_check("science.org", within=3600) is None


def test_check_waits_from_the_config(tmp_path):
    from paperreader.config import load

    path = tmp_path / "config.toml"
    path.write_text("[browser]\npaper_wait = 60\ncheck_memory_hours = 2\n")
    cfg = load(path)
    assert (cfg.browser_paper_wait, cfg.browser_check_memory) == (60.0, 7200.0)
    assert load(tmp_path / "missing.toml").browser_paper_wait == 180.0


def test_download_time_limit_from_the_config(tmp_path):
    from paperreader.config import load

    path = tmp_path / "config.toml"
    path.write_text("max_download_seconds = 90\n")
    assert load(path).max_download_seconds == 90.0
    assert load(tmp_path / "missing.toml").max_download_seconds == 600.0


def test_limit_lookup_matches_parent_domains():
    cfg = Config(limits={"sciencedirect.com": Limit(10.0)}, default_limit=Limit(5.0))
    assert cfg.limit_for("www.sciencedirect.com").interval == 10.0
    assert cfg.limit_for("example.com").interval == 5.0


def test_retry_after_parsing():
    assert retry_after_seconds("120") == 120
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 60) == pytest.approx(60)
    assert retry_after_seconds("soon") is None


def test_429_is_retried_after_waiting(client, router, clock):
    calls = []

    def flaky(request):
        calls.append(clock.t)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "20"})
        return httpx.Response(200, content=b"ok")

    router.add("https://api.example.org/x", flaky)
    r = client.get("https://api.example.org/x")
    assert r.status == 200
    assert calls[1] - calls[0] >= 20


def test_long_retry_after_gives_up(client, router):
    router.add("https://api.example.org/x", status=429, headers={"retry-after": "3600"})
    with pytest.raises(RateLimited):
        client.get("https://api.example.org/x")


def test_a_download_that_takes_too_long_is_given_up(config, client, router):
    router.add("https://slow.example/file.zip", content=b"some bytes")
    assert client.get("https://slow.example/file.zip").content == b"some bytes"
    config.max_download_seconds = -1.0  # any download is now too slow
    with pytest.raises(httpx.ReadTimeout, match="still downloading"):
        client.get("https://slow.example/file.zip")


def test_cloudflare_1015_is_a_block_not_retried(client, router):
    router.add("https://www.biorxiv.org/x.pdf", status=429, headers={"server": "cloudflare"},
               content=b"error code: 1015")
    with pytest.raises(CloudflareBlocked) as e:
        client.get("https://www.biorxiv.org/x.pdf")
    assert len(router.requests) == 1  # waiting would not help, so no retries
    assert "blocked by Cloudflare" in str(e.value)


def test_each_redirect_hop_is_rate_limited(client, router, clock):
    router.add("https://doi.org/10.1/x", status=302, headers={"location": "https://publisher.example/a"})
    router.add("https://publisher.example/a", status=302, headers={"location": "/b"})
    router.add("https://publisher.example/b", content=b"page")
    r = client.get("https://doi.org/10.1/x")
    assert r.url == "https://publisher.example/b"
    assert r.history == ["https://doi.org/10.1/x", "https://publisher.example/a"]
    assert sum(clock.slept) == pytest.approx(1.0)  # second hit on publisher.example waited


def test_api_answers_are_cached(client, router):
    router.add("https://api.example.org/w", json_body={"a": 1})
    assert client.get_json("https://api.example.org/w", {"q": "1"}, secret_params={"api_key": "s"}) == {"a": 1}
    assert client.get_json("https://api.example.org/w", {"q": "1"}, secret_params={"api_key": "s"}) == {"a": 1}
    assert len(router.requests) == 1
    assert "api_key=s" in router.urls()[0]


def test_missing_records_are_none(client, router):
    assert client.get_json("https://api.example.org/missing") is None


def test_user_agent_identifies_us(tmp_path, clock, router):
    cfg = Config(email="me@example.edu", state_dir=tmp_path)
    c = PoliteClient(cfg, limiter(tmp_path, clock, Limit(0)), Cache(tmp_path / "c.sqlite"),
                     transport=httpx.MockTransport(router))
    router.add("https://x.example/", content=b"")
    c.get("https://x.example/")
    ua = router.requests[0].headers["user-agent"]
    assert ua.startswith("paperreader/") and "mailto:me@example.edu" in ua


def test_api_keys_from_environment(tmp_path, monkeypatch):
    from paperreader.config import load

    monkeypatch.setenv("OPENALEX_API_KEY", "from-env")
    assert load(tmp_path / "missing.toml").openalex_key == "from-env"
    path = tmp_path / "config.toml"
    path.write_text('[keys]\nopenalex = "from-file"\n')
    assert load(path).openalex_key == "from-file"


def test_email_from_environment(tmp_path, monkeypatch):
    from paperreader.config import load

    monkeypatch.setenv("PAPERREADER_EMAIL", "agent@example.edu")
    assert load(tmp_path / "missing.toml").email == "agent@example.edu"
