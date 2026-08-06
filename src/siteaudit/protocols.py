from collections.abc import Sequence
from typing import Protocol

from siteaudit.models import CheckResult, Page, Response


class Fetcher(Protocol):
    """Anything that can fetch a URL over the network.

    The seam that lets crawler.py run without a network: the real fetcher
    wraps httpx, and tests pass a fake built from a dict. Neither inherits
    from this — matching the shape is enough.
    """

    async def get(self, url: str) -> Response: ...


class Check(Protocol):
    """A rule applied to a fetched page, producing zero or more findings.

    Sync by design: checks are pure CPU work over already-parsed data, which
    is what lets them run inside the process pool alongside parsing.

    `name` must be a plain class attribute, not ClassVar — a Protocol member
    declared as an instance variable will not accept a ClassVar override.
    """

    name: str

    def run(self, page: Page) -> Sequence[CheckResult]: ...
