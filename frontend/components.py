"""
frontend/components.py — Reusable Streamlit components for the dashboard.

Every component is a thin wrapper around ``st.markdown(..., unsafe_allow_html=True)``
or stock Streamlit widgets. No HTTP, no data access — components take a dict
(already fetched by a page) and render it.

Components defined here:

  * ``page_header(title, last_run, help_text=None)`` — spec §3.2
  * ``bias_bar(label, score)``            — spec §2.5
  * ``section_badge(section)``            — Arabic section name pill
  * ``article_card(article, …)``          — spec §4.4 / §6.4
  * ``event_card(event, expanded=False)`` — spec §4.5 / §5.2
  * ``error_banner(detail)``              — spec §10.2
  * ``pending_banner(message=None)``      — spec §10.3
  * ``degraded_banner(component)``        — spec §10.2 (health-failure)
  * ``empty_state(message)``              — spec §10.1 lookup
  * ``trigger_refresh()``                 — sets st.session_state["refresh"] + reruns
  * ``tooltip_icon(help_text)``           — spec §9 ⓘ icon (Step 5.2c)
  * ``tooltip_header(level, text, key)``  — markdown header with inline ⓘ (5.2c)

All Arabic UI strings in this file are taken verbatim from the spec where the
spec quotes them (§10.1, §10.2, §10.3, §5.2 inline labels).
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any, Optional

import streamlit as st

from frontend.styles import ACCENT_COLOR, BIAS_COLORS, BIAS_FALLBACK_COLOR

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# English section ID → Arabic display name. Single source of truth for the
# whole frontend (mirrored in pages.section_detail.SECTION_NAMES_AR).
SECTION_NAMES_AR: dict[str, str] = {
    "libya":       "ليبيا",
    "middle_east": "الشرق الأوسط",
    "world":       "العالم",
}

# Empty-state strings — spec §10.1 verbatim. Keys are short symbolic IDs so
# pages can reference them without hard-coding Arabic.
MESSAGES_AR: dict[str, str] = {
    "no_articles_in_db":       "لم تُجمع أيّ مقالات بعد. شغّل الـ pipeline للحصول على البيانات.",
    "no_events_in_db":         "لم تُكوَّن أيّ أحداث بعد.",
    "section_empty":           "لا توجد مقالات في هذا القسم حالياً.",
    "event_no_summary":        "لم يُنشأ ملخص لهذا الحدث بعد.",
    "event_no_bias_assessment": "لم يُنشأ تحليل لهذا الحدث بعد.",
    "event_no_blindspot":      "لا توجد نقاط عمياء لهذا الحدث (يتطلب تحليل النقاط العمياء تغطيةً من 3 مصادر على الأقل).",
    "event_no_recommendations": "لا توجد توصيات لهذا الحدث.",
    "filter_empty":            "لا توجد نتائج تطابق التصفية الحالية.",
    # Step 5.2c: per-chart empty-state on the Statistics page (spec §10.1
    # generic "no data yet" — used when a chart's data array is [] but the
    # endpoint did not return the pending status).
    "no_data_yet":             "لا توجد بيانات بعد.",
    # §10.2 banners
    "api_error":               "تعذّر الاتصال بالخادم. يرجى المحاولة مرة أخرى.",
    # §10.3
    "pipeline_pending":        "⚠️ لم يُشغَّل الـ pipeline بعد. بعض الإحصائيات قد تكون ناقصة.",
}

# Step 5.2c: tooltip strings for the seven technical terms in spec §9.
# Keys are short symbolic IDs. Strings are spec §9 table verbatim.
TOOLTIPS_AR: dict[str, str] = {
    "bias_label":      "تصنيف التحيّز الأيديولوجي للمقالة، من خمس فئات: pro_government, opposition, neutral, pan_arab, western_aligned.",
    "bias_assessment": "تحليل نقدي مقارن يوضّح كيف تُؤطّر كل مصدر الحدث، باللغة العربية.",
    "blindspot":       "منظور سياسي مفقود في تغطية الحدث — مثلاً، حدث غطّته كل المصادر القومية لكن لم تغطّه المصادر الغربية.",
    "event":           "حدث إخباري واحد تُغطّيه عدّة مقالات من مصادر مختلفة، تم تجميعها تلقائياً بناءً على الزمن والمحتوى والكيانات.",
    "framing":         "جملة عربية تشرح كيف تُؤطّر هذه المقالة الحدث مقارنةً بالمصادر الأخرى.",
    "neutral_summary": "فقرة عربية محايدة تلخّص الحدث بناءً على الحقائق المشتركة بين المصادر، دون لغة منحازة.",
    "pipeline_funnel": "تدرّج المقالات عبر مراحل المعالجة، من جلبها من GDELT حتى تجميعها في أحداث.",
}

# English bias-label ID → Arabic display name. Mirrors the descriptions in
# frontend/pages/about.py so blindspot perspectives render in Arabic.
BIAS_LABELS_AR: dict[str, str] = {
    "pro_government":  "مؤيد للحكومة",
    "opposition":      "معارض",
    "neutral":         "محايد",
    "pan_arab":        "قومي عربي",
    "western_aligned": "متوافق مع الغرب",
}

# Inline labels used inside event_card. Spec §5.2.
_EVENT_LABEL_SUMMARY: str = "◾ الملخص المحايد"
_EVENT_LABEL_BIAS_ASSESSMENT: str = "◾ التحليل النقدي للتأطير"
_EVENT_LABEL_ARTICLES: str = "◾ المقالات المُكوّنة للحدث"
_EVENT_LABEL_BLINDSPOTS: str = "◾ النقاط العمياء"
_EVENT_LABEL_RECOMMENDATIONS: str = "◾ مقالات مقترحة بمنظور مختلف"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: Optional[str]) -> str:
    """HTML-escape user-supplied content before embedding in raw HTML markup.

    The API returns Arabic strings with no markup of its own; this guards
    against the rare case of an article title containing ``<`` or ``&`` that
    would otherwise break our HTML structure.
    """
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def _fmt_date(iso_str: Optional[str]) -> str:
    """Render an ISO-8601 timestamp as a ``YYYY-MM-DD`` date string.

    Best-effort: on parse failure, returns the original string. Per spec
    §2.2, dates render with Western digits and Gregorian calendar; this
    helper preserves both since Python's ``isoformat()`` already emits
    Western digits.
    """
    if not iso_str:
        return ""
    s = iso_str.replace("Z", "+00:00") if isinstance(iso_str, str) else str(iso_str)
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return str(iso_str)
    return dt.date().isoformat()


def _fmt_datetime_min(iso_str: Optional[str]) -> str:
    """Render an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM`` (no seconds).

    Used by the page header's ``آخر تشغيل`` subtitle (spec §3.2).
    """
    if not iso_str:
        return ""
    s = iso_str.replace("Z", "+00:00") if isinstance(iso_str, str) else str(iso_str)
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return str(iso_str)
    return dt.strftime("%Y-%m-%d %H:%M")


def _articles_count_ar(n: int) -> str:
    """Render an article count as ``N مقال`` (number + singular noun).

    Modern Arabic conventionally accepts the digit-prefixed singular form
    in counter contexts. Avoids the dual/plural feminine variations that
    would require more elaborate pluralisation logic.
    """
    return f"{n} مقال"


# ─────────────────────────────────────────────────────────────────────────────
# Refresh trigger (used by page_header + error_banner retry button)
# ─────────────────────────────────────────────────────────────────────────────

def trigger_refresh() -> None:
    """Mark the next rerun as a cache-bypassing refresh, then rerun immediately.

    The flag is read and popped in ``frontend/app.py`` and forwarded to the
    page's ``render(refresh=True)`` call, which threads it down to the
    relevant ``api_client.get_*`` function. This is the only path the
    dashboard uses to bypass FastAPI's 5-minute Redis cache.
    """
    st.session_state["refresh"] = True
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Tooltips (spec §9) — Step 5.2c
# ─────────────────────────────────────────────────────────────────────────────

def tooltip_icon(help_text: str) -> str:
    """Return inline HTML for a hoverable ⓘ icon.

    Uses the browser-native ``title=`` attribute so no JavaScript is needed
    and the tooltip works identically across all browsers. The icon is
    styled by ``.veritas-tooltip`` in styles.py.

    The returned string is intended to be concatenated into a larger
    ``st.markdown(..., unsafe_allow_html=True)`` payload (e.g., next to a
    section header). The help text is HTML-escaped so embedded quotes /
    angle brackets cannot break the ``title=`` attribute.
    """
    safe = _esc(help_text)
    return f"<span class='veritas-tooltip' title='{safe}'>ⓘ</span>"


def tooltip_header(level: int, text: str, help_key_or_text: str) -> None:
    """Render a markdown header with an inline ⓘ tooltip to its right.

    ``level`` is 1–6 (mapped to ``#``–``######``). ``help_key_or_text`` is
    looked up in ``TOOLTIPS_AR``; if not found, treated as a literal
    Arabic tooltip string. Falls back to a plain header (no icon) if the
    lookup yields an empty value.
    """
    level = max(1, min(6, int(level)))
    hashes = "#" * level
    help_text = TOOLTIPS_AR.get(help_key_or_text, help_key_or_text)
    if not help_text:
        st.markdown(f"{hashes} {_esc(text)}", unsafe_allow_html=False)
        return
    # Combine the markdown header and the inline HTML icon in one block.
    # The ⓘ icon trails the text so RTL layout places it visually after
    # (i.e., to the left of) the header text — natural for Arabic reading.
    st.markdown(
        f"{hashes} {_esc(text)} {tooltip_icon(help_text)}",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Page header
# ─────────────────────────────────────────────────────────────────────────────

def page_header(
    title: str,
    last_run: Optional[str],
    *,
    help_text: Optional[str] = None,
) -> None:
    """Render the per-page header: title (right), refresh button (left), subtitle.

    ``last_run`` is the ISO timestamp from ``/api/health``. ``None`` means the
    pipeline has never run; we render ``آخر تشغيل: غير متاح`` to match the
    additional decision in the task spec.

    Step 5.2c: ``help_text`` is an optional spec §9 tooltip string. When
    provided, an inline ⓘ icon is rendered next to the page title with
    the given text as a hover tooltip. Pass either a key from
    ``TOOLTIPS_AR`` (recommended) or a literal Arabic string; lookup
    is performed inside ``tooltip_icon``-style fallback semantics by
    the caller's pre-lookup (we just embed whatever ``help_text`` is).
    """
    col_title, col_refresh = st.columns([5, 1])
    with col_title:
        if help_text:
            icon_html = tooltip_icon(help_text)
            st.markdown(f"# {_esc(title)} {icon_html}", unsafe_allow_html=True)
        else:
            st.markdown(f"# {_esc(title)}", unsafe_allow_html=True)
    with col_refresh:
        # Vertical spacer so the button visually aligns with the title's
        # baseline. ``st.markdown`` accepts simple HTML for an empty div.
        st.markdown("<div style='height: 24px'></div>", unsafe_allow_html=True)
        if st.button("⟳ تحديث", key="refresh_btn", use_container_width=True):
            trigger_refresh()

    if last_run:
        subtitle = f"آخر تشغيل: {_fmt_datetime_min(last_run)}"
    else:
        subtitle = "آخر تشغيل: غير متاح"
    st.markdown(
        f"<div class='veritas-header-subtitle'>{_esc(subtitle)}</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bias bar (spec §2.5)
# ─────────────────────────────────────────────────────────────────────────────

def bias_bar(label: str, score: float) -> None:
    """Render the visual horizontal bar for a single bias score.

    Fill width is ``abs(score) * 100%`` clamped to [0, 100]. The bar fill
    is drawn left-to-right regardless of page direction — the bar wrapper
    forces ``direction: ltr`` (see styles.RTL_CSS). The label stays in
    English per spec §2.2.
    """
    try:
        score_f = float(score)
    except (TypeError, ValueError):
        score_f = 0.0
    fill_pct = max(0.0, min(1.0, abs(score_f))) * 100.0
    color = BIAS_COLORS.get(label, BIAS_FALLBACK_COLOR)
    sign_str = f"{score_f:+.2f}".rstrip("0").rstrip(".")
    # If the strip removed the decimal entirely (e.g. "+0"), re-add ".0"
    # for visual consistency: "+0.0" is clearer than "+0".
    if "." not in sign_str:
        sign_str = f"{score_f:+.1f}"

    label_text = _esc(label)
    st.markdown(
        f"""
        <div class='veritas-bias-bar-wrap'>
          <span class='veritas-bias-bar-label'>{label_text}</span>
          <span class='veritas-bias-bar-track'>
            <span class='veritas-bias-bar-fill'
                  style='width: {fill_pct:.1f}%; background-color: {color};'></span>
          </span>
          <span class='veritas-bias-bar-score'>{_esc(sign_str)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Section badge
# ─────────────────────────────────────────────────────────────────────────────

def section_badge(section: str) -> str:
    """Return the HTML markup for the inline Arabic-section pill.

    Returns a string (not an ``st.markdown`` call) so callers can embed
    it inside a larger ``st.markdown`` payload alongside other inline
    elements (e.g., the meta line in ``event_card``).
    """
    name_ar = SECTION_NAMES_AR.get(section, section or "")
    return f"<span class='veritas-section-badge'>{_esc(name_ar)}</span>"


# ─────────────────────────────────────────────────────────────────────────────
# Banners
# ─────────────────────────────────────────────────────────────────────────────

def error_banner(detail: str = "", *, key_suffix: str = "") -> None:
    """Render the §10.2 red error banner with a retry button.

    ``detail`` is shown in a smaller line under the spec-mandated message.
    The retry button just reruns the script; the next render will retry
    the failing call. ``key_suffix`` lets a page distinguish multiple
    banners on the same render (e.g., health-error vs page-error).
    """
    msg = _esc(MESSAGES_AR["api_error"])
    detail_html = (
        f"<div style='font-size:0.85rem;color:#7F1D1D;margin-top:4px;'>"
        f"{_esc(detail)}</div>"
        if detail else ""
    )
    st.markdown(
        f"<div class='veritas-error-banner'><strong>{msg}</strong>{detail_html}</div>",
        unsafe_allow_html=True,
    )
    btn_key = f"retry_btn{('_' + key_suffix) if key_suffix else ''}"
    if st.button("إعادة المحاولة", key=btn_key):
        # On retry we deliberately do NOT set the refresh flag — the user is
        # asking for a re-attempt of the same call, not a cache bypass.
        st.rerun()


def pending_banner(message: Optional[str] = None) -> None:
    """Render the §10.3 pending banner.

    Used when an endpoint returns ``{"status":"pending", ...}`` — i.e.,
    the system has 0 articles, 0 events, and no Redis ``results:{section}``
    keys (api/main.py ``_is_system_pending``).
    """
    text = message or MESSAGES_AR["pipeline_pending"]
    st.markdown(
        f"<div class='veritas-pending-banner'>{_esc(text)}</div>",
        unsafe_allow_html=True,
    )


def degraded_banner(component: str) -> None:
    """Render the §10.2 degraded-mode banner (DB or Redis disconnected).

    ``component`` is the human-readable component name (e.g. "قاعدة البيانات"
    or "Redis"). Banner appears at the very top of every page when
    ``/api/health`` reports ``db == "disconnected"`` or ``redis ==
    "disconnected"``.
    """
    msg = f"⚠️ النظام يعمل بوضع محدود: {component} غير متصل."
    st.markdown(
        f"<div class='veritas-degraded-banner'>{_esc(msg)}</div>",
        unsafe_allow_html=True,
    )


def empty_state(key_or_message: str) -> None:
    """Render an empty-state message in italic gray.

    Pass a key from ``MESSAGES_AR`` to look up the spec-canonical wording,
    or pass an arbitrary Arabic string to render it directly. Lookup is
    "key-first": if the argument exists in ``MESSAGES_AR``, that wording
    wins.
    """
    text = MESSAGES_AR.get(key_or_message, key_or_message)
    st.markdown(
        f"<div class='veritas-empty'>{_esc(text)}</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Article card (Home §4.4 and Section Detail §6.4)
# ─────────────────────────────────────────────────────────────────────────────

def article_card(
    article: dict[str, Any],
    *,
    show_section_badge: bool = False,
    show_source: bool = True,
) -> None:
    """Render one article row.

    Two callsites differ slightly:
      * Home (§4.4): title + section badge + source-domain hyperlink +
        bias bar + framing. Pass ``show_section_badge=True, show_source=True``.
      * Section Detail (§6.4): title-as-link only (no separate source
        field, no section badge — the whole page is already a section).
        Pass ``show_section_badge=False, show_source=False``.

    Empty-state per task spec: when ``bias`` is null, render only the
    title (+ optional source/section line) and a small framing-not-
    available note. Do NOT render an empty bias bar.
    """
    title = _esc(article.get("title") or "")
    url = article.get("url") or ""
    url_safe = _esc(url)

    # Title is always a clickable hyperlink to the original article.
    title_html = (
        f"<a class='veritas-headline' href='{url_safe}' target='_blank' "
        f"rel='noopener noreferrer'>{title}</a>"
    )
    st.markdown(title_html, unsafe_allow_html=True)

    # Meta line: section badge (optional) + source domain hyperlink (optional).
    meta_parts: list[str] = []
    if show_section_badge and article.get("section"):
        meta_parts.append(section_badge(article["section"]))
    if show_source and article.get("source"):
        src = _esc(article["source"])
        meta_parts.append(
            f"<a href='{url_safe}' target='_blank' rel='noopener noreferrer' "
            f"style='color:#6B7280;text-decoration:none;'>{src}</a>"
        )
    if meta_parts:
        meta_html = " · ".join(meta_parts)
        st.markdown(
            f"<div class='veritas-meta-line'>{meta_html}</div>",
            unsafe_allow_html=True,
        )

    # Bias bar — only when bias is present (Q5 of api spec: framing can be
    # empty string; that's fine, we render the empty string below).
    bias = article.get("bias")
    if bias and bias.get("label"):
        bias_bar(bias["label"], bias.get("score", 0.0))
        framing = bias.get("framing") or ""
        if framing:
            st.markdown(
                f"<div class='veritas-framing'>{_esc(framing)}</div>",
                unsafe_allow_html=True,
            )
        else:
            # Per spec, framing is always visible — render a small placeholder
            # so the user sees the row reserved space for it.
            st.markdown(
                "<div class='veritas-framing' style='color:#9CA3AF;'>"
                "—</div>",
                unsafe_allow_html=True,
            )
    else:
        # No bias: skip the bar entirely, render a tiny notice in lieu of framing.
        st.markdown(
            "<div class='veritas-framing' style='color:#9CA3AF;'>"
            "تحليل التأطير غير متاح بعد.</div>",
            unsafe_allow_html=True,
        )

    # Light visual separator between successive cards.
    st.markdown("<hr style='margin:12px 0;border:0;border-top:1px solid var(--vt-line);'/>",
                unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Event card (Home §4.5 and All Events §5.2)
# ─────────────────────────────────────────────────────────────────────────────

def _render_recommendation(rec: dict[str, Any]) -> None:
    """Render one cross-bias recommendation row inside an event card.

    Title is a clickable link to the recommended article's URL. If the URL
    is missing (api/main.py:461 returns ``url: None`` for deleted articles)
    we render the title as plain text — the user can still read it.
    """
    title = _esc(rec.get("title") or "")
    url = rec.get("url") or ""
    bias_label = rec.get("bias_label") or ""
    sim = rec.get("similarity")
    try:
        sim_f = float(sim) if sim is not None else None
    except (TypeError, ValueError):
        sim_f = None

    if url:
        url_safe = _esc(url)
        title_html = (
            f"<a href='{url_safe}' target='_blank' rel='noopener noreferrer' "
            f"style='font-weight:500;color:{ACCENT_COLOR};'>{title}</a>"
        )
    else:
        title_html = (
            f"<span style='font-weight:500;'>{title}</span>"
            f" <span style='color:#9CA3AF;font-size:0.85rem;'>(الرابط غير متاح)</span>"
        )

    parts: list[str] = []
    if bias_label:
        parts.append(_esc(bias_label))
    if sim_f is not None:
        parts.append(f"similarity: {sim_f:.2f}")
    meta = " · ".join(parts)

    st.markdown(
        f"<div style='margin:6px 0;'>{title_html}"
        + (f"<div class='veritas-meta-line'>{_esc(meta)}</div>" if meta else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def event_card(event: dict[str, Any], expanded: bool = False) -> None:
    """Render one event as an expandable card (spec §4.5 / §5.2).

    Spec §5.2 ordering, enforced in this function:
      1. meta line (section badge + N مقال + created date)
      2. ◾ الملخص المحايد + summary (or empty-state)
      3. ◾ التحليل النقدي للتأطير + bias_assessment (or empty-state)
      4. ◾ المقالات المُكوّنة للحدث (N مقال) + per-article cards
      5. ◾ النقاط العمياء + missing perspectives list (or empty-state)
      6. ◾ مقالات مقترحة بمنظور مختلف + top-3 recommendations (or empty-state)
    """
    headline = event.get("headline") or "—"
    section = event.get("section") or ""
    article_count = int(event.get("article_count") or 0)
    created_at = _fmt_date(event.get("created_at"))
    summary = event.get("summary")
    bias_assessment = event.get("bias_assessment")
    articles = event.get("articles") or []
    blindspot = event.get("blindspot") or {}
    missing = blindspot.get("missing_perspectives") if isinstance(blindspot, dict) else None
    recommendations = event.get("recommendations") or []

    with st.expander(headline, expanded=expanded):
        # ── 1) Meta line ────────────────────────────────────────────────
        meta_parts: list[str] = []
        if section:
            meta_parts.append(section_badge(section))
        meta_parts.append(_esc(_articles_count_ar(article_count)))
        if created_at:
            meta_parts.append(_esc(created_at))
        st.markdown(
            f"<div class='veritas-meta-line'>{' · '.join(meta_parts)}</div>",
            unsafe_allow_html=True,
        )

        # ── 2) Neutral summary ─────────────────────────────────────────
        st.markdown(
            f"### {_esc(_EVENT_LABEL_SUMMARY)}",
            unsafe_allow_html=True,
        )
        if summary:
            st.markdown(_esc(summary).replace("\n", "<br/>"), unsafe_allow_html=True)
        else:
            empty_state("event_no_summary")

        # ── 3) Bias assessment ─────────────────────────────────────────
        st.markdown(
            f"### {_esc(_EVENT_LABEL_BIAS_ASSESSMENT)}",
            unsafe_allow_html=True,
        )
        if bias_assessment:
            st.markdown(_esc(bias_assessment).replace("\n", "<br/>"), unsafe_allow_html=True)
        else:
            empty_state("event_no_bias_assessment")

        # ── 4) Constituent articles ────────────────────────────────────
        st.markdown(
            f"### {_esc(_EVENT_LABEL_ARTICLES)} "
            f"<span style='font-weight:400;font-size:0.9rem;color:#6B7280;'>"
            f"({_esc(_articles_count_ar(article_count))})</span>",
            unsafe_allow_html=True,
        )
        if articles:
            for art in articles:
                article_card(art, show_section_badge=False, show_source=True)
        else:
            empty_state("section_empty")  # closest match — "no articles in this group"

        # ── 5) Blindspots ──────────────────────────────────────────────
        st.markdown(
            f"### {_esc(_EVENT_LABEL_BLINDSPOTS)}",
            unsafe_allow_html=True,
        )
        if isinstance(missing, list) and len(missing) > 0:
            # Render as a comma-separated list of Arabic perspective names,
            # falling back to the technical ID for any unmapped label.
            labels_html = "، ".join(
                _esc(BIAS_LABELS_AR.get(m, m)) for m in missing
            )
            st.markdown(
                f"<div>المنظورات المفقودة: <strong>{labels_html}</strong></div>",
                unsafe_allow_html=True,
            )
        else:
            empty_state("event_no_blindspot")

        # ── 6) Recommendations ─────────────────────────────────────────
        st.markdown(f"### {_esc(_EVENT_LABEL_RECOMMENDATIONS)}", unsafe_allow_html=True)
        if recommendations:
            # Spec §5.2: "top 3 cross-bias recommendations." Defensive slice.
            for rec in list(recommendations)[:3]:
                _render_recommendation(rec)
        else:
            empty_state("event_no_recommendations")
