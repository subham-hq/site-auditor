import asyncio
import time
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from siteaudit.decorators import RetryPolicy, retry
from siteaudit.errors import FetchError
from siteaudit.models import Response
from siteaudit.ratelimit import RateLimiter
from siteaudit.urls import normalize_url

# Bodies worth reading: HTML to parse, and plain text because robots.txt
# travels through this same fetcher.
TEXT_CONTENT_TYPES = ("text/html", "text/plain")

# An order of magnitude above any ordinary page, and small enough that twenty
# concurrent fetches stay within a few hundred megabytes.
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024


class HttpxFetcher:
    """The real Fetcher: one httpx.AsyncClient for the whole run.

    A single client means connection pooling — reusing it is roughly twice as
    fast as building one per request, because the TLS handshake happens once
    per host rather than once per URL.

    Adapts httpx.Response into the local Response type and translates every
    httpx.HTTPError into FetchError, so crawler.py never learns which HTTP
    client is underneath. A 404 is not an error here — it is a successful
    fetch that returned 404, and reporting it is the point of the tool.

    `transport` is the seam the tests use: an httpx.MockTransport serves canned
    responses with no sockets. Left as None, httpx opens real connections.
    """

    def __init__(
        self,
        *,
        max_concurrency: int,
        timeout: float,
        rate: float,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError(f"max_body_bytes must be positive, got {max_body_bytes}")
        self._client: httpx.AsyncClient | None = None
        self._max_concurrency = max_concurrency
        self._timeout = timeout
        self._transport = transport
        self._max_body_bytes = max_body_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._limiter = RateLimiter(rate=rate)
        self._retry_policy = retry_policy or RetryPolicy()
        self._get = retry(self._retry_policy)(self._get_once)

    async def __aenter__(self) -> "HttpxFetcher":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=self._max_concurrency),
            transport=self._transport,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_once(self, url: str) -> Response:
        """One attempt at one URL. Retrying is get()'s job.

        The semaphore and the rate limiter both live here, inside a single
        attempt. A worker waiting on the limiter therefore holds a concurrency
        slot — deliberate, since that throttles the crawler against a slow host
        — but a worker backing off between retries does not, because by then
        this method has already returned.

        `timeout` bounds the whole exchange — connect, headers and body — not
        each network read. httpx applies its own timeout per operation, so a
        server sending one byte just inside it could hold a request, a worker
        and a semaphore slot open indefinitely. The deadline starts after the
        rate limiter: time spent waiting for this host's turn is the crawler's
        own politeness, not the server's slowness.
        """

        if self._client is None:
            raise RuntimeError("Fetcher must be used inside 'async with'")
        async with self._semaphore:
            host = urlsplit(url).hostname or ""
            await self._limiter.acquire(host)
            started = time.perf_counter()
            try:
                async with (
                    asyncio.timeout(self._timeout),
                    self._client.stream("GET", url) as response,
                ):
                    content_type = response.headers.get("content-type", "")
                    text = await self._read_text(response, content_type)
            except TimeoutError as exc:
                raise FetchError(f"no full response within {self._timeout}s: {url}") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"could not fetch {url}: {exc}") from exc
            elapsed_ms = (time.perf_counter() - started) * 1000

            return Response(
                url=url,
                final_url=normalize_url(str(response.url)),
                status=response.status_code,
                elapsed_ms=elapsed_ms,
                content_type=content_type,
                text=text,
            )

    async def _read_text(self, response: httpx.Response, content_type: str) -> str | None:
        """The body as text when it is worth parsing, or None without reading it.

        Anything that is not HTML or plain text is closed unread: a link to an
        installer is checked by its status, not downloaded. A text body larger
        than max_body_bytes is abandoned the moment it crosses the line and
        comes back as None, like a binary. A declared Content-Length over the
        cap is believed up front, so an honest server costs nothing at all; the
        running count catches the ones that don't declare, or that lie.
        """

        if not content_type.lower().startswith(TEXT_CONTENT_TYPES):
            return None
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_body_bytes:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > self._max_body_bytes:
                return None
        return body.decode(response.encoding or "utf-8", errors="replace")

    async def get(self, url: str) -> Response:
        """Fetch one URL and return what came back, whatever the status.

        A 4xx or 5xx is a successful fetch that returned an error status, and
        reporting it is the whole point of the tool — so no raise_for_status()
        here, ever. Only transport failures raise: DNS, connection refused,
        timeout, TLS. Those become FetchError, so the caller never learns that
        httpx is underneath.

        Transport failures and server-side error statuses are retried with
        backoff; a 404 is not, because it will not become a 200. The policy is
        a constructor argument rather than a decorator so it can be tuned from
        the CLI or shortened in tests.

        Redirects are followed by the client, so `final_url` may differ from
        the `url` you passed in; comparing the two is how a redirect is
        detected downstream.

        The response is streamed, and the headers decide whether the body is
        read at all. Only text/html and text/plain are read, up to
        max_body_bytes; anything else — a PDF, an image, an installer — comes
        back with `text=None` without being downloaded. The link is still
        checked by its status; it just isn't parsed. For a skipped body,
        `elapsed_ms` is therefore close to the time to headers.
        """

        return await self._get(url)
