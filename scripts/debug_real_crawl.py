"""Crawl a real site under asyncio's debug mode and print the report.

A manual check, not a test: it reaches a live third-party site, so it never
runs in CI. Debug mode logs every callback that holds the event loop longer
than slow_callback_duration — 20 ms here, a fifth of asyncio's default — which
is how the link-loop stall in ADR-007 was found. The crawl stays polite:
robots.txt is honoured, requests are paced at two per second, and it stops at
150 pages.

    uv run python scripts/debug_real_crawl.py
"""

import asyncio

from siteaudit.crawler import crawl
from siteaudit.fetcher import HttpxFetcher
from siteaudit.robots import RobotsPolicy


async def main() -> None:
    asyncio.get_running_loop().slow_callback_duration = 0.02

    async with HttpxFetcher(rate=2.0, max_concurrency=10, timeout=30) as fetcher:
        robots = RobotsPolicy(fetcher)
        report = await crawl(
            "https://docs.python.org/3/tutorial/",
            fetcher=fetcher,
            robots=robots,
            max_depth=2,
            max_pages=150,
            workers=10,
        )
    print(report)


if __name__ == "__main__":
    asyncio.run(main(), debug=True)
