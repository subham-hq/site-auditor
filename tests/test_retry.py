"""Retry behaviour: what gets tried again, what does not, and how it ends."""

import pytest

from siteaudit.decorators import RetryPolicy, jittered_backoff, retry
from siteaudit.errors import FetchError
from tests.fakes import FlakyFetcher, StatusFetcher


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


def test_backoff_never_exceeds_max_delay() -> None:
    """The exponential growth stays inside the cap at every attempt.

    Delete the min() and the first few attempts still look reasonable — attempt
    3 is 4 seconds either way. Attempt 10 is 512 seconds and attempt 20 is 145
    hours, which is a crawler that appears to hang rather than one that fails.
    Nothing else in this suite reaches an attempt high enough to notice.

    Sampled repeatedly per attempt because the delay is random: one draw could
    land under the cap by luck.
    """

    for attempt in range(30):
        for _ in range(50):
            assert 0 <= jittered_backoff(attempt, 0.5, 10.0) <= 10.0


def test_backoff_is_jittered() -> None:
    """Two calls at the same attempt should not return the same delay."""
    delays = {jittered_backoff(3, 0.5, 10.0) for _ in range(50)}
    assert len(delays) > 1


def test_zero_attempts_is_rejected() -> None:
    """A policy that would never call the wrapped function is refused at construction.

    Without the guard, attempts=0 skips the loop entirely and the wrapper falls
    through to `assert last_response is not None` — an AssertionError with an
    empty message, raised from inside a decorator, several frames from whoever
    passed the zero. A --retries 0 flag would produce exactly that.

    Validating where the value enters turns it into a message naming the field
    and the value it got.
    """

    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)
