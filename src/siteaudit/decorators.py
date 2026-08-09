import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import ParamSpec

from siteaudit.errors import FetchError
from siteaudit.models import Response

P = ParamSpec("P")


RETRY_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
"""Statuses worth trying again.

All of them mean "this might work in a moment": the server timed out, is
overloaded, is restarting, or explicitly asked us to slow down. Everything
else is a permanent fact about that URL — a 404 will not become a 200, and
retrying it just wastes budget and looks like an attack. 501 and 505 are 5xx
but stay out for the same reason.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """When to try again, and how long to wait.

    Policy as data rather than branches: a test can pass attempts=1 to disable
    retrying entirely, or base_delay=0.01 to run in milliseconds, without the
    retry logic knowing anything about it.
    """

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 10.0
    retry_statuses: frozenset[int] = RETRY_STATUSES


def jittered_backoff(attempt: int, base_delay: float, max_delay: float) -> float:
    """How long to wait before the next attempt, in seconds.

    Exponential because a server that just failed is usually overloaded, and
    retrying every 100ms makes that worse. Jittered because without it, twenty
    workers that failed at the same moment all retry at the same moment — the
    same spike, one second later.

    Full jitter, uniform across the whole window rather than a percentage
    either side of the delay, which spreads contention better and is simpler.
    """

    delay = min(base_delay * 2**attempt, max_delay)
    return random.uniform(0, delay)


def retry(
    policy: RetryPolicy,
) -> Callable[[Callable[P, Awaitable[Response]]], Callable[P, Awaitable[Response]]]:
    """Retry a fetch on transport failures and on server-side error statuses.

    Two failure modes, because get() does not raise on a bad status: a
    FetchError means the request never produced a response, while a 503 means
    it produced one that is worth asking for again. Both are retried; only the
    most recent of the two is reported.

    How it ends matters as much as how it retries:

      - success at any attempt      -> return that response immediately
      - a permanent status (404)    -> return it on the first attempt, no retry
      - out of attempts on a 503    -> return the 503, because it is a real
                                       response and the report should say so
      - out of attempts on an error -> re-raise the original exception, so the
                                       traceback still points at the real cause

    Specific to Response rather than generic: the wrapper inspects .status, so
    the return type cannot be a free TypeVar. ParamSpec keeps the wrapped
    function's arguments intact, so get(self, url) still typechecks.

    Note the sleep happens while the fetcher's semaphore is held, so a worker
    backing off holds a concurrency slot. That is deliberate — it throttles
    the crawler against a struggling host — but it is why a crawl can appear
    to stall when a site starts returning 503s.
    """

    def decorator(func: Callable[P, Awaitable[Response]]) -> Callable[P, Awaitable[Response]]:

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Response:
            last_response: Response | None = None
            last_exc: FetchError | None = None

            for attempt in range(policy.attempts):
                try:
                    response = await func(*args, **kwargs)
                    last_exc = None
                    last_response = response
                    if response.status not in policy.retry_statuses:
                        return response

                except FetchError as exc:
                    last_exc = exc
                if attempt < policy.attempts - 1:
                    await asyncio.sleep(
                        jittered_backoff(
                            attempt=attempt,
                            base_delay=policy.base_delay,
                            max_delay=policy.max_delay,
                        )
                    )

            if last_exc is not None:
                raise last_exc

            assert last_response is not None
            return last_response

        return wrapper

    return decorator
