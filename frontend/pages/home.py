"""
frontend/pages/home.py — Home page (الرئيسية), spec §4.

Renders two sections under the page header:
  1. Latest News (5 articles, selected per the §4.4 backend rule)
  2. Latest Events (3 expandable event cards)                      §4.5

Explicit non-features:
  * No KPI cards on this page (spec §4.1, acceptance criterion 5).
  * No statistics summary.

The page consumes ``/api/home`` exactly as documented in api/main.py:726.
The ``last_pipeline_run`` value is NOT on that payload — it comes from
``/api/health`` and is passed in by the caller (additional decision A1).
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from frontend import api_client
from frontend.components import (
    article_card,
    empty_state,
    error_banner,
    event_card,
    page_header,
    pending_banner,
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called by frontend/app.py routing
# ─────────────────────────────────────────────────────────────────────────────

def render(refresh: bool, last_run: Optional[str]) -> None:
    """Render the Home page.

    ``refresh`` is the cache-bypass flag forwarded from the sidebar
    Refresh button. ``last_run`` is the ``last_pipeline_run`` value
    from ``/api/health`` already fetched by ``app.py`` (additional
    decision (a) — single health call per rerun).
    """
    page_header("الرئيسية", last_run)

    data = api_client.get_home(refresh=refresh)

    if data.get("status") == "error":
        error_banner(data.get("detail", ""), key_suffix="home")
        return
    if data.get("status") == "pending":
        pending_banner()
        return

    _render_latest_news(data.get("latest_news") or [])
    _render_latest_events(data.get("latest_events") or [])


# ─────────────────────────────────────────────────────────────────────────────
# Section — latest news (spec §4.4)
# ─────────────────────────────────────────────────────────────────────────────

def _render_latest_news(articles: list[dict]) -> None:
    """Render the 5-article latest-news block.

    The selection rule (1-per-section + 2-newest-from-rest) is enforced
    on the API side (api/main.py:_fetch_latest_news). The page renders
    whatever the API returned, sorted descending by published_at.

    Step 5.2c: tooltip for the "Framing" term (spec §9) is attached to
    this section header — articles below display their framing line, so
    this is the natural first-mention location for the Framing concept
    on the Home page. Per-article ⓘ icons were deliberately skipped to
    avoid visual clutter (5.2c framing-placement decision).
    """
    st.markdown("## أحدث الأخبار")
    if not articles:
        empty_state("no_articles_in_db")
        return
    for art in articles:
        article_card(art, show_section_badge=True, show_source=True)


# ─────────────────────────────────────────────────────────────────────────────
# Section 3 — latest events (spec §4.5)
# ─────────────────────────────────────────────────────────────────────────────

def _render_latest_events(events: list[dict]) -> None:
    """Render the 3 most recent events as collapsed cards + "see all" link."""
    st.markdown("## أحدث الأحداث")
    if not events:
        empty_state("no_events_in_db")
    else:
        for ev in events:
            event_card(ev, expanded=False)

    # "See all events" link — implemented as a button so it can route in-app.
    st.markdown("")  # small vertical gap
    if st.button(
        "عرض كل الأحداث ←",
        key="goto_all_events",
        use_container_width=False,
    ):
        st.session_state["current_page"] = "events"
        st.rerun()
