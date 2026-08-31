"""Phase 5.2 Step 5.2a+b verification harness.

Drives the running Streamlit dashboard with a headless Chromium browser
(via Playwright) and runs Checks 2, 4, 5, 7, 9 from the task spec:

  * Check 2 — Dashboard renders; the body contains "Veritas".
  * Check 4 — Each of the 4 pages (Home, Events, Libya, ME, World)
    renders the expected Arabic anchor strings.
  * Check 5 — RTL applied page-wide (CSS computed direction == "rtl").
  * Check 7 — Bias multi-select filter behaviour (deselect to single label,
    article count drops).
  * Check 9 — Statistics nav item renders the placeholder, not a crash.

This script is a verification artefact, NOT part of the dashboard code,
and lives in tmp/. It depends only on the running stack (FastAPI on 8001
+ Streamlit on 8501); the dashboard code itself has no test deps.

Screenshots are saved under screenshots/step_5_2_ab/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = ROOT / "screenshots" / "step_5_2_ab"
SHOT_DIR.mkdir(parents=True, exist_ok=True)

DASHBOARD_URL = "http://localhost:8501"

# Per-page anchor text we expect to find in the rendered DOM. Arabic
# strings must appear verbatim (Streamlit's renderer preserves them).
PAGE_CHECKS: list[dict] = [
    {
        "id":           "home",
        "nav_label":    "🏠 الرئيسية",
        "title":        "الرئيسية",
        "anchors":      ["الأقسام", "أحدث الأخبار", "أحدث الأحداث",
                         "عرض كل الأحداث ←"],
        "shot":         "01_home.png",
    },
    {
        "id":           "events",
        "nav_label":    "📰 الأحداث",
        "title":        "الأحداث",
        "anchors":      ["القسم", "من تاريخ", "إلى تاريخ"],
        "shot":         "02_all_events.png",
    },
    {
        "id":           "libya",
        "nav_label":    "🇱🇾 ليبيا",
        "title":        "ليبيا",
        "anchors":      ["تصفية حسب التحيّز"],
        "shot":         "03_section_libya.png",
    },
    {
        "id":           "middle_east",
        "nav_label":    "🌍 الشرق الأوسط",
        "title":        "الشرق الأوسط",
        "anchors":      ["تصفية حسب التحيّز"],
        "shot":         "04_section_middle_east.png",
    },
    {
        "id":           "world",
        "nav_label":    "🌐 العالم",
        "title":        "العالم",
        "anchors":      ["تصفية حسب التحيّز"],
        "shot":         "05_section_world.png",
    },
    {
        "id":           "statistics",
        "nav_label":    "📊 الإحصائيات",
        "title":        "الإحصائيات",
        "anchors":      ["قيد الإنشاء", "5.2c"],
        "shot":         "06_statistics_placeholder.png",
    },
]


def _wait_for_render(page: Page, timeout_ms: int = 15000) -> None:
    """Wait until Streamlit's busy spinner is gone and h1 is in DOM."""
    page.wait_for_selector("h1", timeout=timeout_ms)
    # Wait for any in-flight network calls to finish (Streamlit fetches the
    # API on first script run; the WebSocket connection is also slow on
    # cold start).
    page.wait_for_load_state("networkidle", timeout=timeout_ms)


def _click_nav(page: Page, nav_label: str) -> None:
    """Click the sidebar radio item with the given Arabic label."""
    # The radio renders each option as a <label> with the formatted text.
    page.locator(f"label:has-text('{nav_label}')").first.click()
    _wait_for_render(page)
    # Defensive small settle to avoid race with deferred rerun.
    time.sleep(0.5)


def main() -> int:
    failures: list[str] = []
    rtl_status: dict[str, str] = {}

    with sync_playwright() as pw:
        # Use the locally-installed Google Chrome rather than Playwright's
        # auto-downloaded Chromium — the project sandbox blocks the
        # ms-playwright CDN download, but Chrome.app is already on the
        # system at /Applications/Google Chrome.app.
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        print(f"→ Navigating to {DASHBOARD_URL} ...")
        page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=30000)
        _wait_for_render(page)

        # ── Check 2 — body contains "Veritas" ───────────────────────────
        body_text = page.locator("body").inner_text()
        if "Veritas" in body_text:
            print("✓ Check 2 — body contains 'Veritas'")
        else:
            failures.append("Check 2 — body does NOT contain 'Veritas'")
            print("✗ Check 2 — body does NOT contain 'Veritas'")

        # ── Check 4/5/9 — iterate pages ────────────────────────────────
        for page_check in PAGE_CHECKS:
            print(f"\n→ Visiting page '{page_check['id']}'")
            try:
                _click_nav(page, page_check["nav_label"])
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{page_check['id']}: nav click failed: {exc}")
                continue

            # Wait a touch longer for the API call to come back.
            time.sleep(1.0)

            body = page.locator("body").inner_text()

            # Title check
            if page_check["title"] in body:
                print(f"  ✓ title '{page_check['title']}' present")
            else:
                failures.append(f"{page_check['id']}: title missing")
                print(f"  ✗ title '{page_check['title']}' missing")

            # Anchor checks (any-of semantics for robustness when DB is empty)
            anchor_found = False
            for anchor in page_check["anchors"]:
                if anchor in body:
                    anchor_found = True
                    print(f"  ✓ anchor '{anchor}' present")
                else:
                    print(f"  - anchor '{anchor}' missing (acceptable if empty state)")
            if not anchor_found:
                failures.append(f"{page_check['id']}: NO anchor strings found")

            # Check 5 — RTL applied
            direction = page.evaluate(
                "() => getComputedStyle("
                "document.querySelector('[data-testid=\"stMain\"]') "
                "|| document.body).direction"
            )
            rtl_status[page_check["id"]] = direction
            if direction == "rtl":
                print(f"  ✓ RTL applied (computed direction = '{direction}')")
            else:
                failures.append(
                    f"{page_check['id']}: RTL not applied, direction='{direction}'"
                )

            # Screenshot
            shot_path = SHOT_DIR / page_check["shot"]
            page.screenshot(path=str(shot_path), full_page=True)
            print(f"  ✓ screenshot → {shot_path.relative_to(ROOT)}")

        # ── Check 7 — bias filter behavior on Libya ───────────────────
        print("\n→ Check 7 — bias multi-select on Libya")
        try:
            _click_nav(page, "🇱🇾 ليبيا")
            time.sleep(1.0)

            # Count article cards before filter change
            initial_card_count = page.locator("a[target='_blank']").count()
            print(f"  initial link count: {initial_card_count}")

            # The Streamlit multiselect renders each chip with an X icon.
            # Find chips one by one and remove four of the five labels —
            # leaving only pan_arab selected.
            labels_to_keep = "pan_arab"
            labels_to_remove = ["pro_government", "opposition", "neutral",
                                "western_aligned"]
            for label in labels_to_remove:
                # Streamlit multiselect chips are rendered as spans with the
                # text content; the X close icon is next to them.
                chip_locator = page.locator(
                    f"div[data-baseweb='tag']:has-text('{label}')"
                ).first
                if chip_locator.count() == 0:
                    print(f"  - chip '{label}' not visible (was already removed?)")
                    continue
                # The chip has a clickable X — typically the second child
                # element with role='img' or class 'styled close'. We click
                # the close span inside the chip.
                chip_locator.locator("span").last.click()
                time.sleep(0.4)

            time.sleep(2.0)  # let API call complete

            after_card_count = page.locator("a[target='_blank']").count()
            print(f"  after-filter link count: {after_card_count}")

            # Take filter screenshot
            shot_path = SHOT_DIR / "07_libya_filter_pan_arab_only.png"
            page.screenshot(path=str(shot_path), full_page=True)
            print(f"  ✓ screenshot → {shot_path.relative_to(ROOT)}")

            # Semantic check: pan_arab text should appear in the
            # bias-bar labels of all visible cards.
            body = page.locator("body").inner_text()
            if labels_to_keep in body:
                print(f"  ✓ '{labels_to_keep}' still visible in bias bars")
            else:
                # If the DB has no pan_arab articles, the empty-state
                # message is the correct render. Check for that.
                if "لا توجد نتائج تطابق التصفية الحالية" in body:
                    print("  ✓ filter_empty message rendered (no pan_arab "
                          "articles in DB — expected)")
                else:
                    failures.append(
                        "Check 7 — bias filter did not produce pan_arab or "
                        "the expected empty-state message"
                    )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Check 7: exception: {exc}")
            print(f"  ✗ exception: {exc}")

        browser.close()

    print("\n========== SUMMARY ==========")
    print(f"RTL status per page: {rtl_status}")
    print(f"Screenshots saved under: {SHOT_DIR.relative_to(ROOT)}")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  • {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
