"""Retry behaviour: what gets tried again, what does not, and how it ends."""

import pytest
from tests.fakes import FlakyFetcher, StatusFetcher

from siteaudit.decorators import RetryPolicy, retry
from siteaudit.errors import FetchError


async def test_retry_recovers_after_two_failures() -> None:
    """A transient failure that clears should not surface to the caller."""

    fetcher = FlakyFetcher(failures=2)
    wrapped = retry(RetryPolicy(attempts=3, base_delay=0.01))(fetcher.get)
    response = await wrapped("https://example.com/")
    assert response.status == 200
    assert fetcher.calls == 3


async def test_retry_exhausted_on_transport_error() -> None:
    """Out of attempts on a transport failure re-raises rather than returning.

    The original exception, not a fresh one, so the traceback still points at
    the cause. Three calls, not four — the count proves the loop bound holds.
    """

    fetcher = FlakyFetcher(failures=99)
    wrapped = retry(RetryPolicy(attempts=3, base_delay=0.01))(fetcher.get)
    with pytest.raises(FetchError):
        await wrapped("https://example.com/")

    assert fetcher.calls == 3


async def test_retry_exhausts_on_retriable_status_code() -> None:
    """Out of attempts on a retryable status returns it rather than raising.

    The less obvious of the two exhaustion paths, and the one a refactor would
    get backwards. A 503 after three tries is a real response and a finding the
    report should carry — raising would throw away the thing the tool exists to
    report.
    """

    fetcher = StatusFetcher(status=503)
    wrapped = retry(RetryPolicy(attempts=3, base_delay=0.01))(fetcher.get)
    response = await wrapped("https://example.com/")

    assert response.status == 503
    assert fetcher.calls == 3


async def test_permanent_status_is_not_retried() -> None:
    """A permanent status is fetched exactly once.

    The most valuable assertion in the file. A 404 will not become a 200, so
    retrying wastes budget and looks like an attack from the server's side.
    Replacing the retry_statuses check with a naive `status == 200` passes
    every other test here and fails only this one.
    """

    fetcher = StatusFetcher(status=404)

    wrapped = retry(RetryPolicy(attempts=3, base_delay=0.01))(fetcher.get)

    response = await wrapped("https://example.com/")

    assert response.status == 404
    assert fetcher.calls == 1
