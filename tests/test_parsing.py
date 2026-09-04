"""Link extraction: what comes out of a document, and what deliberately does not."""

import pytest

from siteaudit.parsing import extract_links

BASE = "https://e.com/docs/guide/intro.html"

def test_duplicates_are_preserved() -> None:
    """The same href twice comes back twice.

    Deduplication is the frontier's guarantee, not this function's. A set()
    here would look like a tidy-up and would silently zero the DUPLICATE
    counter, which is how the report shows a heavily cross-linked site.
    """
    
    html_content = '<a href="/a">1</a><a href="/a">2</a>'
    links = extract_links(html_content, BASE)
    assert links == ["https://e.com/a", "https://e.com/a"]
    
def test_document_order_is_preserved() -> None:
    """Links come back in the order they appear, unclosed tags and all.

    Order is what makes breadth-first predictable — the frontier queues them as
    it receives them. The unclosed tags are deliberate: real HTML is malformed
    constantly, and a strict parser would return nothing here.
    """
    
    html_content = '<a href="/a">1<a href="/b">2<a href="/c">'
    links = extract_links(html_content, BASE)
    assert links == ["https://e.com/a", "https://e.com/b", "https://e.com/c"]

def test_relative_hrefs_resolve_against_the_page_they_were_found() -> None:
    """A relative href only means something next to the page it came from.

    "setup.html" is a sibling, "../api/" walks up a directory, "/about" starts
    from the root. All three appear in real HTML and all three resolve
    differently against the same base.
    """
    
    html_content = '<a href="setup.html">x</a><a href="../api/">x</a><a href="/about">x</a>'
    links = extract_links(html_content, BASE)
    assert links == ["https://e.com/docs/guide/setup.html", "https://e.com/docs/api/", "https://e.com/about"]

def test_protocol_relative_inherits_the_base_scheme() -> None:
    """A //host href takes the scheme of the page that linked to it.

    Common on CDN links. It also means the same href resolves to http or https
    depending on the page, which is where mixed-content warnings come from.
    """
    
    html_content = '<a href="//cdn.e.com/x.js">x</a>'
    links = extract_links(html_content, BASE)
    assert links == ["https://cdn.e.com/x.js"]

def test_an_absolute_href_wins_outright() -> None:
    """An href that is already absolute is returned unchanged, base ignored."""
    
    html_content = '<a href="https://other.com/a">x</a>'
    links = extract_links(html_content, BASE)
    assert links == ["https://other.com/a"]

def test_non_http_schemes_pass_through() -> None:
    """mailto: comes back unchanged. This function does not filter.

    A scheme check here would make is_crawlable in the frontier unreachable and
    the NON_HTTP counter permanently zero. Deciding what to crawl is the
    frontier's job; this one just reads the document.
    """
    
    html_content = '<a href="mailto:a@b.com">x</a>'
    links = extract_links(html_content, BASE)
    assert links == ["mailto:a@b.com"]

def test_an_href_that_points_nowhere_is_skipped() -> None:
    """Both `<a href="">` and `<a href>` match the selector and point nowhere.

    The bare attribute is the surprising one — .get("href") returns None for
    it, and passing that to urljoin raises. The truthiness guard covers both.
    """

    html_content = '<a href="">x</a><a href>x</a>'
    links = extract_links(html_content, BASE)
    assert links == []

@pytest.mark.parametrize(
    "html_content",
    [
        "<a>x</a>",
        "<p>no links",
        "",
    ],
)

def test_extract_links_without_valid_links(html_content):
    """A document with nothing to find returns an empty list, never raises.

    Three ways to have no links: an anchor without an href, a document with no
    anchors, and no document at all. A crawler meets all three, and none of
    them is an error.
    """

    assert extract_links(html_content, BASE) == []
