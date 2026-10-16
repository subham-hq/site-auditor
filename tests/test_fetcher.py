"""HttpxFetcher itself, driven through httpx.MockTransport.

Everywhere else the fakes stand in for the fetcher. These tests exercise the
real adapter instead: its deadline, its FetchError wrapping and how much of a
body it reads. MockTransport keeps them off the network — no sockets, no
servers, nothing that can flake.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from siteaudit.decorators import RetryPolicy
from siteaudit.errors import FetchError
from siteaudit.fetcher import HttpxFetcher

URL = "https://e.com/page"


def fetcher_for(transport: httpx.MockTransport, *, timeout: float = 5.0) -> HttpxFetcher:
    """A fetcher with no retries and no rate limiting worth waiting on."""

    return HttpxFetcher(
        rate=100.0,
        max_concurrency=1,
        timeout=timeout,
        retry_policy=RetryPolicy(attempts=1),
        transport=transport,
    )


class DripStream(httpx.AsyncByteStream):
    """A body that arrives one byte at a time: 50 bytes over 2.5 seconds."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(50):
            await asyncio.sleep(0.05)
            yield b"x"


def drip(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, stream=DripStream())


async def test_a_slow_drip_cannot_outlive_the_timeout() -> None:
    """timeout bounds the whole request, not each read.

    httpx's own timeouts apply per network operation, so a server sending one
    byte just inside the read timeout can hold a request open indefinitely —
    and with it a worker and a semaphore slot. The fetcher's deadline has to
    cover the exchange end to end.

    The outer wait_for is the safety net: without the deadline this test fails
    after one second instead of quietly taking the drip's full 2.5.
    """

    async with fetcher_for(httpx.MockTransport(drip), timeout=0.2) as fetcher:
        with pytest.raises(FetchError):
            await asyncio.wait_for(fetcher.get(URL), timeout=1.0)


async def test_a_transport_failure_becomes_a_fetch_error() -> None:
    """httpx's exceptions never escape the fetcher.

    The crawler catches FetchError and nothing else. An httpx.ConnectError that
    leaked through would end the crawl instead of being counted — and would
    make crawler.py depend on which HTTP client sits underneath.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with fetcher_for(httpx.MockTransport(refuse)) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.get(URL)


async def test_the_fetcher_refuses_to_run_outside_its_context() -> None:
    """get() before `async with` raises at once rather than building a client.

    The client is created on entry and closed on exit, so a fetcher used
    outside that block has no connection pool. Failing loudly is better than
    quietly opening one per request.
    """

    fetcher = fetcher_for(httpx.MockTransport(drip))
    with pytest.raises(RuntimeError):
        await fetcher.get(URL)
