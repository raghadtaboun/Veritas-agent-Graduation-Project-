"""
frontend/pages/statistics.py — Statistics page (الإحصائيات), spec §7.

Single page that consumes ``GET /api/statistics`` exactly once and renders:

  1. Page header (title + refresh + last-pipeline-run subtitle) — spec §3.2
  2. KPI cards row (4 cards: articles, events, blindspots, sources) — §7.3
  3. Eight chart sections A–H rendered via Plotly:                     §7.4–§7.11
       A — Pipeline Funnel                (horizontal bar)
       B — Bias Distribution              (vertical bar, BIAS_COLORS)
       C — Articles per Section           (vertical bar)
       D — Articles per Source (top 10)   (horizontal bar)
       E — Events per Section             (vertical bar)
       F — Blindspots per Section         (vertical bar)
       G — Avg Confidence per Bias Label  (horizontal bar, BIAS_COLORS)
       H — Articles per Event Distribution (vertical bar, forced ordering)

Per spec §10.1 / clarification (d): an empty data array on any chart
section renders the spec §10.1 ``no_data_yet`` message in place of the
chart canvas. The ``pending`` status from /api/statistics is distinct —
that's a page-level banner via ``pending_banner`` and short-circuits all
chart rendering.

Architecture compliance: this module imports only ``streamlit``, ``plotly``,
``frontend.api_client`` and ``frontend.components`` / ``frontend.styles``.
No psycopg2 / redis / agents / mcp_server / config imports.
"""

from __future__ import annotations

from typing import Any, Optional

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from frontend import api_client
from frontend.components import (
    MESSAGES_AR,
    SECTION_NAMES_AR,
    TOOLTIPS_AR,
    empty_state,
    error_banner,
    page_header,
    pending_banner,
    tooltip_icon,
)
from frontend.styles import BIAS_COLORS, BIAS_FALLBACK_COLOR

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Plotly layout defaults — keep every chart visually consistent.
# RTL note (spec §2.3): we let axis labels stay LTR (technical convention)
# while section headers and tooltips remain right-aligned via the page-level
# RTL CSS injection in styles.py.
_CHART_FONT_FAMILY: str = "Inter, Arial, sans-serif"
_CHART_HEIGHT: int = 340

# Horizontal bar charts (A, D, G) place categories on the y-axis. Some of
# those category strings are full Arabic phrases (e.g., "المُسترَدّ محتواها")
# or long domains (e.g., "english.alarabiya.net") — they need a fat left
# margin so they aren't clipped.
_CHART_MARGIN_VERTICAL: dict[str, int] = dict(l=40, r=80, t=40, b=60)
_CHART_MARGIN_HORIZONTAL: dict[str, int] = dict(l=190, r=80, t=40, b=40)

# Spec §7.11 — bucket ordering must be 1, 2, 3, 4, 5+ regardless of any
# alphabetical-sort surprise from Plotly when '5+' lands after '4' (which it
# does by default in this case, but we force-order explicitly per (c)).
_BUCKET_ORDER: list[str] = ["1", "2", "3", "4", "5+"]

# Per spec §7.5: the 5 bias bars must use BIAS_COLORS. Build the discrete
# color map once so every chart that displays bias labels uses the same
# label→color mapping (Check 6 — color palette consistency).
_BIAS_COLOR_MAP: dict[str, str] = dict(BIAS_COLORS)

# Arabic display labels for the three sections, matching the rest of the
# dashboard's Arabic-chrome convention (spec §2.2).
_SECTION_DISPLAY_AR: dict[str, str] = dict(SECTION_NAMES_AR)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called by frontend/app.py routing
# ─────────────────────────────────────────────────────────────────────────────

def render(refresh: bool, last_run: Optional[str]) -> None:
    """Render the Statistics page.

    Mirrors the render() signature of the other 4 pages — see
    Step 5.2c ambiguity #3 / approval. ``last_run`` is the
    ``last_pipeline_run`` value already extracted from /api/health by
    frontend/app.py (additional decision (a): one health probe per rerun).
    """
    page_header("الإحصائيات", last_run)

    data = api_client.get_statistics(refresh=refresh)

    # 1) Error short-circuit (Rule 2.4 — no silent failures).
    if data.get("status") == "error":
        error_banner(data.get("detail", ""), key_suffix="statistics")
        return

    # 2) Pending short-circuit (spec §10.3 — pipeline never ran).
    if data.get("status") == "pending":
        pending_banner()
        return

    # 3) KPI cards (spec §7.3).
    _render_kpi_cards(data.get("kpis") or {})

    # 4) Chart sections A–H (spec §7.4–§7.11), in order.
    _render_section_funnel(
        data.get("pipeline_funnel") or [],
        data.get("pipeline_funnel_note"),
    )
    _render_section_bias_distribution(data.get("bias_distribution") or [])
    _render_section_articles_per_section(data.get("articles_per_section") or [])
    _render_section_articles_per_source(data.get("articles_per_source") or [])
    _render_section_events_per_section(data.get("events_per_section") or [])
    _render_section_blindspots_per_section(data.get("blindspots_per_section") or [])
    _render_section_avg_confidence(data.get("avg_confidence_per_label") or [])
    _render_section_articles_per_event(data.get("articles_per_event_distribution") or [])


# ─────────────────────────────────────────────────────────────────────────────
# KPI cards (spec §7.3)
# ─────────────────────────────────────────────────────────────────────────────

def _render_kpi_cards(kpis: dict[str, Any]) -> None:
    """Render the 4 KPI cards row via ``st.metric``.

    A KPI of 0 is still rendered (spec §7.3 — "don't show empty state").
    Missing keys are coerced to 0 defensively; the API always returns all
    four (api/main.py:_fetch_kpis) so this is a safety net.
    """
    cols = st.columns(4)
    with cols[0]:
        st.metric(
            "مقالات",
            value=int(kpis.get("articles", 0) or 0),
        )
    with cols[1]:
        st.metric(
            "أحداث",
            value=int(kpis.get("events", 0) or 0),
        )
    with cols[2]:
        st.metric(
            "نقاط عمياء",
            value=int(kpis.get("blindspots", 0) or 0),
        )
    with cols[3]:
        st.metric(
            "مصادر",
            value=int(kpis.get("sources", 0) or 0),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Section header helper
# ─────────────────────────────────────────────────────────────────────────────

def _section_header(title: str, tooltip_key: Optional[str] = None) -> None:
    """Render a chart-section header with optional spec §9 tooltip.

    Uses the ``.veritas-stats-section`` CSS class (added in styles.py for
    Step 5.2c). Headers carry a subtle right-side accent stripe to visually
    separate one chart section from the next.
    """
    icon = ""
    if tooltip_key:
        help_text = TOOLTIPS_AR.get(tooltip_key, "")
        if help_text:
            icon = " " + tooltip_icon(help_text)
    st.markdown(
        f"<div class='veritas-stats-section'>◾ {_html_escape(title)}{icon}</div>",
        unsafe_allow_html=True,
    )


def _section_caption(text: str) -> None:
    """Render a small italic caption below a chart (e.g., Section A note)."""
    st.markdown(
        f"<div class='veritas-stats-caption'>{_html_escape(text)}</div>",
        unsafe_allow_html=True,
    )


def _html_escape(text: str) -> str:
    """Tiny HTML escape to avoid importing components._esc for one-liners."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _no_data() -> None:
    """Render the spec §10.1 generic empty-state message for an empty chart.

    Per clarification (d): an empty data array on a chart section is
    rendered as an inline ``لا توجد بيانات بعد.`` message, NOT an empty
    Plotly canvas.
    """
    empty_state("no_data_yet")


# ─────────────────────────────────────────────────────────────────────────────
# Shared chart builder
# ─────────────────────────────────────────────────────────────────────────────

def _apply_layout(fig: go.Figure, *, vertical: bool) -> None:
    """Apply the project-wide Plotly layout defaults to ``fig`` in place.

    Horizontal bar charts get a fat left margin so Arabic category labels
    on the y-axis aren't clipped. Vertical charts use a slim left margin.
    RTL handling: we leave axis ticks LTR (spec §2.3) and rely on the
    page-level RTL CSS for section headers.
    """
    margin = _CHART_MARGIN_VERTICAL if vertical else _CHART_MARGIN_HORIZONTAL
    fig.update_layout(
        font=dict(family=_CHART_FONT_FAMILY, size=12),
        height=_CHART_HEIGHT,
        margin=margin,
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    if vertical:
        fig.update_xaxes(tickangle=0)


# ─────────────────────────────────────────────────────────────────────────────
# Section A — Pipeline Funnel (spec §7.4)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_funnel(
    funnel: list[dict[str, Any]],
    note: Optional[str],
) -> None:
    _section_header("Pipeline Funnel", tooltip_key="pipeline_funnel")

    if not funnel:
        _no_data()
        return

    # Horizontal bars; preserve API ordering (fetched → in_events). Plotly
    # reverses the y-axis by default for category axes — we want stage 1 at
    # the TOP so the funnel reads top-to-bottom. Order categories explicitly.
    labels_ar: list[str] = [str(r.get("label_ar") or r.get("stage") or "") for r in funnel]
    counts: list[int] = [int(r.get("count") or 0) for r in funnel]
    fig = go.Figure(
        go.Bar(
            x=counts,
            y=labels_ar,
            orientation="h",
            marker_color="#1E5BFF",
            text=counts,
            textposition="outside",
        )
    )
    fig.update_yaxes(autorange="reversed", categoryorder="array",
                     categoryarray=labels_ar)
    _apply_layout(fig, vertical=False)
    st.plotly_chart(fig, use_container_width=True, key="stats_funnel")

    if note:
        # Spec §7.4 — show the Arabic caption when fetched/relevant are
        # unavailable. The API already provides the canonical wording in
        # ``pipeline_funnel_note`` (api/main.py:_FUNNEL_NOTE_AR).
        _section_caption(note)


# ─────────────────────────────────────────────────────────────────────────────
# Section B — Bias Distribution (spec §7.5)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_bias_distribution(rows: list[dict[str, Any]]) -> None:
    _section_header("توزّع التحيّز", tooltip_key="bias_label")

    if not rows:
        _no_data()
        return

    labels: list[str] = [str(r.get("label") or "") for r in rows]
    counts: list[int] = [int(r.get("count") or 0) for r in rows]
    colors: list[str] = [_BIAS_COLOR_MAP.get(lbl, BIAS_FALLBACK_COLOR) for lbl in labels]

    fig = go.Figure(
        go.Bar(
            x=labels,
            y=counts,
            marker_color=colors,
            text=counts,
            textposition="outside",
            texttemplate="%{y}",
        )
    )
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=labels)
    _apply_layout(fig, vertical=True)
    st.plotly_chart(fig, use_container_width=True, key="stats_bias_dist")


# ─────────────────────────────────────────────────────────────────────────────
# Section C — Articles per Section (spec §7.6)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_articles_per_section(rows: list[dict[str, Any]]) -> None:
    _section_header("المقالات حسب القسم")
    _render_section_bar(rows, chart_key="stats_articles_per_section")


# ─────────────────────────────────────────────────────────────────────────────
# Section D — Articles per Source, top 10 (spec §7.7)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_articles_per_source(rows: list[dict[str, Any]]) -> None:
    _section_header("المقالات حسب المصدر (أعلى 10)")

    if not rows:
        _no_data()
        return

    sources: list[str] = [str(r.get("source") or "") for r in rows]
    counts: list[int] = [int(r.get("count") or 0) for r in rows]

    fig = go.Figure(
        go.Bar(
            x=counts,
            y=sources,
            orientation="h",
            marker_color="#1E5BFF",
            text=counts,
            textposition="outside",
        )
    )
    # Top source at top (largest first). API returns ORDER BY count DESC,
    # so reversing the y-axis aligns the largest source with the top edge.
    fig.update_yaxes(autorange="reversed", categoryorder="array",
                     categoryarray=sources)
    _apply_layout(fig, vertical=False)
    st.plotly_chart(fig, use_container_width=True, key="stats_articles_per_source")


# ─────────────────────────────────────────────────────────────────────────────
# Section E — Events per Section (spec §7.8)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_events_per_section(rows: list[dict[str, Any]]) -> None:
    _section_header("الأحداث حسب القسم")
    _render_section_bar(rows, chart_key="stats_events_per_section")


# ─────────────────────────────────────────────────────────────────────────────
# Section F — Blindspots per Section (spec §7.9)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_blindspots_per_section(rows: list[dict[str, Any]]) -> None:
    _section_header("النقاط العمياء حسب القسم")
    _render_section_bar(rows, chart_key="stats_blindspots_per_section")


# ─────────────────────────────────────────────────────────────────────────────
# Shared: 3-bar section chart (C/E/F)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_bar(rows: list[dict[str, Any]], *, chart_key: str) -> None:
    """Render a 3-bar vertical chart for the per-section breakdown.

    The API guarantees a row per section (zero-padded by
    ``_fetch_count_per_section``). When the *entire* dataset is empty
    (no articles, no events) the caller-level pending check has already
    fired; reaching here with sum=0 still renders 3 zero-bars, which is
    the desired "show 0" semantics for partial-data states.
    """
    if not rows:
        _no_data()
        return

    sections_ar: list[str] = [
        _SECTION_DISPLAY_AR.get(str(r.get("section") or ""), str(r.get("section") or ""))
        for r in rows
    ]
    counts: list[int] = [int(r.get("count") or 0) for r in rows]

    fig = go.Figure(
        go.Bar(
            x=sections_ar,
            y=counts,
            marker_color="#1E5BFF",
            text=counts,
            textposition="outside",
        )
    )
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=sections_ar)
    _apply_layout(fig, vertical=True)
    st.plotly_chart(fig, use_container_width=True, key=chart_key)


# ─────────────────────────────────────────────────────────────────────────────
# Section G — Avg Confidence per Bias Label (spec §7.10)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_avg_confidence(rows: list[dict[str, Any]]) -> None:
    _section_header("متوسط الثقة لكلّ تصنيف")

    if not rows:
        _no_data()
        return

    labels: list[str] = [str(r.get("label") or "") for r in rows]
    avgs: list[float] = [float(r.get("avg_conf") or 0.0) for r in rows]
    # Clarification (b): use BIAS_COLORS for visual consistency even though
    # the metric is confidence rather than bias intensity. The "label →
    # color" mental model is preserved across the dashboard.
    colors: list[str] = [_BIAS_COLOR_MAP.get(lbl, BIAS_FALLBACK_COLOR) for lbl in labels]

    fig = go.Figure(
        go.Bar(
            x=avgs,
            y=labels,
            orientation="h",
            marker_color=colors,
            text=[f"{v:.2f}" for v in avgs],
            textposition="outside",
        )
    )
    # Per spec §7.10: x-axis is 0.0 → 1.0.
    fig.update_xaxes(range=[0.0, 1.05], tickformat=".1f")
    fig.update_yaxes(autorange="reversed", categoryorder="array",
                     categoryarray=labels)
    _apply_layout(fig, vertical=False)
    st.plotly_chart(fig, use_container_width=True, key="stats_avg_conf")


# ─────────────────────────────────────────────────────────────────────────────
# Section H — Articles per Event Distribution (spec §7.11)
# ─────────────────────────────────────────────────────────────────────────────

def _render_section_articles_per_event(rows: list[dict[str, Any]]) -> None:
    _section_header("توزّع المقالات لكلّ حدث")

    if not rows:
        _no_data()
        return

    # Force the bucket order (clarification (c)). Pad missing buckets
    # with 0 so the chart always shows all 5 columns when ANY events exist
    # — this matches the spec §7.11 layout expectation.
    by_bucket = {str(r.get("bucket") or ""): int(r.get("count") or 0) for r in rows}
    buckets = list(_BUCKET_ORDER)
    counts = [by_bucket.get(b, 0) for b in buckets]

    fig = px.bar(
        x=buckets,
        y=counts,
        category_orders={"x": _BUCKET_ORDER},
        color_discrete_sequence=["#1E5BFF"],
        text=counts,
    )
    fig.update_traces(textposition="outside")
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=_BUCKET_ORDER)
    _apply_layout(fig, vertical=True)
    st.plotly_chart(fig, use_container_width=True, key="stats_articles_per_event")
