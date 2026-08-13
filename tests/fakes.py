"""Test doubles that satisfy the Fetcher protocol without inheriting from it.

Structural typing is what makes this work: none of these classes reference
Fetcher at all, they just match its shape, and mypy accepts them wherever a
Fetcher is expected. That is the payoff for defining a local Response type
rather than returning httpx.Response — a fake here is six plain fields, not an
HTTP object assembled with headers and encodings.

No mocking library anywhere, deliberately. A fake that raises on the second
call is easier to read and harder to get subtly wrong than a patched method
with a side_effect list.
"""

from siteaudit.errors import FetchError
from siteaudit.models import Response


class FakeFetcher:
    """Raises FetchError `failures` times, then returns 200.

    Simulates a transport failure that recovers: DNS resolving on the third
    attempt, a connection accepted after two refusals. Real hosts cannot be
    made to fail on demand, and a test that depended on one would be slow,
    non-deterministic, and would break CI whenever the network hiccuped.

    `calls` is public because counting calls is this fake's entire purpose —
    asserting on it is how a test proves that retrying happened, or didn't.
    """

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages: dict[str, str] = pages

    async def get(self, url: str) -> Response:
        if url in self._pages:
            html = self._pages[url]
            return Response(
                url=url,
                final_url=url,
                status=200,
                elapsed_ms=10.0,
                content_type="text/html",
                text=html,
            )

        return Response(
            url=url,
            final_url=url,
            status=404,
            elapsed_ms=10.0,
            content_type="",
            text=None,
        )


class FlakyFetcher:
    """Raises FetchError `failures` times, then returns 200.

    Simulates a transport failure that recovers: DNS resolving on the third
    attempt, a connection accepted after two refusals. Real hosts cannot be
    made to fail on demand, and a test that depended on one would be slow,
    non-deterministic, and would break CI whenever the network hiccuped.

    `calls` is public because counting calls is this fake's entire purpose —
    asserting on it is how a test proves that retrying happened, or didn't.
    """

    def __init__(self, *, failures: int) -> None:
        self.calls = 0
        self._failures = failures

    async def get(self, url: str) -> Response:
        self.calls += 1
        if self.calls <= self._failures:
            raise FetchError("simulated transport failure")
        return Response(
            url=url,
            final_url=url,
            status=200,
            elapsed_ms=10.0,
            content_type="text/html",
            text="<html><body>OK</body></html>",
        )


class StatusFetcher:
    """Always returns the same status. Counts calls.

    Covers both sides of the retry decision with one class: 503 for a status
    worth retrying, 404 for one that never is.
    """

    def __init__(self, *, status: int) -> None:
        self.status = status
        self.calls = 0

    async def get(self, url: str) -> Response:
        self.calls += 1

        return Response(
            url=url,
            final_url=url,
            status=self.status,
            elapsed_ms=10.0,
            content_type="text/html",
            text="<html></html>",
        )
