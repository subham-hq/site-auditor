import asyncio
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from siteaudit.decorators import RetryPolicy, retry
from siteaudit.errors import FetchError
from siteaudit.models import Response
from siteaudit.ratelimit import RateLimiter
from siteaudit.urls import normalize_url


class HttpxFetcher:
    """The real Fetcher: one httpx.AsyncClient for the whole run.

    A single client means connection pooling — reusing it is roughly twice as
    fast as building one per request, because the TLS handshake happens once
    per host rather than once per URL.

    Adapts httpx.Response into the local Response type and translates every
    httpx.HTTPError into FetchError, so crawler.py never learns which HTTP
    client is underneath. A 404 is not an error here — it is a successful
    fetch that returned 404, and reporting it is the point of the tool.
    """

    def __init__(
        self,
        *,
        max_concurrency: int,
        timeout: float,
        rate: float,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._client: httpx.AsyncClient | None = None
        self._max_concurrency = max_concurrency
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._limiter = RateLimiter(rate=rate)
        self._retry_policy = retry_policy or RetryPolicy()
        self._get = retry(self._retry_policy)(self._get_once)

    async def __aenter__(self) -> "HttpxFetcher":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=self._max_concurrency),
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
        """

        if self._client is None:
            raise RuntimeError("Fetcher must be used inside 'async with'")
        async with self._semaphore:
            host = urlsplit(url).hostname or ""
            await self._limiter.acquire(host)
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                raise FetchError(f"could not fetch {url}: {exc}") from exc

            content_type = response.headers.get("content-type", "")
            return Response(
                url=url,
                final_url=normalize_url(str(response.url)),
                status=response.status_code,
                elapsed_ms=response.elapsed.total_seconds() * 1000,
                content_type=content_type,
                text=response.text
                if content_type.startswith(("text/html", "text/plain"))
                else None,
            )

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

        The body is read for text/html and text/plain — the second because
        robots.txt is plain text and travels through this same fetcher. A PDF
        or an image comes back with `text=None` rather than a binary blob
        decoded into memory: the link is still checked, it just isn't parsed.
        """

        return await self._get(url)
