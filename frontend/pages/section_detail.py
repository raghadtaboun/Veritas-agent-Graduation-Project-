"""
frontend/pages/section_detail.py — Section Detail page (×3), spec §6.

A single parameterized renderer serving Libya, Middle East, and World.
The caller (``frontend/app.py`` routing) decides which section to display
based on the sidebar selection.

Layout:
  1. Filter — multi-select bias dropdown (5 labels, all selected by default) §6.2
  2. Article list — grouped by Africa/Tripoli date (today / yesterday / ISO) §6.3
  3. Pagination — 20 articles per page                                      §6.5

Per spec §6.4 the article cards use title-as-link only (``show_source=False``).
Per additional decision (c) an empty ``articles_by_date`` triggers the
spec §10.1 ``filter_empty`` message, not an empty date header.
Per additional decision (d) Refresh does NOT reset filter selections.
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from frontend import api_client
from frontend.components import (
    SECTION_NAMES_AR,
    TOOLTIPS_AR,
    article_card,
    empty_state,
    error_banner,
    page_header,
    pending_banner,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Page size — spec §6.5: "20 articles per page."
_PER_PAGE: int = 20

# The five canonical bias labels in canonical display order (matches the
# api/main.py _VALID_BIAS_LABELS frozenset, ordered for UX consistency).
_BIAS_LABELS_ORDERED: list[str] = [
    "pro_government",
    "opposition",
    "neutral",
    "pan_arab",
    "western_aligned",
]


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called by frontend/app.py routing
# ─────────────────────────────────────────────────────────────────────────────

def render(section: str, refresh: bool, last_run: Optional[str]) -> None:
    """Render the section-detail page for ``section`` (libya / middle_east / world).

    The Arabic page title is the section's Arabic display name. The bias
    multi-select, page number, and previous-filter snapshot are stored in
    session-state keys namespaced by section so the three pages do not
    share state.
    """
    section_ar = SECTION_NAMES_AR.get(section, section)
    page_header(section_ar, last_run)

    # ── Filter ──────────────────────────────────────────────────────────────
    bias_selected = _render_bias_filter(section)

    # ── Page / change detection ─────────────────────────────────────────────
    page_key = f"section_{section}_page"
    prev_filter_key = f"section_{section}_prev_filter"
    prev_filter = st.session_state.get(prev_filter_key)
    current_filter = tuple(bias_selected)  # tuple for hashable equality
    if prev_filter is not None and prev_filter != current_filter:
        st.session_state[page_key] = 1
    st.session_state[prev_filter_key] = current_filter

    page = int(st.session_state.get(page_key, 1))

    # ── API call ────────────────────────────────────────────────────────────
    # If the user deselected every label, send an empty list (api_client
    # translates that to ?bias_labels= which the API maps to "no labels
    # match → empty result set" per Step 5.1b decision 4b). If they have
    # all five selected, send None so the FastAPI side LEFT-JOINs and
    # returns articles without a bias row too — that matches the default
    # "show everything" semantics from spec §6.2.
    if len(bias_selected) == len(_BIAS_LABELS_ORDERED):
        labels_for_api: Optional[list[str]] = None  # "show everything"
    elif len(bias_selected) == 0:
        labels_for_api = []  # zero rows
    else:
        labels_for_api = list(bias_selected)

    data = api_client.get_section(
        section=section,
        bias_labels=labels_for_api,
        page=page,
        per_page=_PER_PAGE,
        refresh=refresh,
    )

    if data.get("status") == "error":
        error_banner(data.get("detail", ""), key_suffix=f"section_{section}")
        return
    if data.get("status") == "pending":
        pending_banner()
        return

    total = int(data.get("total") or 0)
    groups = data.get("articles_by_date") or []

    # ── Render groups ───────────────────────────────────────────────────────
    if total == 0 or not groups:
        # Additional decision (c): empty articles_by_date → spec §10.1
        # filter_empty message, not an empty date header.
        if len(bias_selected) < len(_BIAS_LABELS_ORDERED):
            empty_state("filter_empty")
        else:
            empty_state("section_empty")
    else:
        st.markdown(
            f"<div class='veritas-meta-line'>المجموع: {total}</div>",
            unsafe_allow_html=True,
        )
        for group in groups:
            _render_date_group(group)

    # ── Pagination ──────────────────────────────────────────────────────────
    _render_pagination(section, page, total)


# ─────────────────────────────────────────────────────────────────────────────
# Bias filter (spec §6.2)
# ─────────────────────────────────────────────────────────────────────────────

def _render_bias_filter(section: str) -> list[str]:
    """Render the multi-select bias dropdown.

    Default: all five labels selected (spec §6.2). State is stored per
    section so that switching between Libya / ME / World preserves each
    section's own filter independently.

    Step 5.2c: spec §9 tooltip for "Bias Label" is attached to the
    multiselect's native ``help`` argument — Streamlit renders an ⓘ icon
    next to the widget label automatically. This is the first mention of
    the Bias Label concept on this page.
    """
    key = f"section_{section}_bias"
    default = st.session_state.get(key, _BIAS_LABELS_ORDERED)
    selected = st.multiselect(
        "تصفية حسب التحيّز",
        options=_BIAS_LABELS_ORDERED,
        default=default,
        key=key,
        help=TOOLTIPS_AR["bias_label"],
    )
    return list(selected)


# ─────────────────────────────────────────────────────────────────────────────
# Date group (spec §6.3)
# ─────────────────────────────────────────────────────────────────────────────

def _render_date_group(group: dict) -> None:
    """Render one ``{date, label, articles}`` group from the API response.

    The Arabic ``label`` ("اليوم" / "الأمس" / ISO date) is computed by the
    API per spec §6.3. If it's an ISO date, we format it with the Arabic day.
    """
    import datetime
    
    ARABIC_DAYS = {
        0: "الإثنين",
        1: "الثلاثاء",
        2: "الأربعاء",
        3: "الخميس",
        4: "الجمعة",
        5: "السبت",
        6: "الأحد"
    }
    
    label = group.get("label") or group.get("date") or ""
    
    # Check if the label is an ISO date (YYYY-MM-DD)
    if len(label) == 10 and label.count("-") == 2:
        try:
            dt = datetime.datetime.strptime(label, "%Y-%m-%d")
            day_name = ARABIC_DAYS[dt.weekday()]
            label = f"{day_name}، {label}"
        except ValueError:
            pass
            
    st.markdown(
        f"<div class='veritas-date-group'>{_html_escape(label)}</div>",
        unsafe_allow_html=True,
    )
    for art in group.get("articles") or []:
        # Spec §6.4: no separate source field on Section Detail — the
        # title itself is the link to the source URL.
        article_card(art, show_section_badge=False, show_source=False)


def _html_escape(text: str) -> str:
    """Tiny HTML escape to avoid importing components._esc for a one-liner."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pagination (spec §6.5)
# ─────────────────────────────────────────────────────────────────────────────

def _render_pagination(section: str, current_page: int, total: int) -> None:
    """Render prev / next buttons + page indicator (20 articles per page)."""
    if total <= 0:
        return

    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    current_page = max(1, min(current_page, total_pages))

    col_prev, col_indicator, col_next = st.columns([1, 2, 1])
    page_key = f"section_{section}_page"

    with col_prev:
        if st.button(
            "→ السابق",
            key=f"section_{section}_prev_page",
            disabled=(current_page <= 1),
            use_container_width=True,
        ):
            st.session_state[page_key] = current_page - 1
            st.rerun()

    with col_indicator:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px;color:#6B7280;'>"
            f"صفحة {current_page} من {total_pages}</div>",
            unsafe_allow_html=True,
        )

    with col_next:
        if st.button(
            "التالي ←",
            key=f"section_{section}_next_page",
            disabled=(current_page >= total_pages),
            use_container_width=True,
        ):
            st.session_state[page_key] = current_page + 1
            st.rerun()
