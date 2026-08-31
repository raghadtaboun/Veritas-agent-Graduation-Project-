"""
tmp/verify_aggregator_source.py — Checks 2 and 3 for the aggregator source
preservation fix. Transient harness, not part of the long-lived test suite.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.env_bootstrap import bootstrap_env
bootstrap_env()

from mcp_server.server import scrape_article  # noqa: E402

LIBYAAKHBAR_URL = "https://www.libyaakhbar.com/libya-news/2794437.html"
JO24_URL = "https://jo24.net/article/566800"


async def main() -> None:
    # Check 2 — libyaakhbar
    print("=" * 72)
    print(f"CHECK 2 — libyaakhbar: {LIBYAAKHBAR_URL}")
    r = await scrape_article(LIBYAAKHBAR_URL)
    content = r.get("content") or ""
    print(f"  success: {r.get('success')}  method: {r.get('method')}  length: {len(content)}")
    print(f"  starts with 'المصدر:': {content.startswith('المصدر:')}")
    print(f"  first 200 chars:\n    {content[:200]}")

    # Check 3 — non-aggregator (jo24)
    print("=" * 72)
    print(f"CHECK 3 — jo24.net: {JO24_URL}")
    r2 = await scrape_article(JO24_URL)
    content2 = r2.get("content") or ""
    print(f"  success: {r2.get('success')}  method: {r2.get('method')}  length: {len(content2)}")
    print(f"  starts with 'المصدر:': {content2.startswith('المصدر:')}")
    print(f"  first 200 chars:\n    {content2[:200]}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
