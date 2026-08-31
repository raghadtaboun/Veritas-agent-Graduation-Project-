# Phase 5 — UI Specification

> **Purpose:** This document defines the complete UI surface of Veritas Agent's Phase 5 deliverable: a FastAPI backend and Streamlit dashboard. It is a reference document for the AI coding agent (Cursor). Implementation steps live in `plan.md` Phase 5. This file describes **what** the UI must show and how it behaves, not **how** to build it.
>
> **Scope:** All five pages, all components, all interactions, all data shapes. Read this file before starting any Phase 5 sub-step.
>
> **Authority:** When this document and `plan.md` Phase 5 disagree, `plan.md` wins on architecture and `agent.md` wins on rules. This file is authoritative on UI content, layout, language, branding, and user interactions.

---

## 1. Architecture Overview

### 1.1 Two services, separate processes

```
┌────────────────┐      ┌────────────────┐      ┌──────────────────┐
│   Streamlit    │─HTTP─│    FastAPI     │─────│  PostgreSQL +    │
│   Dashboard    │      │    Backend     │      │  Redis (existing)│
│ localhost:8501 │      │ localhost:8001 │      │                  │
└────────────────┘      └────────────────┘      └──────────────────┘
```

- **FastAPI backend** is the only component that touches PostgreSQL and Redis. It exposes JSON endpoints. It does not trigger pipeline runs and does not call any LLM.
- **Streamlit dashboard** is a pure HTTP client of FastAPI. It does not import `psycopg2`, `redis`, or any agent module. It uses `httpx` for backend calls.

This boundary is non-negotiable: Streamlit must remain swappable for any other frontend later.

### 1.2 Data freshness model

- The dashboard is **read-only**. It never triggers pipeline runs.
- The scheduler (Phase 6) writes `results:{section}` to Redis after every pipeline completion. FastAPI reads from this cache first, falls back to PostgreSQL only when the cache is empty.
- Manual refresh: every page has a Refresh button (top-right). Clicking it forces FastAPI to bypass its own Redis cache for the next request.

### 1.3 Caching policy (FastAPI layer)

Each endpoint caches its full response in Redis for **5 minutes** under a deterministic key. The cache is in addition to (not a replacement for) the `results:{section}` pipeline cache. Cache keys:

| Endpoint | Cache key | TTL |
|---|---|---|
| `/api/health` | (no cache) | — |
| `/api/home` | `api:home` | 300 s |
| `/api/events` | `api:events:{filters_hash}` | 300 s |
| `/api/events/{id}` | `api:event:{id}` | 300 s |
| `/api/sections/{section}` | `api:section:{section}:{filters_hash}` | 300 s |
| `/api/statistics` | `api:statistics` | 300 s |

The Refresh button on the dashboard appends `?refresh=true` to the FastAPI request, which causes FastAPI to delete the relevant cache key before recomputing.

---

## 2. Branding and Visual Language

### 2.1 Identity

- **Product name:** `Veritas Agent` — text only, no logo image.
- **Header layout:** Product name on the right side (RTL), Refresh button on the left side, last-update timestamp under the product name.
- **Tagline (optional, smaller text under name):** `نظام تحليل الانحياز في الأخبار العربية`

### 2.2 Language policy

- **UI chrome and labels:** Arabic (page titles, menu items, button text, headers, tooltips, status messages).
- **Bias labels:** English (`pro_government`, `opposition`, `neutral`, `pan_arab`, `western_aligned`). They are technical identifiers, not UI strings.
- **Content (article titles, summaries, framings, bias_assessments):** Arabic as stored in the database.
- **Numbers and dates:** Western digits (`0–9`), Gregorian calendar, ISO-ish format (`2026-05-12`) — not Arabic-Indic digits.

### 2.3 Direction

- **Direction:** RTL throughout. All pages, sidebars, tables, cards.
- **Streamlit doesn't apply RTL automatically** — the implementation must inject CSS (`direction: rtl; text-align: right;`) at the page root.
- **Charts:** axis labels can stay LTR (technical convention); the surrounding chart title and legend should be RTL Arabic where the chart sits in a labeled section.

### 2.4 Theme

- **Theme:** Streamlit default (auto-detect — respects user's OS preference).
- **Accent color:** Blue (`#1E5BFF` or Streamlit's default blue). Used for buttons, links, active nav items, and chart primary series.
- Do not impose dark or light mode programmatically — leave the theme picker in the user's hands.

### 2.5 Bias label visualization

Bias labels appear as **score bars** (visual horizontal bar) rather than badges or icons. Format:

```
pan_arab      ▓▓▓▓▓▓▓▓░░  +0.8
opposition    ▓▓▓▓▓░░░░░  +0.5
neutral       ▓░░░░░░░░░  -0.1
```

- The bar fills proportionally to `abs(score)` on a 0–1 scale.
- The label is shown in English next to the bar.
- The signed score is shown as a number, sign included.
- **Confidence is not shown** anywhere in the article-level UI. It is only used in the Statistics page (Section 7) as an aggregate metric.

### 2.6 Color coding for bias labels

Use a consistent palette for bars and chart series across all pages:

| Label | Color (suggested) |
|---|---|
| `pro_government` | `#3B82F6` (blue) |
| `opposition` | `#F59E0B` (amber) |
| `neutral` | `#9CA3AF` (gray) |
| `pan_arab` | `#EF4444` (red) |
| `western_aligned` | `#8B5CF6` (purple) |

These colors must be defined as a single constant dict in the dashboard code and reused in every chart and bar.

---

## 3. Navigation

### 3.1 Sidebar (always visible)

```
┌──────────────────────┐
│  Veritas Agent       │
│                      │
│  🏠 الرئيسية          │
│  📰 الأحداث           │
│  ──────────          │
│  🇱🇾 ليبيا             │
│  🌍 الشرق الأوسط       │
│  🌐 العالم             │
│  ──────────          │
│  📊 الإحصائيات         │
└──────────────────────┘
```

- The currently selected item is highlighted with the accent color.
- Section pages (Libya, ME, World) link directly to the section-detail page.
- The sidebar is the **only** navigation surface. Do not add a top bar with redundant links.

### 3.2 Page header (per page)

Every page renders the same header structure under the sidebar:

- **Right side:** page title in Arabic, large bold.
- **Left side:** Refresh button (icon `⟳` + label `تحديث`). Clicking it forces a cache bypass and re-fetches data.
- **Under the title (small, secondary color):** last pipeline run timestamp, formatted as `آخر تشغيل: 2026-05-12 14:30` — sourced from `/api/health`.

---

## 4. Page 1 — Home (`الرئيسية`)

### 4.1 Purpose

A landing page that previews the system's current state through three components: section access, latest articles, and latest events. **No KPI cards on this page.** All numeric KPIs live exclusively in the Statistics page.

### 4.2 Layout (top to bottom)

```
┌──────────────────────────────────────────────────────┐
│ Page Header (title: الرئيسية, Refresh button)         │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section 1 — Section Cards (3 cards, clickable)      │
│  [ليبيا]  [الشرق الأوسط]  [العالم]                    │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section 2 — Latest News (5 articles)                │
│  • article 1                                          │
│  • article 2                                          │
│  • article 3                                          │
│  • article 4                                          │
│  • article 5                                          │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section 3 — Latest Events (3 events, expandable)    │
│  ┌────────────────────────────────────────────────┐ │
│  │ event 1 (collapsed by default — title only)    │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │ event 2                                         │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │ event 3                                         │ │
│  └────────────────────────────────────────────────┘ │
│                                                       │
│  [ عرض كل الأحداث ← ] (link to Page 2)               │
│                                                       │
└──────────────────────────────────────────────────────┘
```

### 4.3 Section 1 — Section Cards

Three cards in a horizontal row, RTL ordered (Libya → ME → World becomes ME → Libya → World visually). Each card contains **only the section's Arabic name** centered. Examples of card content:

- `ليبيا`
- `الشرق الأوسط`
- `العالم`

No counts, no stats, no images. The entire card is a clickable surface that navigates to Page 3 (Section Detail) for that section.

### 4.4 Section 2 — Latest News (5 articles)

The 5 newest articles across the system, selected by the rule:

> **One latest article from each section (3 total) + the 2 newest articles from the remaining pool.**

Implementation:

1. For each of the 3 sections, fetch the single most recent article by `published_at` (descending). That gives 3 articles, one per section.
2. From all articles excluding those 3, fetch the 2 most recent. That gives 2 more.
3. Sort the resulting 5 by `published_at` descending for display.

Each article row renders:

```
┌────────────────────────────────────────────────────┐
│ title (Arabic)                                      │
│                                                     │
│ [section badge] · [source domain — hyperlinked]    │
│ [bias score bar with English label]                 │
│ framing (Arabic, always visible)                    │
└────────────────────────────────────────────────────┘
```

- **Title:** Arabic, full text (no truncation), medium font weight.
- **Section badge:** Arabic section name, small, secondary background.
- **Source:** the article's source domain (e.g., `aljazeera.net`) rendered as a clickable hyperlink pointing to the article's URL. The displayed text is the domain; the `href` is the full URL. Open in new tab.
- **Bias score bar:** as defined in §2.5 — visual bar + English label + signed score.
- **Framing:** Arabic sentence stored in `bias_scores.framing`, displayed below the bar in smaller text. Always visible (not collapsed).

### 4.5 Section 3 — Latest Events (3 events)

The 3 newest events globally, ordered by `created_at` descending.

Each event is rendered as an **expandable card** — collapsed by default, showing only the headline. When expanded, the card shows the full event detail (see §5.2 for the event detail shape — Page 1 and Page 2 share the same expanded layout).

Below the three event cards, a single text link:

```
عرض كل الأحداث ←
```

This link navigates to Page 2 (All Events).

---

## 5. Page 2 — All Events (`الأحداث`)

### 5.1 Layout

```
┌──────────────────────────────────────────────────────┐
│ Page Header (title: الأحداث, Refresh button)          │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Filters bar:                                         │
│  [Section: All | Libya | ME | World]                  │
│  [Date range: from ___ to ___] (optional)             │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Event card 1 (expandable, collapsed by default)      │
│  Event card 2                                          │
│  Event card 3                                          │
│  ...                                                   │
│                                                       │
│  Pagination: << < page 2 of 7 > >>                    │
│                                                       │
└──────────────────────────────────────────────────────┘
```

### 5.2 Event card — expanded layout (shared with Home §4.5)

When expanded, an event card shows in this exact order:

```
╔════════════════════════════════════════════════════════╗
║  EVENT HEADLINE (Arabic, large bold)                   ║
║                                                         ║
║  [section badge] · [N articles] · [created date]       ║
╠════════════════════════════════════════════════════════╣
║                                                         ║
║  ◾ الملخص المحايد                                       ║
║  [Neutral summary paragraph, Arabic]                   ║
║                                                         ║
║  ◾ التحليل النقدي للتأطير                               ║
║  [Bias assessment paragraph, Arabic]                   ║
║                                                         ║
║  ◾ المقالات المُكوّنة للحدث  (N articles)               ║
║  ┌────────────────────────────────────────────────┐   ║
║  │ • article 1 title — linked to URL              │   ║
║  │   [bias score bar with English label]          │   ║
║  │   framing                                       │   ║
║  ├────────────────────────────────────────────────┤   ║
║  │ • article 2 title — linked to URL              │   ║
║  │   [bias score bar with English label]          │   ║
║  │   framing                                       │   ║
║  └────────────────────────────────────────────────┘   ║
║                                                         ║
║  ◾ النقاط العمياء                                       ║
║  Missing perspectives: pro_government, opposition      ║
║  (or "لا توجد نقاط عمياء" if none)                     ║
║                                                         ║
║  ◾ مقالات مقترحة بمنظور مختلف                          ║
║  ┌────────────────────────────────────────────────┐   ║
║  │ • rec article 1 title — linked to URL          │   ║
║  │   [bias label] · similarity: 0.84               │   ║
║  └────────────────────────────────────────────────┘   ║
║                                                         ║
╚════════════════════════════════════════════════════════╝
```

Notes on the sub-sections:

- **Headline + meta:** event title from `events.headline`. Meta line shows section name, article count, and creation date.
- **Neutral summary:** `events.summary` field. Show empty-state message if NULL: `لم يُنشأ ملخص لهذا الحدث بعد.`
- **Bias assessment:** `events.bias_assessment` field. Show empty-state message if NULL: `لم يُنشأ تحليل لهذا الحدث بعد.`
- **Constituent articles:** all articles in the event via the `article_events` junction. Each article: title (clickable link to URL), bias score bar, framing.
- **Blindspots:** from `blindspot_reports.missing_perspectives`. If the array is empty or no row exists, show the no-blindspots message.
- **Recommendations:** from the Redis key `recommend:{first_article_id_in_event}`. Show top 3 cross-bias recommendations, each as a clickable title with its bias label and similarity score. If Redis is missing the recommendation, show: `لا توجد توصيات لهذا الحدث.`

### 5.3 Filters

- **Section filter:** dropdown `الكل | ليبيا | الشرق الأوسط | العالم`. Default: `الكل`.
- **Date range:** optional pair of date pickers. If both empty, return all events.

### 5.4 Pagination

10 events per page. Pagination control at the bottom of the events list.

---

## 6. Page 3 — Section Detail (×3: Libya, ME, World)

### 6.1 Layout

```
┌──────────────────────────────────────────────────────┐
│ Page Header (title: ليبيا, Refresh button)            │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Filter: تصفية حسب التحيّز                             │
│  [Multi-select dropdown]                              │
│    ▢ pro_government                                   │
│    ▢ opposition                                       │
│    ▢ neutral                                          │
│    ▢ pan_arab                                         │
│    ▢ western_aligned                                  │
│  (default: all selected)                              │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  📅 اليوم                                              │
│  ┌────────────────────────────────────────────────┐ │
│  │ article 1 — linked title                        │ │
│  │ [bias score bar with English label]             │ │
│  │ framing                                          │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │ article 2                                        │ │
│  └────────────────────────────────────────────────┘ │
│                                                       │
│  📅 الأمس                                              │
│  ┌────────────────────────────────────────────────┐ │
│  │ article 3                                        │ │
│  └────────────────────────────────────────────────┘ │
│                                                       │
│  📅 2026-05-09                                         │
│  ┌────────────────────────────────────────────────┐ │
│  │ article 4                                        │ │
│  │ article 5                                        │ │
│  └────────────────────────────────────────────────┘ │
│                                                       │
│  Pagination at bottom (20 articles per page)         │
│                                                       │
└──────────────────────────────────────────────────────┘
```

### 6.2 Bias filter (multi-select)

- A multi-select dropdown labeled `تصفية حسب التحيّز`.
- Options are the 5 English bias labels.
- Default state: all five selected (show everything).
- Filter applies in real time — selecting/deselecting a label refilters the article list without reloading the page.
- An "all" / "none" pair of helper buttons under the dropdown is optional.

### 6.3 Date grouping

Articles are grouped under date headers. Date headers display:

- **Today's date** → `اليوم`
- **Yesterday's date** → `الأمس`
- **Earlier dates** → ISO format `YYYY-MM-DD` (e.g., `2026-05-09`)

Groups are ordered most-recent first. Within each group, articles are ordered by `published_at` descending.

### 6.4 Article card

Each article in the section list renders:

```
┌────────────────────────────────────────────────────┐
│ title (Arabic) — clickable hyperlink to URL         │
│ [bias score bar with English label]                 │
│ framing (Arabic, always visible)                    │
└────────────────────────────────────────────────────┘
```

- **Title:** the article title from `articles.title`, rendered as a clickable hyperlink whose `href` is the article's `url`. Opens in a new tab.
- **Source is shown via the linked title itself** — the title text is the link, and clicking it opens the original article. No separate source-name field on this page (the title is already the gateway to the source).
- **Bias score bar:** as defined in §2.5.
- **Framing:** Arabic sentence, always visible.

### 6.5 Pagination

20 articles per page. Pagination control at the bottom.

---

## 7. Page 4 — Statistics (`الإحصائيات`)

### 7.1 Purpose

A single page that aggregates all numeric KPIs and analytical charts. This is where the supervisor sees the system's scope and quality at a glance.

### 7.2 Layout

```
┌──────────────────────────────────────────────────────┐
│ Page Header (title: الإحصائيات, Refresh button)        │
├──────────────────────────────────────────────────────┤
│                                                       │
│  KPI Cards Row (4 cards)                              │
│  ┌─────────┐ ┌─────────┐ ┌────────┐ ┌──────────┐    │
│  │ Articles │ │ Events  │ │Blindspot│ │ Sources  │    │
│  │   487   │ │   23    │ │    7   │ │    31    │    │
│  └─────────┘ └─────────┘ └────────┘ └──────────┘    │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section A — Pipeline Funnel (horizontal bar chart)  │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section B — Bias Distribution (bar chart, 5 bars)   │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section C — Articles per Section (3 bars)           │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section D — Articles per Source (top 10, bar chart) │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section E — Events per Section (3 bars)             │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section F — Blindspots per Section (3 bars)         │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section G — Average Confidence per Bias Label       │
│  (5 bars, 0.0 to 1.0)                                │
│                                                       │
├──────────────────────────────────────────────────────┤
│                                                       │
│  Section H — Articles per Event Distribution         │
│  (histogram: 1 article, 2, 3, 4, 5+)                 │
│                                                       │
└──────────────────────────────────────────────────────┘
```

### 7.3 KPI cards (row of 4)

| KPI | SQL semantics |
|---|---|
| Articles | `SELECT COUNT(*) FROM articles` |
| Events | `SELECT COUNT(*) FROM events` |
| Blindspots | `SELECT COUNT(*) FROM blindspot_reports` |
| Sources | `SELECT COUNT(DISTINCT source) FROM articles` |

Each card: large number on top, Arabic label below (`مقالات`, `أحداث`, `نقاط عمياء`, `مصادر`).

### 7.4 Section A — Pipeline Funnel

Horizontal bar chart showing the count at each stage of ingestion + analysis. Stages and SQL semantics:

| Stage | Arabic label | SQL semantics |
|---|---|---|
| Fetched | المُلتقطة من GDELT | not stored in DB — comes from latest pipeline run stats in Redis |
| Relevant | المرتبطة بالقسم | same source |
| Scraped | المُسترَدّ محتواها | `articles WHERE content IS NOT NULL` |
| Entities extracted | المُستخرَجة كياناتها | `articles WHERE entities IS NOT NULL AND jsonb_typeof(entities) = 'object'` |
| Bias classified | المُصنَّفة انحيازياً | `bias_scores` row count |
| In events | المُجمَّعة في أحداث | distinct `article_id` count in `article_events` |

The "Fetched" and "Relevant" stages may be unavailable from DB alone (they are computed during ingestion). Read them from the latest `results:{section}` Redis cache, summed across sections. If unavailable, omit those two bars and note in a small caption: `بيانات الجلب غير متاحة (تحتاج تشغيل pipeline)`.

### 7.5 Section B — Bias Distribution

Bar chart with 5 bars, one per label. Each bar's height is the count of articles with that label. Colors follow the §2.6 palette.

```sql
SELECT label, COUNT(*) FROM bias_scores GROUP BY label;
```

### 7.6 Section C — Articles per Section

Bar chart with 3 bars (Libya, ME, World).

```sql
SELECT section, COUNT(*) FROM articles GROUP BY section;
```

### 7.7 Section D — Articles per Source (top 10)

Horizontal bar chart with the 10 most prolific sources.

```sql
SELECT source, COUNT(*) AS n
FROM articles
GROUP BY source
ORDER BY n DESC
LIMIT 10;
```

### 7.8 Section E — Events per Section

Bar chart with 3 bars.

```sql
SELECT section, COUNT(*) FROM events GROUP BY section;
```

### 7.9 Section F — Blindspots per Section

Bar chart with 3 bars.

```sql
SELECT e.section, COUNT(*)
FROM blindspot_reports br
JOIN events e ON e.id = br.event_id
GROUP BY e.section;
```

### 7.10 Section G — Average Confidence per Bias Label

Bar chart with 5 bars showing average `bias_scores.confidence` per label. Y-axis scale: 0.0 to 1.0.

```sql
SELECT label, ROUND(AVG(confidence)::numeric, 2) AS avg_conf
FROM bias_scores
GROUP BY label;
```

### 7.11 Section H — Articles per Event Distribution

Histogram showing how many events have 1 article, 2, 3, 4, or 5+ articles.

```sql
SELECT bucket, COUNT(*) FROM (
  SELECT CASE
    WHEN c.cnt = 1 THEN '1'
    WHEN c.cnt = 2 THEN '2'
    WHEN c.cnt = 3 THEN '3'
    WHEN c.cnt = 4 THEN '4'
    ELSE '5+'
  END AS bucket
  FROM (
    SELECT event_id, COUNT(*) AS cnt
    FROM article_events
    GROUP BY event_id
  ) c
) t GROUP BY bucket ORDER BY bucket;
```

---

## 8. FastAPI Endpoints

All endpoints return JSON. All endpoints accept an optional `?refresh=true` query parameter that bypasses the FastAPI-level Redis cache.

### 8.1 `GET /api/health`

Health check. No cache. Response shape:

```json
{
  "status": "ok",
  "db": "connected",
  "redis": "connected",
  "gemini_pool": {"total_keys": 8, "quarantined": 0, "available": 8},
  "last_pipeline_run": "2026-05-12T14:30:00Z",
  "version": "0.5.0"
}
```

`last_pipeline_run` is the most recent timestamp across all `results:{section}` Redis entries. If no run has occurred, return `null`.

### 8.2 `GET /api/home`

Aggregates everything needed by Page 1 in one call. Response shape:

```json
{
  "sections": ["libya", "middle_east", "world"],
  "latest_news": [
    {
      "id": 1234,
      "title": "...",
      "url": "https://...",
      "source": "aljazeera.net",
      "section": "libya",
      "published_at": "2026-05-12T13:00:00Z",
      "bias": {"label": "pan_arab", "score": 0.8, "framing": "..."}
    }
  ],
  "latest_events": [
    {
      "id": 256,
      "headline": "...",
      "section": "world",
      "article_count": 5,
      "created_at": "2026-05-12T10:00:00Z",
      "summary": "...",
      "bias_assessment": "...",
      "articles": [...],
      "blindspot": {"missing_perspectives": ["pro_government"]},
      "recommendations": [...]
    }
  ]
}
```

`latest_news` is selected by the rule defined in §4.4 (one per section + 2 newest globally from the rest = 5 total).
`latest_events` is the 3 most recent events by `created_at` descending, with full expanded payload.

### 8.3 `GET /api/events`

Lists events with filters. Query parameters:

| Param | Type | Default | Meaning |
|---|---|---|---|
| `section` | string | `all` | One of `all | libya | middle_east | world` |
| `date_from` | ISO date | null | Inclusive lower bound on `created_at` |
| `date_to` | ISO date | null | Inclusive upper bound on `created_at` |
| `page` | int | 1 | 1-indexed page number |
| `per_page` | int | 10 | Page size (max 50) |

Response shape:

```json
{
  "total": 47,
  "page": 1,
  "per_page": 10,
  "events": [
    { /* same event payload as in /api/home latest_events */ }
  ]
}
```

### 8.4 `GET /api/events/{event_id}`

Returns a single event with full payload (same shape as one item from `/api/events` events array). 404 if not found.

### 8.5 `GET /api/sections/{section}`

Lists articles for one section. Path param: `section` ∈ `{libya, middle_east, world}`. Query parameters:

| Param | Type | Default | Meaning |
|---|---|---|---|
| `bias_labels` | comma-separated string | `all` | e.g., `pan_arab,opposition` |
| `page` | int | 1 | 1-indexed |
| `per_page` | int | 20 | Page size (max 100) |

Response shape:

```json
{
  "section": "libya",
  "total": 147,
  "page": 1,
  "per_page": 20,
  "articles_by_date": [
    {
      "date": "2026-05-12",
      "label": "اليوم",
      "articles": [
        {
          "id": 1234,
          "title": "...",
          "url": "https://...",
          "source": "aljazeera.net",
          "published_at": "2026-05-12T13:00:00Z",
          "bias": {"label": "pan_arab", "score": 0.8, "framing": "..."}
        }
      ]
    },
    {
      "date": "2026-05-11",
      "label": "الأمس",
      "articles": [...]
    },
    {
      "date": "2026-05-09",
      "label": "2026-05-09",
      "articles": [...]
    }
  ]
}
```

The `label` field is the rendered date header for the group. The backend computes whether each date is "today", "yesterday", or older, and returns the appropriate Arabic label.

### 8.6 `GET /api/statistics`

Returns everything Page 4 displays in one call. Response shape:

```json
{
  "kpis": {
    "articles": 487,
    "events": 23,
    "blindspots": 7,
    "sources": 31
  },
  "pipeline_funnel": [
    {"stage": "fetched", "label_ar": "المُلتقطة من GDELT", "count": 525},
    {"stage": "relevant", "label_ar": "المرتبطة بالقسم", "count": 487},
    {"stage": "scraped", "label_ar": "المُسترَدّ محتواها", "count": 462},
    {"stage": "entities_extracted", "label_ar": "المُستخرَجة كياناتها", "count": 412},
    {"stage": "bias_classified", "label_ar": "المُصنَّفة انحيازياً", "count": 385},
    {"stage": "in_events", "label_ar": "المُجمَّعة في أحداث", "count": 52}
  ],
  "bias_distribution": [
    {"label": "pan_arab", "count": 145},
    {"label": "pro_government", "count": 89},
    {"label": "opposition", "count": 67},
    {"label": "western_aligned", "count": 54},
    {"label": "neutral", "count": 30}
  ],
  "articles_per_section": [
    {"section": "libya", "count": 147},
    {"section": "middle_east", "count": 162},
    {"section": "world", "count": 178}
  ],
  "articles_per_source": [
    {"source": "aljazeera.net", "count": 95},
    {"source": "bbc.com", "count": 72}
  ],
  "events_per_section": [
    {"section": "libya", "count": 8},
    {"section": "middle_east", "count": 7},
    {"section": "world", "count": 8}
  ],
  "blindspots_per_section": [
    {"section": "libya", "count": 3},
    {"section": "middle_east", "count": 2},
    {"section": "world", "count": 2}
  ],
  "avg_confidence_per_label": [
    {"label": "pan_arab", "avg_conf": 0.82},
    {"label": "pro_government", "avg_conf": 0.78}
  ],
  "articles_per_event_distribution": [
    {"bucket": "1", "count": 32},
    {"bucket": "2", "count": 12},
    {"bucket": "3", "count": 5},
    {"bucket": "4", "count": 3},
    {"bucket": "5+", "count": 2}
  ]
}
```

Numbers are computed live from PostgreSQL. The `fetched` and `relevant` counts read from the Redis `results:{section}` cache and are summed across sections; if unavailable, those stages are omitted from `pipeline_funnel`.

---

## 9. Tooltips and Inline Documentation

Add tooltips on hover for the following technical terms. Streamlit's `help` parameter on widgets is the standard surface; for static text, use a small `ⓘ` icon next to the term.

| Term | Tooltip (Arabic) |
|---|---|
| Bias Label | تصنيف التحيّز الأيديولوجي للمقالة، من خمس فئات: pro_government, opposition, neutral, pan_arab, western_aligned. |
| Bias Assessment | تحليل نقدي مقارن يوضّح كيف تُؤطّر كل مصدر الحدث، باللغة العربية. |
| Blindspot | منظور سياسي مفقود في تغطية الحدث — مثلاً، حدث غطّته كل المصادر القومية لكن لم تغطّه المصادر الغربية. |
| Event | حدث إخباري واحد تُغطّيه عدّة مقالات من مصادر مختلفة، تم تجميعها تلقائياً بناءً على الزمن والمحتوى والكيانات. |
| Framing | جملة عربية تشرح كيف تُؤطّر هذه المقالة الحدث مقارنةً بالمصادر الأخرى. |
| Neutral Summary | فقرة عربية محايدة تلخّص الحدث بناءً على الحقائق المشتركة بين المصادر، دون لغة منحازة. |
| Pipeline Funnel | تدرّج المقالات عبر مراحل المعالجة، من جلبها من GDELT حتى تجميعها في أحداث. |

Place tooltips next to:
- The first mention of each term on each page.
- Section headers in the Statistics page (Sections A–H).

---

## 10. Error and Empty States

### 10.1 Empty data states

| State | Message (Arabic) |
|---|---|
| No articles in DB | لم تُجمع أيّ مقالات بعد. شغّل الـ pipeline للحصول على البيانات. |
| No events in DB | لم تُكوَّن أيّ أحداث بعد. |
| Section has zero articles | لا توجد مقالات في هذا القسم حالياً. |
| Event has no summary | لم يُنشأ ملخص لهذا الحدث بعد. |
| Event has no bias_assessment | لم يُنشأ تحليل لهذا الحدث بعد. |
| Event has no blindspot | لا توجد نقاط عمياء معروفة لهذا الحدث. |
| No recommendations | لا توجد توصيات لهذا الحدث. |
| Filter returned zero results | لا توجد نتائج تطابق التصفية الحالية. |

### 10.2 API failure states

If a FastAPI call fails or times out, the dashboard shows:

```
تعذّر الاتصال بالخادم. يرجى المحاولة مرة أخرى.
[ إعادة المحاولة ]
```

If `/api/health` reports `db: disconnected` or `redis: disconnected`, show a banner at the top of every page:

```
⚠️ النظام يعمل بوضع محدود: {component} غير متصل.
```

### 10.3 Pipeline-never-ran state

If `results:{section}` is empty for all sections, the Statistics page shows the message at the top:

```
⚠️ لم يُشغَّل الـ pipeline بعد. بعض الإحصائيات قد تكون ناقصة.
```

This is the only place where the dashboard hints at the pipeline's existence to the user. Other pages just show their natural empty states.

---

## 11. Out of Scope (Phase 5)

The following are explicitly excluded from Phase 5 and must not be implemented in this phase, regardless of how easy they appear:

- Real-time / auto-refresh (push updates, websocket, polling).
- Search functionality.
- Export to CSV / PDF.
- User authentication, roles, profiles.
- Bookmark / save article.
- Comment / annotation.
- A "trigger pipeline" button.
- Multilingual UI toggle (EN/AR switch).
- Mobile-specific layout (responsive design is welcome, but no separate mobile flow).
- Timeline chart of articles over time (deferred to Phase 6 with scheduler data).
- Heatmap of bias intensity.
- Source-vs-source comparison view.

If any of these become a requirement later, they enter through a separate phase with its own spec.

---

## 12. Acceptance Criteria

Phase 5 is considered complete when all of the following are demonstrably true:

1. FastAPI runs on port 8001 and exposes the six endpoints in §8.
2. Streamlit runs on port 8501 and renders the four pages defined in §4–§7 (with the Section Detail page parameterized for the three sections).
3. Sidebar navigation (§3.1) is present on every page; the active item is visually distinguished.
4. RTL is correctly applied to every page (no LTR leakage into Arabic content).
5. The Home page has **no KPI cards** — those live only on Statistics.
6. The Section Cards on Home are clickable and link to the right section pages.
7. The Latest News selection on Home follows the §4.4 rule (one per section + 2 globally newest = 5).
8. Article titles on Section Detail pages are clickable links to the original URL (not separate source-name fields).
9. The bias filter on Section Detail is a multi-select dropdown that filters in real time (§6.2).
10. Date grouping on Section Detail uses `اليوم` / `الأمس` / ISO date as defined in §6.3.
11. The Statistics page shows all 8 chart sections (A–H) as defined in §7.4–§7.11, and the 4 KPI cards.
12. Bias labels are rendered as score bars (not badges, not icons) per §2.5.
13. Bias labels are in English; surrounding UI is in Arabic; numbers are Western digits.
14. Confidence is not displayed in any article-level UI; it appears only in the Statistics page Section G.
15. Tooltips per §9 are present on the listed technical terms.
16. Refresh buttons bypass the FastAPI cache via `?refresh=true`.
17. Empty states and error states per §10 are implemented.
18. None of the §11 out-of-scope items are present.
