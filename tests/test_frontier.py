import pytest

from siteaudit.frontier import Frontier, Verdict
from siteaudit.models import Link


def test_rebind_seed_follows_the_redirect() -> None:
    f = Frontier("https://x.com/", max_depth=3, max_pages=10)
    f.rebind_seed("https://www.x.com/")

    link = Link(
        url="https://www.x.com/about",
        source_url="https://www.x.com/",
        depth=1,
    )

    verdict, accepted_link = f.add(link=link)

    assert verdict is Verdict.ACCEPTED
    assert accepted_link is not None
    assert accepted_link.url == "https://www.x.com/about"


def seeded(*, max_pages: int = 10) -> Frontier:
    """A frontier for https://x.com/ with the seed already accepted."""

    frontier = Frontier("https://x.com/", max_depth=3, max_pages=max_pages)
    frontier.add(Link(url="https://x.com/", source_url="", depth=0))
    return frontier


@pytest.mark.parametrize(
    ("url", "depth", "expected"),
    [
        ("https://x.com/a", 1, Verdict.ACCEPTED),
        ("https://x.com/", 1, Verdict.DUPLICATE),
        ("https://X.com/#top", 1, Verdict.DUPLICATE),
        ("https://other.com/", 1, Verdict.OFF_HOST),
        ("https://www.x.com/", 1, Verdict.OFF_HOST),
        ("mailto:a@x.com", 1, Verdict.NON_HTTP),
        ("https://x.com/deep", 4, Verdict.TOO_DEEP),
        ("http://[::1", 1, Verdict.MALFORMED),
    ],
)
def test_each_rule_has_its_verdict(url: str, depth: int, expected: Verdict) -> None:
    """Every way a link can be judged, one case each.

    Only ACCEPTED hands a link back. The duplicates are the interesting rows:
    the seed itself, and the seed again with an uppercase host and a fragment —
    the same page once normalised, which is the whole point of normalising.
    """

    verdict, accepted = seeded().add(Link(url=url, source_url="https://x.com/", depth=depth))
    assert verdict is expected
    assert (accepted is not None) is (expected is Verdict.ACCEPTED)


def test_the_page_budget_counts_the_seed() -> None:
    """max_pages=2 admits the seed and one more page, then refuses."""

    frontier = seeded(max_pages=2)
    first, _ = frontier.add(Link(url="https://x.com/a", source_url="https://x.com/", depth=1))
    second, _ = frontier.add(Link(url="https://x.com/b", source_url="https://x.com/", depth=1))
    assert (first, second) == (Verdict.ACCEPTED, Verdict.BUDGET_FULL)


def test_rebind_seed_works_once_the_seed_is_in() -> None:
    """The crawler rebinds after the seed is accepted and fetched, not before.

    The seed redirected to www, so www links are in scope from here on. The
    final URL counts as seen: the home page's link back to itself is a
    duplicate, not a second fetch.
    """

    frontier = seeded()
    frontier.rebind_seed("https://www.x.com/")

    about, _ = frontier.add(
        Link(url="https://www.x.com/about", source_url="https://www.x.com/", depth=1)
    )
    home, _ = frontier.add(Link(url="https://www.x.com/", source_url="https://www.x.com/", depth=1))
    assert (about, home) == (Verdict.ACCEPTED, Verdict.DUPLICATE)


def test_rebind_seed_is_refused_once_the_crawl_has_moved_on() -> None:
    """After a second page is accepted the crawl has committed to a host."""

    frontier = seeded()
    frontier.add(Link(url="https://x.com/a", source_url="https://x.com/", depth=1))

    with pytest.raises(RuntimeError):
        frontier.rebind_seed("https://www.x.com/")
