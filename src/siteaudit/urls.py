from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from url_normalize import url_normalize

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}

SUPPORTED_SCHEMES = {"http", "https"}


def resolve_url(base_url: str, relative_url: str) -> str:
    """Turn a link found in a page into an absolute URL.

    HTML links are usually relative — "setup.html", "../api/", "/about",
    "//cdn.example.com/x.js" — and only mean something next to the page they
    were found on. Resolution against the base URL happens first in the
    pipeline, before normalization or any of the filters, because those all
    assume an absolute URL.

    Non-HTTP schemes such as mailto: pass through unchanged; is_crawlable
    rejects them at the next step.
    """

    return urljoin(base_url, relative_url)


def normalize_url(url: str) -> str:
    """Return the canonical form of a URL for deduplication.

    Two URLs that normalise to the same string are treated as the same page,
    so this function defines what "same page" means for the whole crawler.

    Policy:
      - RFC 3986 normalization is delegated to url-normalize
      - Fragments are dropped: /a#intro and /a are one page
      - Query parameters are sorted, so ?a=1&b=2 and ?b=2&a=1 are one page
      - Tracking parameters are stripped; content parameters are preserved
      - Trailing slashes are preserved: /a and /a/ are distinct, and any
        redirect between them is caught later via the response's final URL
    """

    normalized_url = url_normalize(url)
    if normalized_url is None:
        raise ValueError(f"could not normalise URL: {url!r}")
    parts = urlsplit(normalized_url)
    query_params = parse_qsl(parts.query, keep_blank_values=True)
    untracked_params = [(k, v) for k, v in query_params if k not in TRACKING_PARAMS]
    encoded_query = urlencode(sorted(untracked_params), quote_via=quote)
    return urlunsplit(parts._replace(query=encoded_query, fragment=""))


def is_crawlable(url: str) -> bool:
    """Whether this URL is worth fetching. Call only on absolute URLs —
    relative paths have no scheme and are rejected."""

    return urlsplit(url).scheme in SUPPORTED_SCHEMES


def is_same_host(url_a: str, url_b: str) -> bool:
    """Whether two URLs belong to the same host, bounding the crawl.

    Compares hostnames only. Ports and schemes are ignored, so http and https
    on one host count as the same host — the redirect between them shows up in
    the response's final URL instead.

    Subdomains are treated as distinct hosts: www.example.com is not
    example.com. This is the strict reading, and it is deliberate — merging
    them would be a guess about the site's structure, and wrong for any site
    where subdomains serve genuinely different content.

    The cost is that a seed of example.com will not follow internal links to
    www.example.com. Mitigated by comparing against the seed's *final* URL
    after redirects, since a site that canonicalises to www will redirect
    there on the first request.
    """
    host_a = urlsplit(url_a).hostname
    host_b = urlsplit(url_b).hostname

    if host_a is None or host_b is None:
        return False
    else:
        return host_a == host_b
