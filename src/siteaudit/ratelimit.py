import asyncio
from time import monotonic


class RateLimiter:
    """Paces requests per host so a crawl stays polite.

    Separate from the fetcher's semaphore, which bounds how many requests are
    in flight. Twenty concurrent requests against a server that answers in 50ms
    is 400 requests a second — well inside the concurrency limit and still a
    small denial-of-service from the server's side. This bounds frequency
    instead.

    Enforces even spacing rather than a token bucket: same sustained rate, no
    burst allowance. A bucket lets a host that has been idle fire several
    requests at once, which is the thing that gets a crawler blocked. This also
    costs one float per host instead of four fields plus refill arithmetic.
    """

    def __init__(self, *, rate: float) -> None:
        """Pace at `rate` requests per second, per host."""

        self._interval = 1.0 / rate
        self._next_allowed: dict[str, float] = {}

    async def acquire(self, host: str) -> None:
        """Return when it is this caller's turn to send a request to `host`.

        Each caller reads the host's next free slot, claims it by pushing the
        stored time one interval forward, and then sleeps until its turn. The
        read and the write happen with no await between them, and code between
        awaits is atomic in asyncio — so two coroutines can never claim the
        same slot, and no lock is needed.

        `max(now, ...)` is what handles an idle host: a booking that has
        already passed is discarded and the request goes out immediately.
        That is why no refill logic exists — time passing is the refill.

        Keyed by host, so a slow site never throttles a fast one. Pass a
        hostname, not a URL: keying on full URLs gives every page its own
        schedule and silently disables the limiter.
        """

        now = monotonic()
        slot = max(now, self._next_allowed.get(host, now))
        self._next_allowed[host] = slot + self._interval

        if slot > now:
            await asyncio.sleep(slot - now)
