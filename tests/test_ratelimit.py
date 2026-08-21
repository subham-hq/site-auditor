"""Rate limiting: how often requests go out, per host.

Timing tests, so every assertion is a lower bound on delay or a generous upper
bound on total. A sleep can overshoot on a loaded runner; it cannot undershoot.
An assertion like `gap < 0.22` would flake in CI, `gap >= 0.18` will not.
"""

import asyncio
import time
from itertools import pairwise

import pytest

from siteaudit.ratelimit import RateLimiter


async def test_requests_to_one_host_are_spaced() -> None:
    """Eight concurrent requests to one host go out one interval apart.

    Concurrency and frequency are different limits. Eight coroutines can all
    be in flight at once as far as the semaphore is concerned; this is what
    stops them all reaching the server at once.
    """

    rate = 5.0
    limiter = RateLimiter(rate=rate)

    async def acquire_and_record() -> float:
        await limiter.acquire("x.com")
        return time.perf_counter()

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(acquire_and_record()) for _ in range(8)]

    timestamps = [task.result() for task in tasks]
    timestamps.sort()

    for previous, current in pairwise(timestamps):
        gap = current - previous
        assert gap >= 0.9 * (1 / rate)


async def test_hosts_are_limited_independently() -> None:
    """A slow host does not throttle a fast one.

    Eight requests sharing one schedule would take seven intervals. Split
    across two hosts it is three each, so the bound is loose by more than
    double — which is why an upper bound is safe here. Keying on the host is
    the only thing making that true, and replacing the key with a constant
    fails this test.
    """

    rate = 5.0
    limiter = RateLimiter(rate=rate)

    start = time.perf_counter()

    async with asyncio.TaskGroup() as tg:
        for _ in range(4):
            tg.create_task(limiter.acquire("a.com"))
            tg.create_task(limiter.acquire("b.com"))

    total = time.perf_counter() - start

    assert total < 7 * (1 / rate)


async def test_idle_host_is_not_delayed() -> None:
    """A booking that has already passed costs nothing.

    This is why there is no refill logic. max(now, slot) discards a stale
    booking, so an idle host resumes immediately — time passing is the refill,
    and there is no balance to credit back.
    """

    rate = 5.0
    interval = 1 / rate
    limiter = RateLimiter(rate=rate)

    await limiter.acquire("x.com")
    await asyncio.sleep(interval * 2)
    start = time.perf_counter()
    await limiter.acquire("x.com")
    elapsed = time.perf_counter() - start

    assert elapsed < interval * 0.5


def test_zero_rate_is_rejected() -> None:
    """A rate of zero means no requests are ever allowed, which is not a limiter.

    Without the guard, 1.0 / rate raises ZeroDivisionError from the constructor
    — technically loud, but it names arithmetic rather than the argument, and a
    negative rate would silently produce a negative interval and no limiting
    at all.
    """

    with pytest.raises(ValueError):
        RateLimiter(rate=0)


def test_negative_rate_is_rejected() -> None:
    """A negative rate fails silently rather than loudly, which is worse.

    rate=0 at least raises ZeroDivisionError. rate=-1 produces a negative
    interval, so every slot is in the past, no sleep ever happens, and the
    limiter is decorative — the same shape as passing a URL where a host
    belongs.
    """

    with pytest.raises(ValueError):
        RateLimiter(rate=-1.0)
