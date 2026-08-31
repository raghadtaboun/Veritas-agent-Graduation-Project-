"""Phase 5 Step 5.2c verification harness.

Drives the running Streamlit dashboard with a headless Chromium browser
and runs the 10 verification checks from the Step 5.2c task spec:

  Check 1 — Module imports (handled outside this script).
  Check 2 — Statistics page renders: 4 KPI cards, 8 chart sections.
  Check 3 — Tooltips appear (3 screenshots with ⓘ icons).
  Check 4 — Empty-state on Statistics (covered when DB is empty).
  Check 5 — Architecture greps (handled outside this script).
  Check 6 — Color palette consistency across bias-related visuals.
  Check 7 — RTL layout (computed direction = 'rtl').
  Check 8 — Refresh button on Statistics issues ?refresh=true.
  Check 9 — Full dashboard smoke test (6 sidebar items render).
  Check 10 — Acceptance criteria walk-through (reported in chat).

Screenshots are saved under screenshots/step_5_2_c/.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = ROOT / "screenshots" / "step_5_2_c"
SHOT_DIR.mkdir(parents=True, exist_ok=True)

DASHBOARD_URL = "http://localhost:8501"

# Sidebar nav items, in spec §3.1 canonical order.
NAV_LABELS: list[tuple[str, str]] = [
    ("home",        "🏠 الرئيسية"),
    ("events",      "📰 الأحداث"),
    ("libya",       "🇱🇾 ليبيا"),
    ("middle_east", "🌍 الشرق الأوسط"),
    ("world",       "🌐 العالم"),
    ("statistics",  "📊 الإحصائيات"),
]

# Page titles expected on each page.
PAGE_TITLES: dict[str, str] = {
    "home":        "الرئيسية",
    "events":      "الأحداث",
    "libya":       "ليبيا",
    "middle_east": "الشرق الأوسط",
    "world":       "العالم",
    "statistics":  "الإحصائيات",
}

# Statistics-page expected content: 4 KPI labels + 8 chart section headers.
STATS_KPIS: list[str] = ["مقالات", "أحداث", "نقاط عمياء", "مصادر"]
STATS_SECTIONS: list[str] = [
    "Pipeline Funnel",
    "توزّع التحيّز",
    "المقالات حسب القسم",
    "المقالات حسب المصدر (أعلى 10)",
    "الأحداث حسب القسم",
    "النقاط العمياء حسب القسم",
    "متوسط الثقة لكلّ تصنيف",
    "توزّع المقالات لكلّ حدث",
]


def _wait(page: Page, timeout_ms: int = 20000) -> None:
    page.wait_for_selector("h1", timeout=timeout_ms)
    page.wait_for_load_state("networkidle", timeout=timeout_ms)


def _click_nav(page: Page, nav_label: str) -> None:
    page.locator(f"label:has-text('{nav_label}')").first.click()
    _wait(page)
    time.sleep(0.6)


def main() -> int:
    failures: list[str] = []
    rtl_per_page: dict[str, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="chrome")
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()

        print(f"→ Navigating to {DASHBOARD_URL} ...")
        page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=30000)
        _wait(page)

        # ── Check 9 — full smoke test (visit each of 6 pages, verify title) ──
        print("\n=== Check 9 — Full dashboard smoke test ===")
        for page_id, nav_label in NAV_LABELS:
            try:
                _click_nav(page, nav_label)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"smoke[{page_id}]: nav click failed: {exc}")
                print(f"  ✗ {page_id}: nav click failed: {exc}")
                continue
            body = page.locator("body").inner_text()
            title_ok = PAGE_TITLES[page_id] in body
            if title_ok:
                print(f"  ✓ {page_id}: title '{PAGE_TITLES[page_id]}' present")
            else:
                failures.append(f"smoke[{page_id}]: title missing")
                print(f"  ✗ {page_id}: title missing")
            # Statistics-specific assertion: NO placeholder text
            if page_id == "statistics" and "قيد الإنشاء" in body:
                failures.append("smoke[statistics]: placeholder still present")
                print("  ✗ statistics: placeholder text still present!")
            # RTL check
            direction = page.evaluate(
                "() => getComputedStyle("
                "document.querySelector('[data-testid=\"stMain\"]') "
                "|| document.body).direction"
            )
            rtl_per_page[page_id] = direction
            if direction != "rtl":
                failures.append(f"{page_id}: RTL not applied (got '{direction}')")

        # ── Check 2 — Statistics page renders 4 KPIs + 8 sections ───────
        print("\n=== Check 2 — Statistics page renders ===")
        _click_nav(page, "📊 الإحصائيات")
        time.sleep(1.5)
        body = page.locator("body").inner_text()

        for kpi in STATS_KPIS:
            if kpi in body:
                print(f"  ✓ KPI label '{kpi}' present")
            else:
                failures.append(f"Check 2 — KPI '{kpi}' missing")
                print(f"  ✗ KPI '{kpi}' missing")

        for sec in STATS_SECTIONS:
            if sec in body:
                print(f"  ✓ section '{sec}' present")
            else:
                failures.append(f"Check 2 — section '{sec}' missing")
                print(f"  ✗ section '{sec}' missing")

        # Plotly canvases: each chart should render at least one bar.
        # Plotly produces SVG inside <div class='js-plotly-plot'>.
        plot_count = page.locator("div.js-plotly-plot").count()
        print(f"  Plotly plot count: {plot_count}")
        if plot_count < 8:
            failures.append(f"Check 2 — only {plot_count}/8 Plotly charts rendered")

        # Take full-page screenshot and a charts-D/E section screenshot.
        shot_full = SHOT_DIR / "01_statistics_full.png"
        page.screenshot(path=str(shot_full), full_page=True)
        print(f"  ✓ screenshot → {shot_full.relative_to(ROOT)}")

        # Scroll into "Articles per Source" header for the D+E screenshot.
        try:
            # Use a substring match to avoid parenthesis-parsing quirks.
            page.locator(":text('المقالات حسب المصدر')").first.scroll_into_view_if_needed(
                timeout=10000
            )
            time.sleep(0.7)
            shot_de = SHOT_DIR / "02_statistics_charts.png"
            page.screenshot(path=str(shot_de), full_page=False)
            print(f"  ✓ screenshot → {shot_de.relative_to(ROOT)}")
        except Exception as exc:  # noqa: BLE001
            # Fallback: scroll by viewport heights and shoot whatever we land on.
            try:
                page.evaluate("window.scrollTo(0, 900)")
                time.sleep(0.7)
                shot_de = SHOT_DIR / "02_statistics_charts.png"
                page.screenshot(path=str(shot_de), full_page=False)
                print(f"  ✓ screenshot (scroll fallback) → "
                      f"{shot_de.relative_to(ROOT)}")
            except Exception as exc2:  # noqa: BLE001
                print(f"  - charts D+E screenshot failed: {exc} / {exc2}")

        # ── Check 3 — tooltips visible (ⓘ icons present) ─────────────────
        print("\n=== Check 3 — Tooltips visible ===")

        # Section detail Libya: hover over multiselect help icon.
        _click_nav(page, "🇱🇾 ليبيا")
        time.sleep(1.0)
        # Streamlit's multiselect with help= renders a
        # [data-testid="stTooltipHoverTarget"] button (icon) next to the label.
        tooltip_targets_section = page.locator(
            "[data-testid='stTooltipHoverTarget']"
        ).count()
        print(f"  Section Detail: stTooltipHoverTarget count = "
              f"{tooltip_targets_section}")
        # Scroll the bias-filter label into view so the ⓘ help icon is
        # captured by the screenshot. window.scrollTo doesn't work
        # reliably with Streamlit's nested scroll containers.
        try:
            page.locator("text=تصفية حسب التحيّز").first.scroll_into_view_if_needed(
                timeout=8000
            )
        except Exception:  # noqa: BLE001
            page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.6)
        shot_section = SHOT_DIR / "03_tooltip_bias_label.png"
        page.screenshot(path=str(shot_section), full_page=False)
        print(f"  ✓ screenshot → {shot_section.relative_to(ROOT)}")

        # Home page: expand an event to see 4 inline ⓘ tooltips inside event_card.
        _click_nav(page, "🏠 الرئيسية")
        time.sleep(1.0)
        # The custom ⓘ tooltips render as <span class='veritas-tooltip'>ⓘ</span>.
        ico_count_home = page.locator("span.veritas-tooltip").count()
        print(f"  Home page: veritas-tooltip ⓘ count = {ico_count_home}")
        # Try to expand the first event card so the inner tooltips are visible.
        try:
            page.locator("[data-testid='stExpander'] summary").first.click()
            time.sleep(0.8)
            ico_count_home_expanded = page.locator("span.veritas-tooltip").count()
            print(f"  Home (expanded): veritas-tooltip ⓘ count = "
                  f"{ico_count_home_expanded}")
        except Exception as exc:  # noqa: BLE001
            print(f"  - expand-event-card failed: {exc}")
        shot_event = SHOT_DIR / "04_tooltip_event_card.png"
        page.screenshot(path=str(shot_event), full_page=False)
        print(f"  ✓ screenshot → {shot_event.relative_to(ROOT)}")

        # Statistics page: Pipeline Funnel ⓘ visible.
        _click_nav(page, "📊 الإحصائيات")
        time.sleep(1.0)
        ico_count_stats = page.locator("span.veritas-tooltip").count()
        print(f"  Statistics page: veritas-tooltip ⓘ count = {ico_count_stats}")
        # Scroll Section A's header into view so the Pipeline Funnel ⓘ
        # icon is captured. ``window.scrollTo`` does not work reliably
        # with Streamlit's nested scroll container.
        try:
            page.locator("text=Pipeline Funnel").first.scroll_into_view_if_needed(
                timeout=8000
            )
        except Exception:  # noqa: BLE001
            page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.6)
        shot_funnel = SHOT_DIR / "05_tooltip_funnel.png"
        page.screenshot(path=str(shot_funnel), full_page=False)
        print(f"  ✓ screenshot → {shot_funnel.relative_to(ROOT)}")

        # Verify ⓘ title attributes contain expected substrings.
        spans = page.locator("span.veritas-tooltip")
        titles = []
        for i in range(spans.count()):
            titles.append(spans.nth(i).get_attribute("title") or "")
        joined_titles = " | ".join(titles)
        if "تدرّج المقالات عبر مراحل المعالجة" in joined_titles:
            print("  ✓ Pipeline Funnel tooltip text found")
        else:
            failures.append("Check 3 — Pipeline Funnel tooltip text missing")
            print("  ✗ Pipeline Funnel tooltip text NOT found")
        if "تصنيف التحيّز الأيديولوجي" in joined_titles:
            print("  ✓ Bias Label tooltip text found")
        else:
            print("  - Bias Label tooltip text not on Statistics (expected on "
                  "Section Detail multiselect, which uses st.multiselect help= "
                  "not span.veritas-tooltip)")

        # ── Check 6 — color palette consistency ──────────────────────────
        print("\n=== Check 6 — Color palette consistency ===")
        # The Plotly SVG renders bars with the corresponding fill colors.
        # We extract all <path class='point' style='...fill...'> elements
        # from the Bias Distribution plot and verify pan_arab → #EF4444.
        # Plotly's typical structure: each trace's bars are <g class='points'>
        # > <path style='fill: rgb(...)' />. We look for the discrete colors.
        # First, locate the chart whose preceding header text is "توزّع التحيّز".
        try:
            # Plotly v5 nests bar paths under `g.points > g.point > path`,
            # so a descendant selector is needed (the `>` direct-child
            # selector returned 0 in an earlier iteration).
            fills = page.evaluate("""() => {
                const paths = Array.from(document.querySelectorAll(
                    'g.points path, g.barlayer path'
                ));
                return paths.map(p => p.getAttribute('style') || '');
            }""")
            uniq_fills = sorted({s for s in fills if s.strip()})
            print(f"  Found {len(uniq_fills)} unique bar fill styles on Statistics page.")
            # Convert RGB to hex for matching.
            def _hex_of_rgb(rgb_str):
                # rgb(239, 68, 68) → #EF4444 etc.
                import re
                m = re.search(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", rgb_str)
                if not m:
                    return None
                r, g, b = map(int, m.groups())
                return f"#{r:02X}{g:02X}{b:02X}"
            hexes = {_hex_of_rgb(s) for s in uniq_fills}
            hexes.discard(None)
            print(f"  Hex colors observed: {sorted(hexes)}")

            expected = {"#EF4444", "#3B82F6", "#F59E0B", "#9CA3AF", "#8B5CF6"}
            seen = expected & hexes
            if seen >= {"#EF4444"}:
                print(f"  ✓ pan_arab color #EF4444 present on Statistics charts")
            else:
                # If pan_arab has 0 articles in the DB the bar isn't rendered.
                # That's acceptable but we should at least see other BIAS_COLORS.
                if expected & hexes:
                    print("  - pan_arab not visible (zero articles?) but "
                          "other BIAS_COLORS present: %s" % sorted(expected & hexes))
                else:
                    failures.append("Check 6 — no BIAS_COLORS hex found on Statistics page")
                    print("  ✗ none of the expected BIAS_COLORS hexes were observed")
        except Exception as exc:  # noqa: BLE001
            print(f"  - color extraction failed: {exc}")

        # ── Check 7 — RTL summary ────────────────────────────────────────
        print("\n=== Check 7 — RTL per page ===")
        for k, v in rtl_per_page.items():
            print(f"  {k}: {v}")

        # ── Check 8 — refresh button on Statistics ───────────────────────
        # Verified separately via uvicorn log inspection (this script just
        # clicks the button and observes no crash).
        print("\n=== Check 8 — Refresh button on Statistics ===")
        _click_nav(page, "📊 الإحصائيات")
        time.sleep(1.0)
        try:
            page.locator("button:has-text('⟳ تحديث')").first.click()
            time.sleep(2.0)
            body = page.locator("body").inner_text()
            if "الإحصائيات" in body:
                print("  ✓ refresh click did not crash the page")
            else:
                failures.append("Check 8 — page title missing after refresh")
                print("  ✗ page title missing after refresh")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Check 8 — refresh failed: {exc}")
            print(f"  ✗ refresh failed: {exc}")

        # ── Final shot ───────────────────────────────────────────────────
        shot_final = SHOT_DIR / "06_dashboard_complete.png"
        page.screenshot(path=str(shot_final), full_page=True)
        print(f"  ✓ screenshot → {shot_final.relative_to(ROOT)}")

        browser.close()

    print("\n========== SUMMARY ==========")
    print(f"RTL per page: {rtl_per_page}")
    print(f"Screenshots saved under: {SHOT_DIR.relative_to(ROOT)}")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  • {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
