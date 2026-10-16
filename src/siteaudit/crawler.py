"""The crawl engine. Imports protocols and models, nothing concrete."""

import asyncio
import heapq
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from siteaudit.errors import FetchError
from siteaudit.frontier import Frontier, Verdict
from siteaudit.models import AuditReport, Link
from siteaudit.parsing import extract_links
from siteaudit.protocols import Fetcher, RobotsChecker

# How many links a worker processes before handing the event loop back. Each
# frontier.add costs tens of microseconds, almost all of it in normalize_url,
# so a chunk holds the loop for a few milliseconds rather than a whole page's
# worth. See ADR-007.
_LINKS_PER_YIELD = 100

# How many of the slowest pages the report names. The engine keeps exactly this
# many timings in a heap, however many pages it crawls.
_SLOWEST_KEPT = 10


@dataclass(slots=True)
class _Stats:
    """Mutable counters, shared by every worker. Never exported.

    Safe without a lock because asyncio is single-threaded and none of these
    increments span an await — the read and the write happen in one
    uninterrupted step. That guarantee is the reason this can be a plain
    dataclass rather than something synchronised.
    """

    pages_crawled: int = 0
    links_checked: int = 0
    status_counts: Counter[int] = field(default_factory=Counter)
    verdicts: Counter[Verdict] = field(default_factory=Counter)
    skipped_robots: int = 0
    fetch_errors: int = 0
    slowest: list[tuple[float, str]] = field(default_factory=list)  # min-heap of (ms, url)


async def crawl(
    seed_url: str,
    *,
    fetcher: Fetcher,
    robots: RobotsChecker,
    max_depth: int = 3,
    max_pages: int = 500,
    workers: int = 20,
) -> AuditReport:
    """Crawl one host from a seed URL and return a summary of what was found.

    Takes its collaborators as protocols rather than constructing them, which
    is what lets the whole engine run against fakes with no network. cli.py is
    the only place real implementations are built.

    Two structures share the work and neither is redundant. The Frontier is the
    gatekeeper: it normalises, deduplicates, and enforces depth and budget,
    returning a Verdict for every link offered. The asyncio.Queue is the work
    distribution: it hands accepted links to workers and tracks completion,
    which a plain deque cannot do.

    Termination is the interesting part. queue.join() waits on one counter —
    every put increments it, every task_done decrements it — so it answers
    "has all the work been marked done", not "are the workers finished". The
    workers loop forever by design, because an empty queue mid-crawl only means
    other workers are still discovering links. They are cancelled explicitly
    once join() returns.

    Raises ValueError if the seed cannot be crawled. The frontier can only
    refuse a seed for budget or depth, both of which mean the caller passed
    something meaningless — returning an empty report would look like a clean
    crawl of an empty site.
    """

    start_time = datetime.now(UTC)
    stats = _Stats()

    frontier = Frontier(seed_url, max_depth=max_depth, max_pages=max_pages)
    queue: asyncio.Queue[Link] = asyncio.Queue()
    seed = Link(url=seed_url, source_url="", depth=0)
    verdict, accepted_seed = frontier.add(seed)
    if accepted_seed is None:
        raise ValueError(f"seed URL was rejected by the frontier: {verdict}")
    queue.put_nowait(accepted_seed)

    async with asyncio.TaskGroup() as tg:
        worker_tasks = [
            tg.create_task(_worker(queue, frontier, fetcher, robots, stats)) for _ in range(workers)
        ]
        await queue.join()
        for task in worker_tasks:
            task.cancel()

    return _build_report(
        seed_url,
        start_time,
        datetime.now(UTC),
        stats,
        completed=stats.verdicts[Verdict.BUDGET_FULL] == 0,
    )


async def _worker(
    queue: asyncio.Queue[Link],
    frontier: Frontier,
    fetcher: Fetcher,
    robots: RobotsChecker,
    stats: _Stats,
) -> None:
    """Take links off the queue and crawl them until cancelled.

    Every exit path from the body is a `continue`, and task_done() sits in a
    finally below them all. That placement is load-bearing: queue.join() waits
    on the task_done counter, so a single missed call means the crawl hangs on
    an empty queue with no output and no traceback.

    Links discovered here go back to the frontier rather than straight to the
    queue. Only an accepted link comes back non-None, and that is what gets
    enqueued — the frontier decides, this loop just carries.

    The link loop yields every _LINKS_PER_YIELD links. A page with thousands of
    links would otherwise be processed as one uninterrupted step, and while it
    runs nothing else does: no other worker, no socket callback, no timer. The
    rate limiter depends on those timers firing on time — a stall longer than
    its interval sends two requests to the same host at once.

    A robots denial, a fetch failure and a non-HTML body are all ordinary
    outcomes, counted and stepped over. Nothing here should end the crawl.
    """

    while True:
        link = await queue.get()
        try:
            if not await robots.allows(link.url):
                stats.skipped_robots += 1
                continue
            try:
                response = await fetcher.get(link.url)
                stats.pages_crawled += 1
                stats.status_counts[response.status] += 1
                _record_timing(stats.slowest, response.final_url, response.elapsed_ms)
                if response.text is None:
                    continue
            except FetchError:
                stats.fetch_errors += 1
                continue

            hrefs = extract_links(response.text, response.final_url)
            for index, href in enumerate(hrefs):
                if index % _LINKS_PER_YIELD == 0:
                    await asyncio.sleep(0)
                candidate_link = Link(url=href, source_url=response.final_url, depth=link.depth + 1)
                verdict, accepted_link = frontier.add(candidate_link)
                stats.links_checked += 1
                stats.verdicts[verdict] += 1
                if accepted_link is not None:
                    queue.put_nowait(accepted_link)
        finally:
            queue.task_done()


def _build_report(
    seed_url: str,
    start_time: datetime,
    end_time: datetime,
    stats: _Stats,
    *,
    completed: bool,
) -> AuditReport:
    """Project the mutable counters into the frozen report.

    The one place the two representations meet. _Stats is mutable because
    twenty workers increment it; AuditReport is frozen because it is the
    result. Mappings and sequences are copied on the way across — frozen only
    stops the attribute being rebound, not the dict underneath it being
    mutated through the original reference.
    """

    return AuditReport(
        seed_url=seed_url,
        started_at=start_time,
        finished_at=end_time,
        pages_crawled=stats.pages_crawled,
        links_checked=stats.links_checked,
        status_counts=MappingProxyType(dict(stats.status_counts)),
        errors=0,
        warnings=0,
        fetch_errors=stats.fetch_errors,
        skipped_off_host=stats.verdicts[Verdict.OFF_HOST],
        skipped_robots=stats.skipped_robots,
        skipped_non_http=stats.verdicts[Verdict.NON_HTTP],
        completed=completed,
        slowest=tuple((url, ms) for ms, url in sorted(stats.slowest, reverse=True)),
    )


def _record_timing(slowest: list[tuple[float, str]], url: str, elapsed_ms: float) -> None:
    """Keep the _SLOWEST_KEPT slowest pages seen so far, in a min-heap.

    The fastest of the slow sits at the root, so each new timing either
    displaces it or is discarded on the spot. Appending every timing and sorting
    at the end would hold one entry per page for the whole crawl — the growing
    list AuditReport's bounded-memory rule exists to prevent.
    """

    entry = (elapsed_ms, url)
    if len(slowest) < _SLOWEST_KEPT:
        heapq.heappush(slowest, entry)
    else:
        heapq.heappushpop(slowest, entry)
