"""
frontend/app.py — Streamlit dashboard entry point (Phase 5 Step 5.2).

Run with: ``streamlit run frontend/app.py``.

Responsibilities:
  1. Configure the page and inject the RTL stylesheet (spec §2.3 / §2.4).
  2. Fetch ``/api/health`` ONCE per script rerun and stash the result in
     ``st.session_state["_health"]`` so every consumer (page headers,
     degraded banner) shares one call (additional decision (a)).
  3. Render the sidebar with the canonical navigation order from spec §3.1.
  4. Pop the refresh flag from session state and forward it to the chosen
     page's ``render(refresh, last_run)`` call.
  5. Render the degraded-mode banner when ``/api/health`` reports
     ``db == "disconnected"`` or ``redis == "disconnected"`` (spec §10.2).
  6. Render the top-level error banner and STOP if the health call itself
     fails (additional decision (e)).
  7. Render an "under construction" placeholder for the Statistics nav
     item — that page is implemented in Step 5.2c.

Architecture compliance is verified at the end of the session via the
greps documented in `phase_5_ui_spec.md` §1 and the Step 5.2 task spec.
The dashboard is a pure HTTP client of the backend on port 8001 and has
no other infrastructure imports.

Module-level imports below are side-effect free. All Streamlit calls
(set_page_config, sidebar, etc.) live inside ``main()`` so that importing
this module for verification (Check 1) does NOT trigger a Streamlit
runtime, which would crash outside of ``streamlit run``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit's script runner adds the entry-point's directory (frontend/)
# to sys.path, NOT the project root. Insert the project root so the
# `frontend.*` import paths resolve when this file is launched via
# `streamlit run frontend/app.py`. Imports remain `from frontend.*` so
# that `python -c "from frontend.app import *"` (Check 1) also works.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from typing import Optional  # noqa: E402

import streamlit as st  # noqa: E402

from frontend import api_client  # noqa: E402
from frontend.components import (  # noqa: E402
    SECTION_NAMES_AR,
    degraded_banner,
    error_banner,
)
from frontend.styles import inject_rtl  # noqa: E402

import datetime
from hijridate.convert import Gregorian

ARABIC_DAYS = {
    0: "الإثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد"
}

ARABIC_MONTHS_GREGORIAN = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل",
    5: "مايو", 6: "يونيو", 7: "يوليو", 8: "أغسطس",
    9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر"
}

ARABIC_MONTHS_HIJRI = {
    1: "محرم", 2: "صفر", 3: "ربيع الأول", 4: "ربيع الآخر",
    5: "جمادى الأولى", 6: "جمادى الآخرة", 7: "رجب", 8: "شعبان",
    9: "رمضان", 10: "شوال", 11: "ذو القعدة", 12: "ذو الحجة"
}

def _get_arabic_date_string() -> str:
    now = datetime.datetime.now()
    day_name = ARABIC_DAYS[now.weekday()]
    
    greg_day = now.day
    greg_month = ARABIC_MONTHS_GREGORIAN[now.month]
    greg_year = now.year
    
    hijri = Gregorian.today().to_hijri()
    hijri_day = hijri.day
    hijri_month = ARABIC_MONTHS_HIJRI[hijri.month]
    hijri_year = hijri.year
    
    return f"{day_name}، {greg_day} {greg_month} {greg_year} م <br/> {hijri_day} {hijri_month} {hijri_year} هـ"

# ─────────────────────────────────────────────────────────────────────────────
# Navigation labels — spec §3.1
# ─────────────────────────────────────────────────────────────────────────────

# Internal page IDs and their Arabic+emoji display labels. The radio renders
# Arabic via ``format_func``; the rest of the code uses the English IDs.
# Order is fixed by spec §3.1.
NAV_PAGES: list[str] = ["home", "events", "libya", "middle_east", "world", "statistics", "about"]

NAV_LABELS_AR: dict[str, str] = {
    "home":        "الرئيسية",
    "events":      "الأحداث",
    "libya":       SECTION_NAMES_AR["libya"],
    "middle_east": SECTION_NAMES_AR["middle_east"],
    "world":       SECTION_NAMES_AR["world"],
    "statistics":  "الإحصائيات",
    "about":       "عن النظام",
}


# ─────────────────────────────────────────────────────────────────────────────
# Shared health fetch (additional decision (a))
# ─────────────────────────────────────────────────────────────────────────────

def _get_shared_health() -> dict:
    """Fetch /api/health exactly once per script rerun, cache in session state.

    The session-state cache is cleared at the end of ``main()`` so the
    next rerun re-fetches. This bounds the cache lifetime to a single
    rerun pass — long enough for the page header and the degraded
    banner to share one call, short enough that ``last_pipeline_run``
    stays fresh.
    """
    cached = st.session_state.get("_health")
    if cached is not None:
        return cached
    health = api_client.get_health()
    st.session_state["_health"] = health
    return health


# ─────────────────────────────────────────────────────────────────────────────
# Masthead + top navigation
# ─────────────────────────────────────────────────────────────────────────────

def _on_nav_change() -> None:
    """Sync the radio selection to the current_page state."""
    st.session_state["current_page"] = st.session_state["nav"]

def _render_nav() -> str:
    """Render the centered masthead + horizontal top navigation."""
    date_str = _get_arabic_date_string()
    st.markdown(
        f"<div class='veritas-masthead'>"
        f"<div class='veritas-masthead-date'>{date_str}</div>"
        f"<div class='veritas-masthead-title'>Veritas Agent</div>"
        f"<div class='veritas-masthead-sub'>"
        f"نظام تحليل انحياز الأخبار باللغة العربية</div>"
        f"</div>"
        f"<hr class='veritas-masthead-rule'/>",
        unsafe_allow_html=True,
    )
    
    current = st.session_state.get("current_page", "home")
    idx = NAV_PAGES.index(current) if current in NAV_PAGES else 0

    st.radio(
        label="التنقّل",
        options=NAV_PAGES,
        format_func=lambda x: NAV_LABELS_AR.get(x, x),
        key="nav",
        index=idx,
        on_change=_on_nav_change,
        label_visibility="collapsed",
        horizontal=True,
    )
    return st.session_state["current_page"]


# ─────────────────────────────────────────────────────────────────────────────
# Degraded banner (spec §10.2)
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_render_degraded(health: dict) -> None:
    """Render the §10.2 degraded banner when DB or Redis is disconnected.

    Both conditions can be true simultaneously — we render one banner per
    affected component, stacked, so the user sees exactly what's down.
    """
    db_status = str(health.get("db", "")).lower()
    redis_status = str(health.get("redis", "")).lower()

    if db_status == "disconnected":
        degraded_banner("قاعدة البيانات")
    if redis_status == "disconnected":
        degraded_banner("Redis")


# ─────────────────────────────────────────────────────────────────────────────
# Main dispatch
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Top-level dispatcher. Called once per Streamlit rerun.

    Every Streamlit API call in this module is reached from inside this
    function, so a plain ``import frontend.app`` (e.g., from a CI
    verification script outside of ``streamlit run``) does not trigger
    any Streamlit runtime calls.
    """
    # 1) Page config + RTL injection. Must come before any st.* render.
    st.set_page_config(
        page_title="Veritas Agent",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    inject_rtl()

    # 2) Shared health fetch (additional decision (a)).
    health = _get_shared_health()

    # 3) Masthead + top navigation — always renders, even if health failed,
    #    so the user can navigate or retry.
    if "current_page" not in st.session_state:
        st.session_state["current_page"] = "home"

    selected = _render_nav()

    # 4) Pop the refresh flag set by page-header / section-card buttons.
    refresh: bool = bool(st.session_state.pop("refresh", False))

    # 5) Health-failure short-circuit (additional decision (e)). If
    #    /api/health itself errored, we DO NOT attempt to render any
    #    page — the page would just emit a second error banner. Render
    #    one top-level error banner and stop. The retry button reruns
    #    the script; we clear the cached error payload here so the
    #    next rerun re-fetches health.
    if health.get("status") == "error":
        st.session_state.pop("_health", None)
        error_banner(health.get("detail", ""), key_suffix="health")
        return

    # 6) Degraded banner if DB or Redis is down. Appears on every page.
    _maybe_render_degraded(health)

    # 7) Hand off to the selected page.
    last_run: Optional[str] = health.get("last_pipeline_run")

    if selected == "home":
        from frontend.pages import home
        home.render(refresh=refresh, last_run=last_run)
    elif selected == "events":
        from frontend.pages import all_events
        all_events.render(refresh=refresh, last_run=last_run)
    elif selected in ("libya", "middle_east", "world"):
        from frontend.pages import section_detail
        section_detail.render(section=selected, refresh=refresh, last_run=last_run)
    elif selected == "statistics":
        from frontend.pages import statistics
        statistics.render(refresh=refresh, last_run=last_run)
    elif selected == "about":
        from frontend.pages import about
        about.render(refresh=refresh, last_run=last_run)
    else:
        # Defensive fallback — unreachable given the radio's fixed options.
        st.error(f"صفحة غير معروفة: {selected}")

    # 8) Clear the shared-health cache at end-of-script so the NEXT
    #    rerun sees a fresh probe. Without this, the session-state
    #    cache would survive across reruns and ``last_pipeline_run``
    #    would never refresh.
    st.session_state.pop("_health", None)

    # 9) Render the global footer at the bottom of every page
    _render_footer()

def _render_footer() -> None:
    """Render the global footer with the 'About' link and copyright notice."""
    st.markdown("<hr style='margin-top: 4rem; margin-bottom: 1rem; border: 0; border-top: 1px solid var(--vt-line);' />", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("<div style='text-align: center;'>", unsafe_allow_html=True)
        if st.button("ما هو Veritas Agent؟", key="footer_about_btn", use_container_width=True):
            st.session_state["current_page"] = "about"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        
    st.markdown(
        "<div style='text-align: center; font-size: 0.85rem; color: var(--vt-muted); margin-top: 1rem; font-family: var(--vt-serif); direction: rtl;'>"
        "خلف كل خبر زاوية رؤية، فالتحيز لا يحتاج إلى كذب، بل يكفيه انتقاء الحقائق والتحكم في الكلمات والتركيز ليوجه الانتباه قبل الرأي."
        "</div>",
        unsafe_allow_html=True
    )
        
    st.markdown(
        """
        <div class="veritas-footer-copy" style="text-align: center; margin-top: 0.5rem; color: var(--vt-muted);">
            © 2026 veritas agent by Raghad Muftah Taboun
        </div>
        """,
        unsafe_allow_html=True,
    )


# Streamlit invokes the script with ``__name__ == "__main__"`` (it runs the
# file as the entry point). Plain ``import frontend.app`` from a test or
# verification harness does NOT match this guard, so main() is skipped and
# no Streamlit calls fire at import time — Check 1 stays green.
if __name__ == "__main__":
    main()
