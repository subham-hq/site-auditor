from collections import deque
from dataclasses import replace
from enum import StrEnum

from siteaudit.models import Link
from siteaudit.urls import is_crawlable, is_same_host, normalize_url


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    OFF_HOST = "off_host"
    NON_HTTP = "non_http"
    TOO_DEEP = "too_deep"
    BUDGET_FULL = "budget_full"
    MALFORMED = "malformed"


class Frontier:
    """The crawl's work queue and its gatekeeper.

    Holds URLs waiting to be fetched and decides which ones are allowed in.
    Breadth-first: links come out in the order they were accepted, so pages
    close to the seed are crawled before deeper ones.

    Pure and synchronous by design. The decision logic is where the bugs live,
    so it is testable with no event loop and no network; Phase 03 wraps this
    in an asyncio.Queue rather than rewriting it.

    The seed URL is normalized and validated once here, which means every
    method afterward can assume it is a well-formed http or https URL.
    """

    def __init__(self, seed_url: str, *, max_depth: int, max_pages: int) -> None:
        self._seen: set[str] = set()
        self._accepted = 0
        self._pending: deque[Link] = deque()
        try:
            self._seed_url = normalize_url(seed_url)
        except ValueError as exc:
            raise ValueError(f"invalid seed URL: {seed_url!r}") from exc
        if not is_crawlable(self._seed_url):
            raise ValueError(f"seed URL must be http or https: {seed_url!r}")
        self._max_depth = max_depth
        self._max_pages = max_pages

    def add(self, link: Link) -> Verdict:
        """Offer a link to the frontier and report what was decided.

        Normalisation happens here, not in the caller: the frontier's one
        guarantee is that no URL is crawled twice, and that guarantee is only
        as good as the definition of "same URL".

        Checks run in a fixed order. The duplicate check comes first so a URL
        already decided about is never re-examined; `seen` is marked only when
        a URL is accepted. TOO_DEEP and BUDGET_FULL depend on the path taken
        and the crawl's state rather than on the URL itself, so a URL rejected
        for either may be offered again and accepted on a shorter route.

        One consequence: a URL rejected as off-host or non-HTTP is re-evaluated
        every time it is discovered, so those skip counters count link
        occurrences while `pages_crawled` counts unique pages.
        """

        try:
            normalized = normalize_url(link.url)
        except ValueError:
            return Verdict.MALFORMED
        if normalized in self._seen:
            return Verdict.DUPLICATE
        if not is_crawlable(normalized):
            return Verdict.NON_HTTP
        if not is_same_host(self._seed_url, normalized):
            return Verdict.OFF_HOST
        if link.depth > self._max_depth:
            return Verdict.TOO_DEEP
        if self._accepted >= self._max_pages:
            return Verdict.BUDGET_FULL
        self._seen.add(normalized)
        self._accepted += 1
        link = replace(link, url=normalized)
        self._pending.append(link)
        return Verdict.ACCEPTED

    def pop(self) -> Link | None:
        """Take the next link off the queue, or None if nothing is pending.

        Returns None rather than raising because an empty frontier is the
        normal end state, not an error. In Phase 03 this becomes an awaited
        queue get, which blocks until work arrives instead of returning None.
        """

        if self._pending:
            return self._pending.popleft()
        return None

    def __len__(self) -> int:
        """How many links are waiting to be crawled.

        Pending only — not URLs seen, not pages accepted. A frontier that has
        accepted 400 pages and had them all popped has length 0.

        Also makes the object falsy when empty, so `while frontier:` works.
        """

        return len(self._pending)
