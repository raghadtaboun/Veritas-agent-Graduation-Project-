"""Check 8 (continued) — confirm recovery via retry button when FastAPI is back."""

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

        print("→ Loading dashboard (FastAPI is now back up)")
        page.goto("http://localhost:8501", wait_until="networkidle", timeout=30000)
        time.sleep(3)
        page.wait_for_load_state("networkidle", timeout=15000)

        body = page.locator("body").inner_text()
        if "الرئيسية" in body and "أحدث الأخبار" in body:
            print("  ✓ Home page recovered automatically on fresh load")
        else:
            print("  - Home not detected on first load; trying retry click")
            try:
                page.get_by_role("button", name="إعادة المحاولة").first.click()
                time.sleep(5)
                page.wait_for_load_state("networkidle", timeout=15000)
                body = page.locator("body").inner_text()
                if "الرئيسية" in body:
                    print("  ✓ Retry click recovered the page")
                else:
                    print("  ✗ Page did not recover after retry")
                    return 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ retry click failed: {exc}")
                return 1

        shot_path = SHOT_DIR / "09_recovered_after_fastapi_restart.png"
        page.screenshot(path=str(shot_path), full_page=True)
        print(f"  ✓ screenshot → {shot_path.relative_to(ROOT)}")
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
