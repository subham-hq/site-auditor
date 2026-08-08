import pytest

from siteaudit.urls import is_crawlable, is_same_host, normalize_url, resolve_url

BASE = "https://e.com/docs/guide/intro.html"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # fragments are dropped — /a#intro and /a are one page
        ("https://example.com/a#section", "https://example.com/a"),
        # query parameters are sorted
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        # host is lowercased, path case is preserved
        ("HTTP://Example.COM/Path", "http://example.com/Path"),
        # default ports are dropped
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        # tracking parameters stripped, content parameters kept
        ("https://example.com/a?utm_source=x&gclid=y&page=2", "https://example.com/a?page=2"),
        # valueless flags survive — ?print is not the same page as /a
        ("https://example.com/a?flag&page=2", "https://example.com/a?flag=&page=2"),
        # dot segments resolved
        ("https://example.com/./b/../c", "https://example.com/c"),
        # doubled slashes collapsed
        ("https://example.com/a//b", "https://example.com/a/b"),
        # spaces percent-encoded
        ("https://example.com/hello world", "https://example.com/hello%20world"),
        # a missing scheme is supplied
        ("example.com/a", "https://example.com/a"),
        # an empty query is removed entirely
        ("https://example.com/a?", "https://example.com/a"),
        # trailing slashes are preserved — /a and /a/ stay distinct
        ("https://example.com/a/", "https://example.com/a/"),
        # a bare host gains a root path
        ("https://example.com", "https://example.com/"),
        # existing percent-encoding is not double-encoded
        ("https://example.com/caf%C3%A9", "https://example.com/caf%C3%A9"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/a#section",
        "https://example.com/a?b=2&a=1",
        "HTTP://Example.COM/Path",
        "https://example.com/hello world",
        "example.com/a",
    ],
)
def test_normalize_url_is_idempotent(raw: str) -> None:
    """Normalizing an already-normalised URL must not change it again."""
    once = normalize_url(raw)
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://e.com/a", True),
        ("http://e.com", True),
        ("mailto:a@b.com", False),
        ("javascript:void(0)", False),
        ("tel:+911234", False),
        ("data:text/html,x", False),
        ("ftp://e.com/f", False),
        # relative URLs have no scheme — this is why is_crawlable runs
        # after resolve_url, never before it
        ("/about", False),
        ("#section", False),
        ("", False),
    ],
)
def test_is_crawlable(raw: str, expected: bool) -> None:
    assert is_crawlable(raw) is expected


@pytest.mark.parametrize(
    ("raw_url_1", "raw_url_2", "expected"),
    [
        ("https://e.com/a", "https://e.com/b", True),
        # www and the bare domain are deliberately distinct hosts
        ("https://www.e.com/a", "https://e.com/b", False),
        # scheme is ignored
        ("http://e.com/a", "https://e.com/b", True),
        # port is ignored
        ("https://e.com:8080/a", "https://e.com/b", True),
        # hostname comparison is case-insensitive
        ("https://E.COM/a", "https://e.com/b", True),
        # subdomains are distinct
        ("https://blog.e.com/a", "https://e.com/b", False),
        # regression: both hostnames are None, and None == None must not
        # be read as "same host" — this once let mailto: links into the crawl
        ("mailto:a@b.com", "mailto:c@d.com", False),
        ("/about", "/contact", False),
    ],
)
def test_is_same_host(raw_url_1: str, raw_url_2: str, expected: bool) -> None:
    assert is_same_host(raw_url_1, raw_url_2) is expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("setup.html", "https://e.com/docs/guide/setup.html"),
        ("../api/", "https://e.com/docs/api/"),
        ("/about", "https://e.com/about"),
        # protocol-relative links inherit the base scheme
        ("//cdn.e.com/x.js", "https://cdn.e.com/x.js"),
        # an absolute href wins outright
        ("https://other.com/a", "https://other.com/a"),
        # fragment- and query-only hrefs attach to the base page
        ("#section", "https://e.com/docs/guide/intro.html#section"),
        ("?page=2", "https://e.com/docs/guide/intro.html?page=2"),
        ("", "https://e.com/docs/guide/intro.html"),
        # non-HTTP schemes pass through untouched — is_crawlable rejects them next
        ("mailto:a@b.com", "mailto:a@b.com"),
    ],
)
def test_resolve_url(href: str, expected: str) -> None:
    assert resolve_url(BASE, href) == expected
