"""Pure and picklable: module-level functions, no closures, no imports from this package.
A process pool has to send them across a process boundary, and anything holding a reference
to self or to the wider module tree fails at runtime rather than at type-check time."""

from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser


def extract_links(html: str, base_url: str) -> list[str]:
    """Every href in the document, resolved against base_url.

    Absolute URLs, in document order, duplicates included. This function does
    not filter: mailto:, javascript: and off-host links all come back. Deciding
    what is worth crawling belongs to the frontier, and keeping the two apart
    is what lets each be tested without the other.

    An href that is empty or valueless is skipped — `<a href>` and `<a href="">`
    both match the selector but neither points anywhere.

    An href urljoin cannot parse — an unbalanced IPv6 bracket, say — comes back
    exactly as written instead of raising. The frontier parses it with the same
    urlsplit, fails the same way, and counts it MALFORMED: one bad link can
    neither end the crawl nor vanish from the counts.

    Only `<a href>` for now. Checking `<img src>` and `<script src>` is what
    the mixed-content check will need, and a `<base href>` in the document
    should override base_url but currently does not.
    """

    parser = LexborHTMLParser(html)

    links: list[str] = []

    for node in parser.css("a[href]"):
        relative_url = node.attributes.get("href")
        if not relative_url:
            continue
        try:
            links.append(urljoin(base_url, relative_url))
        except ValueError:
            links.append(relative_url)

    return links
