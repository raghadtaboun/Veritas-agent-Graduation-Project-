"""
tmp/verify_scrape_trafilatura.py — Live verification of the 4-layer
``scrape_article`` refactor. Runs scrape_article against the three
production URLs the user verified manually (jo24.net, wafa.ps,
shorouknews.com) and reports method, content length, and the four
pollution markers ("الأكثر قراءة", "قد يعجبك", "function", "var ").

Transient harness — not part of the long-lived test suite.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.env_bootstrap import bootstrap_env

bootstrap_env()

from mcp_server.server import scrape_article  # noqa: E402

_URLS: list[str] = [
    "https://jo24.net/article/566800",
    "https://www.wafa.ps/news/2026/5/15/78-%D8%B9%D8%A7%D9%85%D8%A7-%D8%B9%D9%84%D9%89-%D9%86%D9%83%D8%A8%D8%A9-%D9%81%D9%84%D8%B3%D8%B7%D9%8A%D9%86-147258",
    "https://www.shorouknews.com/news/view.aspx?cdate=15052026&id=9969d7a1-146e-4264-81fd-78782f0f6a57",
]

_POLLUTION_MARKERS: list[str] = [
    "الأكثر قراءة",
    "قد يعجبك",
    "function",
    "var ",
]


async def main() -> None:
    for url in _URLS:
        print("=" * 72)
        print(f"URL: {url}")
        result = await scrape_article(url)
        success = result.get("success")
        method = result.get("method")
        content = result.get("content") or ""
        print(f"  success: {success}")
        print(f"  method:  {method}")
        print(f"  length:  {len(content)} chars")
        if content:
            pollution_hits = [m for m in _POLLUTION_MARKERS if m in content]
            print(f"  pollution markers found: {pollution_hits or 'NONE'}")
            print(f"  preview (first 240 chars): {content[:240]}")
        else:
            print(f"  layer1_error: {result.get('layer1_error', '')}")
            print(f"  layer2_error: {result.get('layer2_error', '')}")
            print(f"  layer3_error: {result.get('layer3_error', '')}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
