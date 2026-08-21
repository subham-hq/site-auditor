"""The Fetcher protocol seam, exercised through a fake."""

from siteaudit.protocols import Fetcher
from tests.fakes import FakeFetcher


async def test_fake_fetcher_satisfies_the_protocol() -> None:
    """A fake that never mentions Fetcher is accepted as one.

    The `fetcher: Fetcher = FakeFetcher(...)` annotation is the real assertion
    here, and mypy is what checks it: if the shape drifts — a renamed method, a
    changed return type, a sync def — this line fails at type-check time rather
    than at runtime in Phase 03.

    The rest verifies the fake behaves like the real thing on both paths: a
    known URL returns its HTML, an unknown one returns 404 with no body.
    """

    fetcher: Fetcher = FakeFetcher(
        {
            "https://example.com/page01": "<html><body>Page 01</body></html>",
        }
    )
    response_1 = await fetcher.get("https://example.com/page01")

    assert response_1.status == 200
    assert response_1.text == "<html><body>Page 01</body></html>"

    response_2 = await fetcher.get("https://example.com/missing")

    assert response_2.status == 404
    assert response_2.text is None
