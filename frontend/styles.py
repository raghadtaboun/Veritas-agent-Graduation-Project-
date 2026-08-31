"""
frontend/styles.py — RTL CSS injection, bias color palette, accent color.

Streamlit does not apply RTL automatically (phase_5_ui_spec.md §2.3). This
module exposes a single ``inject_rtl()`` function that the entry point
calls once per script run to apply the RTL layout, the bias bar styles,
the section-card button styling (additional decision (b) from Step 5.2a),
and the empty-state / banner styling consumed by ``components.py``.

The five bias label colors (§2.6) are a single source of truth shared by
``components.bias_bar`` and any future chart code.
"""

from __future__ import annotations

import streamlit as st

# Spec §2.4 — accent color (project blue, mirrors api/main.py CORS origin).
ACCENT_COLOR: str = "#1E5BFF"

# Spec §2.6 — bias label palette. Used by the bias bar in components.py
# and (in Step 5.2c) by Statistics charts. Keep keys in sync with the API's
# _VALID_BIAS_LABELS frozenset (api/main.py:91).
BIAS_COLORS: dict[str, str] = {
    "pro_government":  "#3B82F6",  # blue
    "opposition":      "#F59E0B",  # amber
    "neutral":         "#9CA3AF",  # gray
    "pan_arab":        "#EF4444",  # red
    "western_aligned": "#8B5CF6",  # purple
}

# Fallback color when a bias label slips through that isn't one of the
# canonical five (e.g., a stale row from before the taxonomy lock).
BIAS_FALLBACK_COLOR: str = "#6B7280"


# Page-wide RTL + "newspaper" stylesheet. Targets the modern Streamlit DOM
# (1.30+). A few selectors are kept for older versions in case the user is on
# a pinned streamlit < 1.50; they no-op on newer versions.
#
# NOTE (presentation only): this stylesheet was restyled to give the dashboard
# a news-site / newspaper look (serif masthead + headlines, horizontal top
# navigation, hairline rules, generous whitespace). Every CSS class name used
# by components.py / the pages is preserved verbatim — only the visual rules
# changed. No Python structure, routing, or API contract is affected.
RTL_CSS: str = """
/* ── Web fonts: a serif display face for the masthead + Arabic headlines ─ *
 * Playfair Display gives the Latin "Veritas Agent" masthead a newspaper feel;
 * Amiri / Noto Naskh provide an elegant Arabic serif for headlines. Source
 * Sans / system fonts handle body copy for readability.
 */
@import url('https://fonts.googleapis.com/css2?family=Amiri:wght@400;700&family=Noto+Naskh+Arabic:wght@400;600;700&family=Playfair+Display:wght@700;900&family=Source+Sans+3:wght@400;600;700&display=swap');

:root {
    --vt-ink: #16140F;
    --vt-muted: #6B6356;
    --vt-line: #E2DDD2;
    --vt-paper: #FBFBF9;
    --vt-accent: #1E5BFF;
    --vt-serif: "Amiri", "Noto Naskh Arabic", "Playfair Display", Georgia, "Times New Roman", serif;
    --vt-sans: "Source Sans 3", "Segoe UI", "Tahoma", system-ui, sans-serif;
}

/* ── Root direction ────────────────────────────────────────────────── */
html, body, [class*="css"], [data-testid="stAppViewContainer"],
[data-testid="stMain"], [data-testid="stSidebar"], .stApp, .main {
    direction: rtl;
    text-align: right;
}

html, body, [data-testid="stMain"] {
    font-family: var(--vt-sans);
    color: var(--vt-ink);
    background-color: var(--vt-paper);
}

/* Constrain the reading column so it feels like a printed page, not a
   full-bleed dashboard. */
[data-testid="stMain"] .block-container {
    max-width: 1180px;
    padding-top: 2.2rem;
}

/* ── Headings — serif, like a newspaper ────────────────────────────── */
h1, h2, h3, h4, h5, h6 {
    text-align: right;
    font-family: var(--vt-serif);
    color: var(--vt-ink);
    letter-spacing: 0;
}

/* ── Masthead (rendered by app.py at the top of the main column) ───── */
.veritas-masthead {
    text-align: center;
    margin: 0 0 2px 0;
    position: relative;
}
.veritas-masthead-date {
    position: absolute;
    left: 0;
    top: 80%;
    transform: translateY(-50%);
    font-family: var(--vt-serif);
    font-size: 0.95rem;
    color: var(--vt-muted);
    text-align: right;
    direction: rtl;
}
.veritas-masthead-title {
    font-family: "Playfair Display", var(--vt-serif);
    font-weight: 900;
    font-size: 3.1rem;
    line-height: 1.05;
    letter-spacing: 0.5px;
    color: var(--vt-ink);
    margin: 0;
}
.veritas-masthead-sub {
    font-family: var(--vt-serif);
    color: var(--vt-muted);
    font-size: 1.05rem;
    margin-top: 14px;
}
/* Thin double rule under the masthead — a classic newspaper divider. */
.veritas-masthead-rule {
    border: 0;
    border-top: 3px double var(--vt-ink);
    margin: 14px auto 4px auto;
    max-width: 1180px;
}

/* ── Top navigation (the nav radio relocated to the main column) ───── *
 * app.py renders the navigation radio (key="nav") inside the main area.
 * Streamlit wraps it in a div with class `st-key-nav`. We apply the full-width
 * borders to this wrapper and center the radio group inside it.
 */
.st-key-nav {
    display: block !important;
    width: 100% !important;
    text-align: center !important;
    border-top: 1px solid var(--vt-line);
    border-bottom: 1px solid var(--vt-line);
    padding: 6px 0;
    margin-bottom: 8px;
}
.st-key-nav [data-testid="stRadio"] {
    display: inline-block !important;
    width: auto !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
}
/* Hide the collapsed widget label so it never offsets the centering. */
.st-key-nav [data-testid="stRadio"] > label,
.st-key-nav [data-testid="stWidgetLabel"] {
    display: none !important;
}
.st-key-nav [role="radiogroup"] {
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: wrap !important;
    justify-content: center !important;
    align-items: center;
    gap: 4px;
}
/* Hide the little radio circles — we want plain text tabs. */
.st-key-nav [role="radiogroup"] label > div:first-child {
    display: none !important;
}
.st-key-nav [role="radiogroup"] label {
    padding: 5px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-family: var(--vt-serif);
    font-size: 1.06rem;
    font-weight: 700;
    color: var(--vt-ink);
    transition: background-color 0.15s ease, color 0.15s ease;
}
.st-key-nav [role="radiogroup"] label:hover {
    background-color: rgba(30, 91, 255, 0.08);
    color: var(--vt-accent);
}
/* Active item: filled block, like the highlighted tab in the mockup. */
.st-key-nav [role="radiogroup"] label:has(input:checked) {
    background-color: #E7E4DC;
    color: var(--vt-ink);
}

/* Navigation now lives in the main column (top nav). Hide the empty
   sidebar and its collapse control so the layout reads like a news site. */
[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {
    display: none !important;
}

/* Legacy sidebar radio styling (kept harmless in case the sidebar is shown). */
[data-testid="stSidebar"] [role="radiogroup"] label {
    padding: 6px 4px;
    border-radius: 6px;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {
    background-color: rgba(30, 91, 255, 0.08);
}

/* ── Expanders: flip the caret to match RTL, give a card-ish frame ──── */
[data-testid="stExpander"] {
    border: 1px solid var(--vt-line);
    border-radius: 8px;
    background-color: #FFFFFF;
    margin-bottom: 8px;
}
[data-testid="stExpander"] details summary {
    direction: rtl;
    font-family: var(--vt-serif);
    font-weight: 700;
    font-size: 1.08rem;
}

/* ── Buttons — outline "pill" style, newspaper-clean ───────────────── */
[data-testid="stMain"] .stButton > button,
.stButton > button {
    border-radius: 999px;
    border: 1px solid var(--vt-ink);
    background-color: transparent;
    color: var(--vt-ink);
    font-family: var(--vt-sans);
    font-weight: 600;
    transition: background-color 0.15s ease, color 0.15s ease,
                border-color 0.15s ease;
}
[data-testid="stMain"] .stButton > button:hover,
.stButton > button:hover {
    background-color: var(--vt-ink);
    color: var(--vt-paper);
    border-color: var(--vt-ink);
}

/* ── Section cards on the Home page (additional decision (b)) ──────── *
 * Streamlit wraps each widget rendered with ``key=foo`` in a div with
 * class ``st-key-foo``. We target those wrappers to make the section-
 * card buttons look like cards rather than buttons. The matching keys
 * used by frontend.pages.home are ``section_card_libya``,
 * ``section_card_middle_east``, ``section_card_world``.
 */
.st-key-section_card_libya button,
.st-key-section_card_middle_east button,
.st-key-section_card_world button {
    height: 88px;
    font-family: var(--vt-serif) !important;
    font-size: 1.45rem !important;
    font-weight: 700 !important;
    border-radius: 8px !important;
    border: 1px solid var(--vt-line) !important;
    background-color: #FFFFFF !important;
    color: var(--vt-ink) !important;
    transition: background-color 0.15s ease, border-color 0.15s ease,
                transform 0.05s ease;
}
.st-key-section_card_libya button:hover,
.st-key-section_card_middle_east button:hover,
.st-key-section_card_world button:hover {
    border-color: var(--vt-accent) !important;
    background-color: rgba(30, 91, 255, 0.05) !important;
    color: var(--vt-accent) !important;
}
.st-key-section_card_libya button:active,
.st-key-section_card_middle_east button:active,
.st-key-section_card_world button:active {
    transform: translateY(1px);
}

/* ── Bias bar — minimal, see components.bias_bar for the markup ────── */
.veritas-bias-bar-wrap {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 6px 0 4px 0;
    direction: ltr;        /* bar fills L→R but caption stays readable */
}
.veritas-bias-bar-label {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    color: var(--vt-muted);
    min-width: 140px;
}
.veritas-bias-bar-track {
    flex: 1;
    height: 8px;
    background-color: #EAE7DE;
    border-radius: 0;
    overflow: hidden;
    position: relative;
}
.veritas-bias-bar-fill {
    height: 100%;
    border-radius: 0;
}
.veritas-bias-bar-score {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.82rem;
    color: var(--vt-muted);
    min-width: 48px;
    text-align: right;
}

/* ── Article headline links (components.article_card) ──────────────── *
 * Big serif headline, ink-colored, that underlines on hover — the classic
 * news-site article-link treatment seen in the reference mockup.
 */
a.veritas-headline {
    font-family: var(--vt-serif);
    font-weight: 700;
    font-size: 1.25rem;
    line-height: 1.4;
    color: var(--vt-ink) !important;
    text-decoration: none;
}
a.veritas-headline:hover {
    color: var(--vt-accent) !important;
    text-decoration: underline;
    text-underline-offset: 3px;
}

/* ── Article and event card framing text ───────────────────────────── */
.veritas-framing {
    font-family: var(--vt-serif);
    font-size: 1.0rem;
    line-height: 1.7;
    color: #3A352C;
    margin-top: 4px;
}
.veritas-meta-line {
    font-size: 0.82rem;
    letter-spacing: 0.3px;
    color: var(--vt-muted);
    margin: 4px 0 8px 0;
}
.veritas-section-badge {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 0;
    background-color: transparent;
    border: 1px solid var(--vt-line);
    color: var(--vt-muted);
    font-size: 0.72rem;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    margin-left: 6px;
}

/* ── Footer ────────────────────────────────────────────────────────── */
.veritas-footer-copy {
    font-family: var(--vt-sans);
    font-size: 0.85rem;
    letter-spacing: 0.5px;
    direction: ltr; /* Keep copyright LTR for English text */
}

/* Style the footer button to look like a clean text link */
.st-key-footer_about_btn button {
    border: none !important;
    background: transparent !important;
    font-family: var(--vt-serif) !important;
    font-size: 1.1rem !important;
    font-weight: 700 !important;
    color: var(--vt-ink) !important;
    padding: 0 !important;
    height: auto !important;
    box-shadow: none !important;
}
.st-key-footer_about_btn button:hover {
    color: var(--vt-accent) !important;
    text-decoration: underline !important;
    text-underline-offset: 3px !important;
    background: transparent !important;
}

/* ── Banners ───────────────────────────────────────────────────────── */
.veritas-error-banner {
    padding: 12px 16px;
    border-radius: 4px;
    background-color: #FBE9E7;
    color: #8E2A1B;
    border: 1px solid #E7B7AE;
    border-right: 4px solid #C0392B;
    margin: 8px 0 16px 0;
}
.veritas-pending-banner {
    padding: 12px 16px;
    border-radius: 4px;
    background-color: #FAF3DE;
    color: #7A5B12;
    border: 1px solid #E6D49A;
    border-right: 4px solid #C99A26;
    margin: 8px 0 16px 0;
}
.veritas-degraded-banner {
    padding: 10px 14px;
    border-radius: 4px;
    background-color: #FAF3DE;
    color: #7A5B12;
    border: 1px solid #E6D49A;
    border-right: 4px solid #C99A26;
    margin: 0 0 12px 0;
    font-size: 0.95rem;
}
.veritas-empty {
    color: var(--vt-muted);
    font-style: italic;
    font-family: var(--vt-serif);
    margin: 8px 0;
}

/* ── Page header layout: title right, refresh left, subtitle under ─── */
.veritas-header-subtitle {
    color: var(--vt-muted);
    font-size: 0.85rem;
    letter-spacing: 0.3px;
    margin-top: -6px;
    margin-bottom: 14px;
}

/* ── Date group header on Section Detail (spec §6.3) ───────────────── */
.veritas-date-group {
    font-family: var(--vt-serif);
    font-size: 1.25rem;
    font-weight: 700;
    margin: 22px 0 8px 0;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--vt-line);
    color: var(--vt-ink);
}

/* ── Section headlines (## headers on Home / lists) — centered kicker ─ *
 * The page-level "## أحدث الأخبار" / "## أحدث الأحداث" headers render as
 * <h2>. Give them a centered, ruled, small-caps newspaper section look.
 */
[data-testid="stMain"] h2 {
    text-align: center;
    font-size: 1.9rem;
    font-weight: 700;
    margin: 10px 0 14px 0;
    padding-bottom: 8px;
    border-bottom: 2px solid var(--vt-ink);
}

/* ── Step 5.2c: Tooltip ⓘ icon (spec §9) ───────────────────────────── */
.veritas-tooltip {
    cursor: help;
    color: var(--vt-accent);
    font-size: 0.8em;
    margin-right: 4px;
    margin-left: 4px;
    user-select: none;
    text-decoration: none;
    vertical-align: baseline;
}
.veritas-tooltip:hover {
    color: #1E40AF;
}

/* ── Step 5.2c: Statistics page chart section header ──────────────── */
.veritas-stats-section {
    font-family: var(--vt-serif);
    font-size: 1.25rem;
    font-weight: 700;
    margin: 22px 0 8px 0;
    color: var(--vt-ink);
    border-right: 3px solid var(--vt-accent);
    padding-right: 10px;
}
.veritas-stats-caption {
    font-size: 0.85rem;
    color: var(--vt-muted);
    font-style: italic;
    margin: 4px 0 12px 0;
}

/* ── Step 5.2c: KPI cards on Statistics page (spec §7.3) ──────────── */
[data-testid="stMetric"] {
    background-color: #FFFFFF;
    border: 1px solid var(--vt-line);
    border-radius: 6px;
    padding: 12px 14px;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
[data-testid="stMetric"]:hover {
    border-color: var(--vt-accent);
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.05);
}
"""


def inject_rtl() -> None:
    """Inject the RTL + bias-bar + card stylesheet exactly once per rerun.

    Streamlit re-runs the entire script on each interaction, so calling
    this every run is correct — the resulting ``<style>`` tag is written
    to the same DOM each time. There is no need to gate it on session
    state.
    """
    st.markdown(f"<style>{RTL_CSS}</style>", unsafe_allow_html=True)
