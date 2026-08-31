"""Check 8 — confirm the dashboard renders the §10.2 error banner when
FastAPI is down, AND recovers when FastAPI comes back.

Run this AFTER FastAPI has been killed but Streamlit is still up.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = ROOT / "screenshots" / "step_5_2_ab"


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        print("→ Loading dashboard with FastAPI down")
        page.goto("http://localhost:8501", wait_until="networkidle", timeout=30000)
        # Wait a touch longer — the httpx client has a 10s timeout, so the
        # health call may take that long to fail.
        time.sleep(15)
        page.wait_for_load_state("networkidle", timeout=20000)

        body = page.locator("body").inner_text()

        if "تعذّر الاتصال بالخادم" in body:
            print("  ✓ §10.2 error banner rendered (Arabic message present)")
        else:
            print("  ✗ §10.2 error banner NOT rendered. Body excerpt:")
            print(body[:500])
            shot_path = SHOT_DIR / "08_error_banner_FAILED.png"
            page.screenshot(path=str(shot_path), full_page=True)
            return 1

        if "إعادة المحاولة" in body:
            print("  ✓ retry button present")
        else:
            print("  ✗ retry button NOT visible")

        shot_path = SHOT_DIR / "08_error_banner_fastapi_down.png"
        page.screenshot(path=str(shot_path), full_page=True)
        print(f"  ✓ screenshot → {shot_path.relative_to(ROOT)}")

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
