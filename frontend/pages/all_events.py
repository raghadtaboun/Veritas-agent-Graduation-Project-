"""
frontend/pages/all_events.py — All Events page (الأحداث), spec §5.

Renders three layers under the page header:
  1. Filters bar — section dropdown + optional date-range pair (§5.3)
  2. Event list — collapsed expandable event cards, 10 per page  (§5.2)
  3. Pagination — prev / next / "page X of Y" indicator           (§5.4)

State management (additional decision (d)): filter changes do NOT reset
on a Refresh-button click. Refresh only invalidates the API cache; the
user's section choice / date range / page number stay intact. Page is
reset to 1 only when the filter values themselves change (detected by
comparing against a session-state cache of the previous filter tuple).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import streamlit as st

from frontend import api_client
from frontend.components import (
    SECTION_NAMES_AR,
    empty_state,
    error_banner,
    event_card,
    page_header,
    pending_banner,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Page size — spec §5.4: "10 events per page."
_PER_PAGE: int = 10

# Section dropdown options. The "all" value mirrors the FastAPI default
# (api/main.py:_EVENTS_SECTION_VALUES). Tuples are (id, label_ar).
_SECTION_OPTIONS: list[tuple[str, str]] = [
    ("all",         "الكل"),
    ("libya",       SECTION_NAMES_AR["libya"]),
    ("middle_east", SECTION_NAMES_AR["middle_east"]),
    ("world",       SECTION_NAMES_AR["world"]),
]

# Session-state keys, namespaced so they don't collide with other pages.
_KEY_PAGE = "events_page"
_KEY_SECTION = "events_filter_section"
_KEY_DATE_FROM = "events_filter_date_from"
_KEY_DATE_TO = "events_filter_date_to"
_KEY_PREV_FILTERS = "events_prev_filters"  # tuple snapshot for change detection


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called by frontend/app.py routing
# ─────────────────────────────────────────────────────────────────────────────

def render(refresh: bool, last_run: Optional[str]) -> None:
    """Render the All Events page.

    Step 5.2c: the spec §9 tooltip for "Event" is rendered next to the
    page title via ``page_header``'s ``help_text`` kwarg. This is the
    first mention of the Event term on this page.
    """
    page_header("الأحداث", last_run)

    # ── Filters bar ─────────────────────────────────────────────────────────
    section_id, date_from, date_to = _render_filters_bar()

    # If filters changed since last render, reset page to 1.
    filters_tuple = (section_id, date_from, date_to)
    prev = st.session_state.get(_KEY_PREV_FILTERS)
    if prev is not None and prev != filters_tuple:
        st.session_state[_KEY_PAGE] = 1
    st.session_state[_KEY_PREV_FILTERS] = filters_tuple

    page = int(st.session_state.get(_KEY_PAGE, 1))

    # ── API call ────────────────────────────────────────────────────────────
    data = api_client.get_events(
        section=section_id,
        page=page,
        per_page=_PER_PAGE,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        refresh=refresh,
    )

    if data.get("status") == "error":
        error_banner(data.get("detail", ""), key_suffix="events")
        return
    if data.get("status") == "pending":
        pending_banner()
        return

    total = int(data.get("total") or 0)
    events = data.get("events") or []

    # ── Event list ──────────────────────────────────────────────────────────
    if total == 0 or not events:
        # If user has filters set, this is "filter returned zero"; if no
        # filters and still zero, this is "no events in DB" — fall back to
        # the broader message in that case for a friendlier UX.
        if section_id != "all" or date_from is not None or date_to is not None:
            empty_state("filter_empty")
        else:
            empty_state("no_events_in_db")
    else:
        st.markdown(f"<div class='veritas-meta-line'>المجموع: {total}</div>",
                    unsafe_allow_html=True)
        for ev in events:
            event_card(ev, expanded=False)

    # ── Pagination ──────────────────────────────────────────────────────────
    _render_pagination(page, total)


# ─────────────────────────────────────────────────────────────────────────────
# Filters bar (spec §5.3)
# ─────────────────────────────────────────────────────────────────────────────

def _render_filters_bar() -> tuple[str, Optional[date], Optional[date]]:
    """Render the section dropdown and the (optional) date-range inputs.

    Returns ``(section_id, date_from, date_to)`` where dates are ``None``
    when the user hasn't picked them. The widgets bind their values to
    session state via the ``key`` argument so they persist across reruns
    (matches additional decision (d): refresh does NOT reset filters).
    """
    col_section, col_from, col_to = st.columns([2, 1, 1])

    with col_section:
        section_ids = [opt[0] for opt in _SECTION_OPTIONS]
        labels = {opt[0]: opt[1] for opt in _SECTION_OPTIONS}
        section_id = st.selectbox(
            "القسم",
            options=section_ids,
            index=section_ids.index(
                st.session_state.get(_KEY_SECTION, "all")
            ) if st.session_state.get(_KEY_SECTION, "all") in section_ids else 0,
            format_func=lambda x: labels[x],
            key=_KEY_SECTION,
        )

    with col_from:
        date_from = st.date_input(
            "من تاريخ",
            value=st.session_state.get(_KEY_DATE_FROM),
            key=_KEY_DATE_FROM,
            format="YYYY-MM-DD",
        )

    with col_to:
        date_to = st.date_input(
            "إلى تاريخ",
            value=st.session_state.get(_KEY_DATE_TO),
            key=_KEY_DATE_TO,
            format="YYYY-MM-DD",
        )

    # ``st.date_input`` may return a single ``date`` or a tuple in range
    # mode; we use single-value mode above, so the return is either a
    # ``date`` or ``None`` (value=None case). Coerce to None when the
    # user hasn't picked.
    date_from_val: Optional[date] = date_from if isinstance(date_from, date) else None
    date_to_val: Optional[date] = date_to if isinstance(date_to, date) else None

    return section_id, date_from_val, date_to_val


# ─────────────────────────────────────────────────────────────────────────────
# Pagination (spec §5.4)
# ─────────────────────────────────────────────────────────────────────────────

def _render_pagination(current_page: int, total: int) -> None:
    """Render the prev / next buttons + "page X of Y" indicator at the bottom.

    With 10 events per page, ``total_pages = ceil(total / 10)`` (at least
    1 even when ``total == 0`` to keep the layout stable). Prev / next
    buttons are disabled when at the first / last page respectively.
    """
    if total <= 0:
        return  # no events → no pagination row

    total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    current_page = max(1, min(current_page, total_pages))

    col_prev, col_indicator, col_next = st.columns([1, 2, 1])

    with col_prev:
        if st.button(
            "→ السابق",
            key="events_prev_page",
            disabled=(current_page <= 1),
            use_container_width=True,
        ):
            st.session_state[_KEY_PAGE] = current_page - 1
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
            key="events_next_page",
            disabled=(current_page >= total_pages),
            use_container_width=True,
        ):
            st.session_state[_KEY_PAGE] = current_page + 1
            st.rerun()
