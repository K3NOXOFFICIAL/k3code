"""The shared fetch layer: TTL cache, per-host token bucket, robots.txt and the whole-request deadline.

Everything runs on local MockTransport handlers and a fake clock: no real network, no real waiting.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from k3code.research import fetch as f
from k3code.research.fetch import FetchDeadline, FetchRefused, FetchStatus, WebFetcher
from k3code.research.tools import fetch_page

PAGE = b"<html><head><title>T</title></head><body><main><p>hello world</p></main></body></html>"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def fetcher_for(handler, clock: FakeClock, **kw) -> WebFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebFetcher(client=client, now=clock.now, sleep=clock.sleep, **kw)


async def test_second_fetch_within_ttl_is_one_http_request():
    clock, hits = FakeClock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.path)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=PAGE)

    web = fetcher_for(handler, clock, respect_robots=False, ttl=60)
    first = await web.get("https://cache.test/page")
    second = await web.get("https://cache.test/page")
    assert hits == ["/page"] and second.cached and second.body == first.body
    clock.t += 61  # past the TTL the page is fetched again
    assert (await web.get("https://cache.test/page")).cached is False and hits == ["/page", "/page"]


async def test_failed_answers_are_not_cached():
    clock, hits = FakeClock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(503 if len(hits) == 1 else 200, content=PAGE)

    web = fetcher_for(handler, clock, respect_robots=False)
    assert (await web.get("https://flaky.test/a")).status == 503
    assert (await web.get("https://flaky.test/a")).status == 200 and len(hits) == 2


async def test_robots_disallow_refuses_the_fetch_with_a_reason():
    clock, hits = FakeClock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, content=PAGE)

    web = fetcher_for(handler, clock)
    with pytest.raises(FetchRefused, match="robots.txt disallows"):
        await web.get("https://rules.test/private/x")
    assert "/private/x" not in hits  # refused before the page is requested
    assert (await web.get("https://rules.test/public")).status == 200


async def test_robots_rules_for_our_product_token_apply_and_others_do_not():
    clock = FakeClock()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            rules = "User-agent: BadBot\nDisallow: /\n\nUser-agent: k3code-research\nDisallow: /x"
            return httpx.Response(200, text=rules)
        return httpx.Response(200, content=PAGE)

    web = fetcher_for(handler, clock)
    with pytest.raises(FetchRefused):
        await web.get("https://tokens.test/x/1")
    assert (await web.get("https://tokens.test/y/1")).status == 200


async def test_missing_robots_allows_and_unreadable_robots_refuses_for_a_while():
    clock, robots_hits = FakeClock(), []
    mode = {"robots": 404}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            robots_hits.append(clock.t)
            return httpx.Response(mode["robots"], content=b"")
        return httpx.Response(200, content=PAGE)

    web = fetcher_for(handler, clock)
    assert (await web.get("https://gone.test/a")).status == 200  # 404 robots: everything allowed
    mode["robots"] = 503
    other = "https://down.test/a"
    with pytest.raises(FetchRefused, match="could not be read"):
        await web.get(other)
    with pytest.raises(FetchRefused, match="could not be read"):  # still inside the retry window: no second robots call
        await web.get("https://down.test/b")
    assert len(robots_hits) == 2
    clock.t += f.ROBOTS_RETRY + 1
    mode["robots"] = 404
    assert (await web.get("https://down.test/c")).status == 200


async def test_per_host_bucket_spaces_one_host_and_leaves_others_alone():
    clock, hits = FakeClock(), []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append((req.url.host, req.url.path, clock.t))
        return httpx.Response(200, content=PAGE)

    web = fetcher_for(handler, clock, respect_robots=False, per_host_rate=1.0, burst=1.0)
    for i in range(5):
        await web.get(f"https://busy.test/p{i}")
    await web.get("https://quiet.test/one")
    busy = [t for host, _path, t in hits if host == "busy.test"]
    assert busy == [0.0, 1.0, 2.0, 3.0, 4.0]  # one per second on the same host
    assert [t for host, _p, t in hits if host == "quiet.test"] == [4.0]  # a different host is not delayed


async def test_trickle_body_is_cut_at_the_deadline_and_flagged():
    clock, chunks = FakeClock(), []

    async def trickle():
        for _ in range(200):  # far more than the deadline allows
            clock.t += 1.0  # one second per chunk, on the fake clock
            chunks.append(1)
            yield b"<p>" + b"x" * 1000 + b"</p>"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=trickle())

    web = fetcher_for(handler, clock, respect_robots=False, deadline=3.5)
    got = await web.get("https://slow.test/trickle")
    assert got.truncated and got.body.startswith("<p>xxx") and len(chunks) < 10
    assert not got.cached  # a cut body is never cached


async def test_stalled_headers_hit_the_real_deadline():
    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)  # never answers in time
        return httpx.Response(200, content=PAGE)

    web = WebFetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), respect_robots=False,
                     deadline=0.05)
    with pytest.raises(FetchDeadline):
        await web.get("https://stall.test/page")


async def test_non_http_urls_are_refused_and_non_2xx_is_reported_not_raised():
    clock = FakeClock()
    web = fetcher_for(lambda req: httpx.Response(403, content=b"no"), clock, respect_robots=False)
    with pytest.raises(ValueError):
        await web.get("file:///etc/passwd")
    assert (await web.get("https://forbidden.test/x")).status == 403
    with pytest.raises(FetchStatus) as info:
        await fetch_page("https://forbidden.test/x", fetcher=web)
    assert info.value.status == 403


async def test_fetch_page_without_a_shared_fetcher_still_works_with_an_injected_client():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=PAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        title, text = await fetch_page("https://plain.test/p", client=client)
    assert title == "T" and "hello world" in text
