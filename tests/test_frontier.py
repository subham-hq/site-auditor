from siteaudit.frontier import Frontier, Verdict
from siteaudit.models import Link


def test_rebind_seed_follows_the_redirect() -> None:
    f = Frontier("https://x.com/", max_depth=3, max_pages=10)
    f.rebind_seed("https://www.x.com/")
    verdict = f.add(Link(url="https://www.x.com/about", source_url="https://www.x.com/", depth=1))
    assert verdict is Verdict.ACCEPTED
