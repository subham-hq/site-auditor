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


def _validated_seed(url: str) -> str:
    """Normalize a seed URL and reject anything the crawler cannot use."""
    try:
        normalized = normalize_url(url)
    except ValueError as exc:
        raise ValueError(f"invalid seed URL: {url!r}") from exc
    if not is_crawlable(normalized):
        raise ValueError(f"seed URL must be http or https: {url!r}")
    return normalized


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
        self._seed_url = _validated_seed(seed_url)
        self._max_depth = max_depth
        self._max_pages = max_pages

    def rebind_seed(self, final_url: str) -> None:  # new — here
        """Re-point the host check at the seed's final URL after redirects.

        The seed is validated at construction from what the user typed, but a
        site that canonicalises to www will redirect on the first request, and
        from then on every internal link is on the redirected host. Call this
        once, after the seed fetch and before any link is added.
        """

        if self._seen:
            raise RuntimeError("rebind_seed must be called before any links are added")
        self._seed_url = _validated_seed(final_url)

    def add(self, link: Link) -> tuple[Verdict, Link | None]:
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
            return Verdict.MALFORMED, None
        if normalized in self._seen:
            return Verdict.DUPLICATE, None
        if not is_crawlable(normalized):
            return Verdict.NON_HTTP, None
        if not is_same_host(self._seed_url, normalized):
            return Verdict.OFF_HOST, None
        if link.depth > self._max_depth:
            return Verdict.TOO_DEEP, None
        if self._accepted >= self._max_pages:
            return Verdict.BUDGET_FULL, None
        self._seen.add(normalized)
        self._accepted += 1
        link = replace(link, url=normalized)
        return Verdict.ACCEPTED, link
