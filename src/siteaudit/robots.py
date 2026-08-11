from typing import Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from siteaudit.errors import FetchError
from siteaudit.protocols import Fetcher

ROBOTS_PATH = "/robots.txt"
USER_AGENT = "site-audit"


class RobotsPolicy:
    """Whether robots.txt permits a URL, cached per host for the whole crawl.

    Takes a Fetcher rather than building its own client, so robots.txt goes
    through the same connection pool, timeouts, retries and rate limiting as
    every other request — and so a FakeFetcher works in tests. Typed as the
    protocol, never the concrete fetcher.

    Parsing is delegated to the stdlib RobotFileParser: wildcards, longest-match
    precedence and Allow-overriding-Disallow are subtler than they look. Note
    it is fed with parse(), never read() — read() does its own synchronous
    urlopen, which would block the event loop for the length of the request.
    """

    def __init__(self, fetcher: Fetcher, *, user_agent: str = USER_AGENT) -> None:
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._cache: dict[str, RobotFileParser | Literal["deny"] | None] = {}

    async def allows(self, url: str) -> bool:
        """Whether this URL may be fetched.

        robots.txt is fetched once per host and cached for the rest of the run.
        The cache holds three states: a missing key means "not looked yet", a
        parser means "rules exist, ask it", and None or "deny" is a host-wide
        answer for when there are no rules to consult.

        The missing-key state is what makes the cache work. Without it, a site
        with no robots.txt would re-fetch the same 404 on every URL.
        """

        host = urlsplit(url).hostname or ""
        if host not in self._cache:
            self._cache[host] = await self._load(host)
        parser = self._cache[host]
        if parser is None:
            return True
        if parser == "deny":
            return False

        return parser.can_fetch(self._user_agent, url)

    async def _load(self, host: str) -> RobotFileParser | Literal["deny"] | None:
        """Fetch and parse one host's robots.txt.

        Returns a parser when rules exist. Otherwise, a host-wide answer: None
        allows everything, "deny" allows nothing. The split follows how
        definite the answer is.

        A 4xx is definite — there is no robots.txt, so nothing is forbidden,
        and crawling freely is exactly right. A 5xx or an unreachable host is
        not an answer at all: rules may well exist and we simply could not read
        them. Crawling anyway would mean ignoring a Disallow the site owner
        wrote, and never knowing we had. So the ambiguous cases resolve to deny
        and the definite one to allow.

        The 5xx check sits above the 200 check deliberately. Anything that is
        not 200 would otherwise be swallowed by the catch-all and quietly
        allowed, which is the opposite of the intent.

        This is a one-shot decision, not a retry: @retry has already made three
        attempts before a FetchError reaches here, and the result is cached for
        the rest of the run. A host denied at minute one stays denied.

        Known limitation: workers hitting a new host simultaneously all miss
        the cache and each fetch robots.txt. Wasteful rather than wrong, and
        bounded by concurrency.
        """

        try:
            response = await self._fetcher.get(f"https://{host}{ROBOTS_PATH}")
        except FetchError:
            return "deny"

        if response.status >= 500:
            return "deny"
        if response.status != 200:
            return None
        if response.text is None:
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser
