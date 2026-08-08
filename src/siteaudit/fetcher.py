import asyncio
from types import TracebackType

import httpx

from siteaudit.errors import FetchError
from siteaudit.models import Response
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

    def __init__(self, *, max_concurrency: int, timeout: float) -> None:
        self._client: httpx.AsyncClient | None = None
        self._max_concurrency = max_concurrency
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def __aenter__(self) -> "HttpxFetcher":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
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

    async def get(self, url: str) -> Response:
        """Fetch one URL and return what came back, whatever the status.

        A 4xx or 5xx is a successful fetch that returned an error status, and
        reporting it is the whole point of the tool — so no raise_for_status()
        here, ever. Only transport failures raise: DNS, connection refused,
        timeout, TLS. Those become FetchError, so the caller never learns that
        httpx is underneath.

        Redirects are followed by the client, so `final_url` may differ from
        the `url` you passed in; comparing the two is how a redirect is
        detected downstream.

        The body is read only for text/html. A PDF or an image comes back with
        `text=None` rather than a binary blob decoded into memory — the link
        is still checked, it just isn't parsed.

        The semaphore bounds how many requests are in flight at once across
        every caller sharing this fetcher.
        """

        if self._client is None:
            raise RuntimeError("Fetcher must be used inside 'async with'")
        async with self._semaphore:
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
                text=response.text if content_type.startswith("text/html") else None,
            )
