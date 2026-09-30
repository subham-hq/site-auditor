"""The crawl engine: does it terminate, does it obey the frontier, does it survive failure.

Every test runs against fakes. No network, no event-loop tricks, no mocking library —
crawl() takes Fetcher and RobotsChecker as protocols, so the whole engine is exercised
from a dict of pages.

Timeouts wrap the crawls deliberately. The failure mode this engine is most likely to have
is a hang, not a wrong answer: queue.join() waits on the task_done counter, and one missed
call means an empty queue that nobody is waiting on and nobody notices. A hang without a
timeout is a frozen terminal; a hang with one is a failed test naming the line.
"""

import asyncio

import pytest

from siteaudit.crawler import crawl
from siteaudit.errors import FetchError
from siteaudit.models import Response
from tests.fakes import DenyingRobots, FakeFetcher, FakeRobots, SlowFetcher

TIMEOUT = 5.0
SEED = "https://e.com/0"


def chain(n: int) -> dict[str, str]:
    """n pages, each linking to the next. The simplest shape that reaches depth n."""
    if n < 1:
        raise ValueError(f"chain needs at least one page, got {n}")
    link_chain: dict[str, str] = {}
    for i in range(n - 1):
        link_chain[f"https://e.com/{i}"] = f'<a href="/{i + 1}">next</a>'
    link_chain[f"https://e.com/{n - 1}"] = ""

    return link_chain


def fan_out(n: int) -> dict[str, str]:
    """One page linking to n others, all at depth 1. Wide rather than deep."""
    link_fan_out: dict[str, str] = {SEED: ""}

    for i in range(1, n + 1):
        link_fan_out[SEED] += f'<a href="/{i}">{i}</a>\n'
        link_fan_out[f"https://e.com/{i}"] = ""

    return link_fan_out


# ─────────────────────────────────────────────────────────────────────────────
# Termination — the Definition of Done for Phase 03
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_fifty_page_crawl_terminates() -> None:
    """Fifty interlinked pages are crawled and the call returns.

    The headline test. Everything else here assumes termination works, and until
    this passes the engine has never actually run.

    Two things are being asserted at once and both matter. That pages_crawled is
    fifty proves the frontier and the queue agree about what work exists. That the
    call returns at all proves queue.join() unblocked and the workers were
    cancelled — without the cancel, the TaskGroup waits forever on workers that
    loop by design.
    """

    pages = chain(50)

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED, fetcher=fetcher, robots=robots, max_depth=49, max_pages=50, workers=20
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 50
    assert report.links_checked == 49
    assert report.fetch_errors == 0
    assert report.skipped_robots == 0
    assert report.skipped_off_host == 0
    assert report.skipped_non_http == 0


async def test_a_crawl_with_more_workers_than_pages_terminates() -> None:
    """Twenty workers over three pages still returns.

    Most workers never receive an item — they block on queue.get() from the moment
    they start and are only released by the cancel. If termination depended on
    workers finishing their own work, this is where it would hang.
    """

    pages = chain(3)

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=20,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 3
    assert report.completed is True


async def test_a_single_page_with_no_links_terminates() -> None:
    """The smallest possible crawl: one page, no links, one fetch."""
    pages = chain(1)

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=20,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 1
    assert report.completed is True
    assert report.links_checked == 0
    assert report.fetch_errors == 0
    assert report.skipped_robots == 0


# ─────────────────────────────────────────────────────────────────────────────
# The frontier's rules, seen from the engine
# ─────────────────────────────────────────────────────────────────────────────


async def test_depth_limit_is_respected() -> None:
    """A page deeper than max_depth is never fetched.

    The frontier decides this, but the engine is what acts on the verdict. A chain
    is the right shape here because depth is unambiguous — page n is exactly n
    hops from the seed.

    Assert the page count, not just the verdict counter: a TOO_DEEP verdict that
    still gets queued would leave the counter correct and the crawl wrong.
    """

    pages = chain(20)

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=20,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 4
    assert report.completed is True
    assert report.links_checked == 4


async def test_page_budget_caps_the_crawl() -> None:
    """max_pages stops the crawl even when more pages are reachable.

    Note it counts pages *admitted*, not pages fetched. The two are equal here
    because FakeFetcher never fails, which is exactly why a separate test with a
    failing fetcher is worth having.
    """
    pages = chain(20)

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=10,
            max_pages=3,
            workers=3,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 3
    assert report.links_checked == 3
    assert report.completed is False


async def test_off_host_links_are_not_followed() -> None:
    """A link to another host is counted and skipped, not crawled.

    The counter is the point. A crawl that silently ignores off-host links looks
    identical to one that found none, and the report is what tells them apart.
    """
    pages = {
        SEED: '<a href="/1">internal</a><a href="https://other.com/x">external</a>',
        "https://e.com/1": "",
    }

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=5,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 2
    assert report.skipped_off_host == 1
    assert report.links_checked == 2
    assert report.completed is True


async def test_a_page_linked_from_many_others_is_fetched_once() -> None:
    """Deduplication holds across concurrent workers.

    Several workers discovering the same URL at the same moment each offer it to
    the frontier, and exactly one should get ACCEPTED. This is the closest thing
    to a race condition the engine has, and it is safe only because add() does its
    check-and-mark with no await in between.
    """
    pages = {
        SEED: ('<a href="/1">1</a><a href="/2">2</a><a href="/3">3</a>'),
        "https://e.com/1": '<a href="/shared">shared</a>',
        "https://e.com/2": '<a href="/shared">shared</a>',
        "https://e.com/3": '<a href="/shared">shared</a>',
        "https://e.com/shared": "",
    }

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=20,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 5
    assert report.completed is True


# ─────────────────────────────────────────────────────────────────────────────
# Failure — one bad page must not end the crawl
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_missing_page_does_not_stop_the_crawl() -> None:
    """A 404 in the middle of a crawl is recorded and stepped over.

    Broken links are the thing this tool exists to find, so a 404 must be an
    ordinary outcome rather than an interruption. Assert that the pages after it
    were still fetched.
    """
    pages = {
        SEED: ('<a href="/ok-1">ok-1</a><a href="/missing">missing</a><a href="/ok-2">ok-2</a>'),
        "https://e.com/ok-1": "",
        "https://e.com/ok-2": "",
    }

    fetcher = FakeFetcher(pages)
    robots = FakeRobots()

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=fetcher,
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=5,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 4
    assert report.status_counts[404] == 1
    assert report.status_counts[200] == 3
    assert report.completed is True


async def test_a_transport_failure_does_not_stop_the_crawl() -> None:
    """A FetchError is counted and the worker carries on.

    The dangerous version of this bug is not a crash — it is a worker that dies
    quietly and takes a concurrency slot with it. With enough failures the crawl
    slows to nothing and then hangs, having reported no error at all.

    Needs a fetcher that fails for one URL and succeeds for the rest.
    """
    pages = {
        SEED: ('<a href="/ok-1">ok-1</a><a href="/broken">broken</a><a href="/ok-2">ok-2</a>'),
        "https://e.com/ok-1": "",
        "https://e.com/ok-2": "",
    }

    robots = FakeRobots()

    base_fetcher = FakeFetcher(pages)

    class FailingFetcher:
        async def get(self, url: str) -> Response:
            if url == "https://e.com/broken":
                raise FetchError("simulated transport failure")
            return await base_fetcher.get(url)

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=FailingFetcher(),
            robots=robots,
            max_depth=3,
            max_pages=20,
            workers=5,
        ),
        timeout=TIMEOUT,
    )

    # seed + two healthy children
    assert report.pages_crawled == 3

    # /broken failed at transport level
    assert report.fetch_errors == 1

    # The failure did not kill the crawl.
    assert report.status_counts[200] == 3
    assert report.completed is True


async def test_robots_denial_is_counted_and_skipped() -> None:
    """A URL robots disallows is skipped without being fetched.

    skipped_robots is the one skip counter the engine owns rather than the
    frontier. Robots costs a request, so it is consulted in the worker, after
    the frontier has already accepted the URL and put it on the queue.

    That ordering has a consequence worth knowing: the denied URL still
    consumed a slot of max_pages, because the frontier counted it as accepted
    before anyone asked robots. links_checked is 2 for the same reason — both
    links were offered and both came back ACCEPTED.
    """

    pages = {
        SEED: '<a href="/allowed">allowed</a><a href="/denied">denied</a>',
        "https://e.com/allowed": "",
        "https://e.com/denied": "",
    }

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=FakeFetcher(pages),
            robots=DenyingRobots({"https://e.com/denied"}),
            max_depth=3,
            max_pages=20,
            workers=5,
        ),
        timeout=TIMEOUT,
    )

    assert report.pages_crawled == 2
    assert report.skipped_robots == 1
    assert report.links_checked == 2
    assert report.status_counts == {200: 2}
    assert report.completed is True


# ─────────────────────────────────────────────────────────────────────────────
# The report
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_seed_is_rejected_loudly() -> None:
    """A seed the frontier will not accept raises rather than returning a report.

    Two reachable ways to get there: a budget of zero, and a negative depth
    limit. Both mean the caller passed something meaningless, and both would
    otherwise produce a clean report over zero pages — indistinguishable from a
    healthy crawl of an empty site.

    The verdict is in the message, so "budget_full" points straight at
    max_pages rather than leaving you to guess which argument was wrong.
    """

    with pytest.raises(ValueError):
        await crawl(
            seed_url=SEED,
            fetcher=FakeFetcher({SEED: ""}),
            robots=FakeRobots(),
            max_depth=3,
            max_pages=0,
            workers=2,
        )

    with pytest.raises(ValueError):
        await crawl(
            seed_url=SEED,
            fetcher=FakeFetcher({SEED: ""}),
            robots=FakeRobots(),
            max_depth=-1,
            max_pages=10,
            workers=2,
        )


async def test_status_counts_reflect_what_was_fetched() -> None:
    """Every fetched page appears exactly once in status_counts.

    Three pages exist and one link points at nothing, so the counts have to add
    up to pages_crawled — four fetches, three of them 200 and one 404. If the
    two ever disagree, a page was fetched without being recorded or recorded
    without being fetched, and the report is lying about a crawl that ran fine.
    """

    pages = {
        SEED: '<a href="/1">1</a><a href="/2">2</a><a href="/missing">missing</a>',
        "https://e.com/1": "",
        "https://e.com/2": "",
    }

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=FakeFetcher(pages),
            robots=FakeRobots(),
            max_depth=3,
            max_pages=20,
            workers=5,
        ),
        timeout=TIMEOUT,
    )

    assert report.status_counts == {200: 3, 404: 1}
    assert sum(report.status_counts.values()) == report.pages_crawled
    assert report.links_checked == 3
    assert report.completed is True


async def test_the_report_is_not_mutable_through_its_collections() -> None:
    """A finished report cannot be rewritten by whoever holds it.

    frozen=True stops the attribute being rebound; it says nothing about the
    dict the attribute points at. Without a copy on the way out, the report
    shares its counters with the crawl that produced them, and anyone holding
    either reference can edit a completed audit.

    Asserting on the exceptions rather than the types is deliberate — it is the
    immutability that matters, not which particular wrapper delivers it.
    """

    report = await asyncio.wait_for(
        crawl(
            seed_url=SEED,
            fetcher=FakeFetcher({SEED: ""}),
            robots=FakeRobots(),
            max_depth=3,
            max_pages=20,
            workers=2,
        ),
        timeout=TIMEOUT,
    )

    with pytest.raises(TypeError):
        report.status_counts[999] = 1  # type: ignore[index]

    with pytest.raises(AttributeError):
        report.slowest.append(("https://e.com/x", 1.0))  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────────────
# Cancellation
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_cancelled_crawl_unwinds_without_hanging() -> None:
    """Cancelling a crawl mid-flight raises and leaves nothing running.

    CancelledError is allowed to propagate rather than being caught and turned
    into a partial report. Swallowing it would mean anything wrapping crawl()
    in a timeout could no longer tell whether its own timeout fired, and that
    is a worse trade than losing counters the caller never sees anyway.

    Partial output is a Phase 05 concern, not this one: once the JSONL writer
    streams records to disk as they arrive, an interrupted crawl leaves its
    completed pages behind for free.

    The timeout is the real assertion. A crawl that hangs on cancellation --
    workers stuck on queue.get(), or a TaskGroup waiting on tasks that never
    finish -- fails here with TimeoutError instead of freezing the suite.
    """

    pages = {f"https://e.com/{i}": f'<a href="/{i + 1}">next</a>' for i in range(200)}

    task = asyncio.create_task(
        crawl(
            seed_url=SEED,
            fetcher=SlowFetcher(pages),
            robots=FakeRobots(),
            max_depth=200,
            max_pages=200,
            workers=10,
        )
    )
    await asyncio.sleep(0.15)
    assert not task.done(), "the crawl finished before it could be cancelled"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=TIMEOUT)

    assert task.cancelled()


async def test_a_cancelled_crawl_leaves_no_orphaned_workers() -> None:
    """No worker outlives the crawl that created it.

    The TaskGroup owns every worker, so cancelling the crawl cancels them with
    it. Without that ownership a cancelled crawl would leave ten coroutines
    blocked on queue.get() forever, and Python would only mention it at
    interpreter shutdown with "Task was destroyed but it is pending".
    """

    pages = {f"https://e.com/{i}": f'<a href="/{i + 1}">next</a>' for i in range(200)}
    before = len(asyncio.all_tasks())

    task = asyncio.create_task(
        crawl(
            seed_url=SEED,
            fetcher=SlowFetcher(pages),
            robots=FakeRobots(),
            max_depth=200,
            max_pages=200,
            workers=10,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=TIMEOUT)

    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == before