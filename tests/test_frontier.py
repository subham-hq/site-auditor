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
