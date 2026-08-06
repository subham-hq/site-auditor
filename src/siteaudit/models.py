from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Severity(StrEnum):
    """How serious a check finding is.

    ERROR is the only level with behavior attached: any ERROR anywhere in
    the crawl makes the process exit 1, so a build can be gated on it.
    WARNING and INFO are reported and counted but never affect the exit code.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True, kw_only=True)
class Page:
    """One fetched page, as it exists in memory during the crawl.

    A working object, not a record: it is created by the fetcher, passed to
    the checks, projected into one JSONL line, and dropped. Nothing keeps it,
    which is what holds memory flat across a crawl of any size.

    Because of that, `html` must never reach the report — build the output
    record field by field rather than with asdict(), which would serialize
    the entire body.
    """

    url: str
    status: int
    elapsed_ms: float
    depth: int
    html: str | None  # None for non-HTML responses
    final_url: str  # after redirect


@dataclass(frozen=True, slots=True, kw_only=True)
class Link:
    """One discovered edge: a link from one page to another.

    An edge, not a URL — `source_url` is what turns "404" into "404 at
    /pricing, linked from /home", which is the difference between a report
    that is readable and one that is merely correct.

    This is also the unit the frontier queues, so `depth` travels with it
    rather than being recomputed at dequeue time.
    """

    url: str  # resolved, normalized, absolute
    source_url: str  # the page it was found on
    depth: int  # source depth + 1
    text: str = ""  # anchor text — empty for <img>, bare hrefs


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResult:
    """A single finding produced by a check.

    Checks emit findings, not verdicts: a page with nothing wrong produces
    an empty sequence. Nothing records that a check passed, which is what
    keeps the report signal rather than noise.

    `subject` names the thing the finding is about when that is not the page
    itself — a link check reports on the target URL, a meta check reports on
    the page it was given and leaves this None.
    """

    check: str  # "links", "meta" — which check produced this
    severity: Severity
    message: str
    subject: str | None = None  # the specific URL/element, if not the page itself


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditReport:
    """Summary of one completed or interrupted crawl.

    Counters only. It never holds Page objects, and adding a `pages` field
    would undo the bounded-memory property the whole design rests on.

    Two fields carry more weight than they look:
      - the skipped_* counters are how a crawl that visited 4 pages of a
        400-page site looks obviously wrong instead of quietly wrong
      - `completed` distinguishes "the site is healthy" from "I stopped at
        --max-pages", which would otherwise read identically
    """

    seed_url: str
    started_at: datetime
    finished_at: datetime

    pages_crawled: int
    links_checked: int
    status_counts: Mapping[int, int]  # {200: 412, 301: 6, 404: 3}

    errors: int
    warnings: int

    skipped_off_host: int
    skipped_robots: int
    skipped_non_http: int

    completed: bool  # False if budget hit or canceled
    slowest: Sequence[tuple[str, float]]  # bounded top-N (url, ms)


@dataclass(frozen=True, slots=True, kw_only=True)
class Response:
    """One HTTP response, reduced to what the crawler actually uses.

    Deliberately not httpx.Response. The fetcher adapts the library's type
    into this one, which is what lets crawler.py depend on protocols and
    models alone and never learn which HTTP client is underneath.

    The cost is a small adapter in fetcher.py and losing access to httpx
    features not projected here. Adding a field is cheap; the seam is not.
    """

    url: str  # the URL requested, normalized
    final_url: str  # after redirects — differs from url when redirected
    status: int
    elapsed_ms: float
    content_type: str  # "" when the header is absent
    text: str | None  # None for non-HTML or oversized bodies
