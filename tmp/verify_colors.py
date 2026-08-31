"""Extract Plotly bar fill colors on the Statistics page and verify the
Step 5.2c color-palette consistency requirement (Check 6).

The earlier broad selector `g.points > path` failed because Plotly v5
wraps bar paths in an additional `<g class="point">` layer:
  g.points > g.point > path
"""
from __future__ import annotations

import re
import sys

from playwright.sync_api import sync_playwright

DASHBOARD = "http://localhost:8501"
EXPECTED = {"#EF4444", "#3B82F6", "#F59E0B", "#9CA3AF", "#8B5CF6", "#1E5BFF"}


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
        page.goto(DASHBOARD, wait_until="networkidle", timeout=30000)
        page.wait_for_selector("h1", timeout=20000)
        page.locator("label:has-text('📊 الإحصائيات')").first.click()
        page.wait_for_load_state("networkidle", timeout=20000)
        import time
        time.sleep(3.0)

        fills = page.evaluate("""() => {
            const paths = Array.from(document.querySelectorAll('g.points path, g.barlayer path'));
            return paths.map(p => p.getAttribute('style') || '');
        }""")
        rgbs = set()
        for s in fills:
            m = re.search(r"fill:\s*rgb\((\d+),\s*(\d+),\s*(\d+)\)", s)
            if m:
                r, g, b = map(int, m.groups())
                rgbs.add(f"#{r:02X}{g:02X}{b:02X}")
        print(f"Total bar paths examined: {len(fills)}")
        print(f"Distinct hex fills observed: {sorted(rgbs)}")
        seen_expected = EXPECTED & rgbs
        print(f"Match against expected BIAS_COLORS / accent: {sorted(seen_expected)}")
        browser.close()
        # We require at least 3 of the 6 expected colors on the page
        # (pan_arab + pro_government + neutral + amber + purple + project blue).
        if len(seen_expected) >= 3:
            print(f"✓ Check 6 PASS — {len(seen_expected)} matching colors found")
            return 0
        print(f"✗ Check 6 FAIL — only {len(seen_expected)} matching colors found")
        return 1


if __name__ == "__main__":
    sys.exit(main())
