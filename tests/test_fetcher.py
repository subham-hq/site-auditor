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


def fetcher_for(
    transport: httpx.MockTransport,
    *,
    timeout: float = 5.0,
    max_body_bytes: int = 10 * 1024 * 1024,
) -> HttpxFetcher:
    """A fetcher with no retries and no rate limiting worth waiting on."""

    return HttpxFetcher(
        rate=100.0,
        max_concurrency=1,
        timeout=timeout,
        retry_policy=RetryPolicy(attempts=1),
        transport=transport,
        max_body_bytes=max_body_bytes,
    )


class CountingStream(httpx.AsyncByteStream):
    """A body that records how much of itself was actually read."""

    def __init__(self, *, chunks: int, size: int) -> None:
        self.sent = 0
        self._chunks = chunks
        self._size = size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.sent += self._size
            yield b"x" * self._size


def serving(stream: httpx.AsyncByteStream, headers: dict[str, str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    return httpx.MockTransport(handler)


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


# ─────────────────────────────────────────────────────────────────────────────
# How much of a body is read
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content_type", ["application/pdf", "image/png", "application/octet-stream", ""]
)
async def test_a_body_that_will_not_be_parsed_is_not_downloaded(content_type: str) -> None:
    """A link to a PDF or an installer is checked by its status, not fetched whole.

    Reading the body first and checking the type afterwards pulled every linked
    download into memory only to throw it away: a 50 MB file peaked at 105 MB.
    The headers now decide before a byte of the body is read.
    """

    stream = CountingStream(chunks=16, size=64 * 1024)
    headers = {"content-type": content_type} if content_type else {}
    async with fetcher_for(serving(stream, headers)) as fetcher:
        response = await fetcher.get(URL)

    assert response.status == 200
    assert response.text is None
    assert stream.sent == 0


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("text/html; charset=utf-8", "<p>café</p>".encode(), "<p>café</p>"),
        ("text/html; charset=iso-8859-1", "<p>café</p>".encode("latin-1"), "<p>café</p>"),
        ("text/plain", b"User-agent: *\nDisallow: /x", "User-agent: *\nDisallow: /x"),
        ("Text/HTML", b"<p>x</p>", "<p>x</p>"),
    ],
)
async def test_a_text_body_is_read_and_decoded(
    content_type: str, body: bytes, expected: str
) -> None:
    """HTML and plain text are read in full and decoded with the declared charset.

    text/plain matters as much as HTML: robots.txt travels through this same
    fetcher, and a robots file that came back as None would mean no rules at
    all. Media types are case-insensitive, so Text/HTML counts too.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    async with fetcher_for(httpx.MockTransport(handler)) as fetcher:
        response = await fetcher.get(URL)

    assert response.text == expected
    assert response.elapsed_ms >= 0


async def test_a_text_body_over_the_cap_is_abandoned() -> None:
    """A text body larger than max_body_bytes is cut off as soon as it crosses.

    It comes back with text=None, exactly like a binary: the link is checked,
    the page is not parsed. Reading stops at the line rather than at the end,
    so a runaway response costs the cap, not its own size.
    """

    stream = CountingStream(chunks=64, size=1024)
    transport = serving(stream, {"content-type": "text/html"})
    async with fetcher_for(transport, max_body_bytes=4096) as fetcher:
        response = await fetcher.get(URL)

    assert response.text is None
    assert stream.sent < 64 * 1024


async def test_a_declared_length_over_the_cap_is_never_read() -> None:
    """An honest Content-Length over the cap costs nothing at all."""

    stream = CountingStream(chunks=4, size=1024)
    headers = {"content-type": "text/html", "content-length": str(10 * 1024**3)}
    async with fetcher_for(serving(stream, headers), max_body_bytes=4096) as fetcher:
        response = await fetcher.get(URL)

    assert response.text is None
    assert stream.sent == 0


def test_a_non_positive_body_cap_is_refused() -> None:
    """Zero would treat every page as oversized and silently parse nothing."""

    with pytest.raises(ValueError):
        fetcher_for(httpx.MockTransport(drip), max_body_bytes=0)
