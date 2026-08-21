"""robots.txt policy: what gets crawled, what does not, and what a failure means.

Every case runs through a fake, so the whole policy — fetch, parse, cache and
verdict — is exercised with no network at all. That is the Fetcher protocol
paying off: RobotsPolicy takes the seam, not a concrete client.
"""

from siteaudit.robots import RobotsPolicy
from tests.fakes import FakeFetcher, FlakyFetcher, StatusFetcher


async def test_disallowed_path_is_blocked() -> None:
    """A plain Disallow blocks its path and leaves everything else alone."""

    robot = RobotsPolicy(
        fetcher=FakeFetcher(
            {"https://example.com/robots.txt": ("User-agent: *\nDisallow: /private")}
        )
    )

    private = await robot.allows("https://example.com/private")
    public = await robot.allows("https://example.com/public")

    assert private is False
    assert public is True


async def test_wildcard_rule_is_honoured() -> None:
    """A wildcard rule is matched, not treated as a literal string.

    The guard against dropping protego for the stdlib to save a dependency.
    urllib.robotparser does prefix matching only — `Disallow: /*q=` becomes a
    literal prefix nothing starts with, so this URL comes back allowed. Every
    other test in this file passes under the stdlib; only this one fails.
    """

    robot = RobotsPolicy(
        fetcher=FakeFetcher({"https://example.com/robots.txt": ("User-agent: *\nDisallow: /*q=")})
    )

    search = await robot.allows("https://example.com/search?q=x")
    about = await robot.allows("https://example.com/about")

    assert search is False
    assert about is True


async def test_missing_robots_allows_everything() -> None:
    """A 404 is a definite answer: no robots.txt exists, nothing is forbidden."""

    robot = RobotsPolicy(fetcher=FakeFetcher({}))

    private = await robot.allows("https://example.com/private")

    assert private is True


async def test_server_error_denies_everything() -> None:
    """A 5xx is not an answer, so it denies.

    Rules may well exist and could not be read. Crawling anyway would mean
    ignoring a Disallow the site owner wrote, and never knowing. The asymmetry
    with the 404 case is the decision: definite allows, ambiguous denies.
    """

    robot = RobotsPolicy(fetcher=StatusFetcher(status=500))
    public = await robot.allows("https://example.com/public")

    assert public is False


async def test_unreachable_host_denies_everything() -> None:
    """A FetchError while loading robots.txt causes crawling to fail closed."""

    fetcher = FlakyFetcher(failures=1)
    robot = RobotsPolicy(fetcher=fetcher)
    allowed = await robot.allows("https://example.com/private")

    assert allowed is False


async def test_robots_is_fetched_once_per_host() -> None:
    """Ten calls across two hosts cost two fetches, not ten.

    Two claims in one assertion: that the result is cached at all, and that the
    cache is keyed by host. Collapsing the key to a constant still gives one
    fetch for the first host and fails here.
    """

    status_fetcher = StatusFetcher(status=200)
    robot = RobotsPolicy(fetcher=status_fetcher)
    for _ in range(5):
        await robot.allows("https://a.com/private")
    for _ in range(5):
        await robot.allows("https://b.com/public")

    assert status_fetcher.calls == 2
