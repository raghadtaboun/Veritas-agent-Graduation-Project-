"""
api/main.py — Veritas Agent FastAPI backend (Phase 5 Steps 5.1a + 5.1b).

Implements all six endpoints required by the Streamlit dashboard
(phase_5_ui_spec.md §8):

  * GET /api/health                no cache
  * GET /api/home                  5-min cache, key=api:home
  * GET /api/statistics            5-min cache, key=api:statistics
  * GET /api/events                5-min cache, key=api:events:{filters_hash}
  * GET /api/events/{event_id}     5-min cache, key=api:event:{id}
  * GET /api/sections/{section}    5-min cache, key=api:section:{section}:{filters_hash}

Architecture rules (plan.md Step 5.1, phase_5_ui_spec.md §1):
  * Redis-first read strategy on every cached endpoint.
  * No LLM calls from this layer (only the Gemini pool *status* is read on
    /api/health, and the read is wrapped in try/except so a missing key or
    a future llm_client refactor cannot crash the API).
  * No MCP tool calls — the API uses psycopg2 and redis-py directly.
  * No pipeline triggering — the pipeline graph module is never imported.

Timezone convention (Step 5.1b): all "today" / "yesterday" labels and the
/api/events date_from/date_to comparisons are anchored to Africa/Tripoli,
the project's primary user locale. A timestamp at 22:30 UTC on May 12 is
00:30 May 13 in Tripoli — the user expects it to be labelled "اليوم" on
the 13th, which UTC grouping would misclassify.
"""

# ── MUST BE FIRST: bootstrap environment before any other import ────────────
# Rule 2.6: every process entry point loads .env via bootstrap_env() before
# importing any LLM SDK or project module. This pops conflicting shell
# GOOGLE_API_KEY variants and validates required env vars.
from config.env_bootstrap import bootstrap_env  # noqa: E402
bootstrap_env()

# ── Stdlib ──────────────────────────────────────────────────────────────────
import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

# ── Third-party ─────────────────────────────────────────────────────────────
import psycopg2
import redis as redis_lib
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ── Project (after bootstrap) ───────────────────────────────────────────────
from api.cache import cache_get_json, cache_set_json, get_redis
from api.db import SOURCE_SQL_EXPR, get_db_conn

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_VERSION: str = "0.5.0"

# Match the pipeline cache TTL exactly (see the pipeline graph's
# _PIPELINE_CACHE_TTL constant). The last_pipeline_run field on /api/health
# derives "time of last write" from the remaining TTL of results:{section}
# keys: elapsed = _PIPELINE_TTL - current_ttl. This is Option A from the
# pre-work — a Phase 6 TODO is to enrich the pipeline cache write with an
# explicit _ran_at timestamp.
_PIPELINE_TTL_SECS: int = 21_600  # 6 hours

# The three canonical sections defined in config/sections.py.
_SECTIONS: list[str] = ["libya", "middle_east", "world"]

# Cache keys and TTL for the six cached endpoints. Spec §1.3.
_CACHE_TTL_SECS: int = 300
_CACHE_KEY_HOME: str = "api:home"
_CACHE_KEY_STATISTICS: str = "api:statistics"
_CACHE_KEY_EVENTS_FMT: str = "api:events:{filters_hash}"
_CACHE_KEY_EVENT_FMT: str = "api:event:{event_id}"
_CACHE_KEY_SECTION_FMT: str = "api:section:{section}:{filters_hash}"

# Pagination caps for the two listing endpoints. Spec §8.3 / §8.5.
_EVENTS_PER_PAGE_DEFAULT: int = 10
_EVENTS_PER_PAGE_MAX: int = 50
_SECTION_PER_PAGE_DEFAULT: int = 20
_SECTION_PER_PAGE_MAX: int = 100

# The five canonical bias labels (per config/bias_taxonomy + bias_agent).
# Used to silently drop unknown values from ?bias_labels=. Spec §2.5/§6.2.
_VALID_BIAS_LABELS: frozenset[str] = frozenset({
    "pro_government", "opposition", "neutral", "pan_arab", "western_aligned",
})

# The accepted section values for the optional ?section filter on
# /api/events. Note: "all" is the no-filter sentinel and is NOT a valid
# value for the /api/sections/{section} path param.
_EVENTS_SECTION_VALUES: frozenset[str] = frozenset({"all", *_SECTIONS})

# Reference timezone for "today" / "yesterday" date labels and for the
# date_from / date_to range comparison on /api/events. The primary user
# is in Libya — see the Step 5.1b progress log entry for the deliberate
# choice over UTC.
_TZ_TRIPOLI: ZoneInfo = ZoneInfo("Africa/Tripoli")

# Arabic labels for the two special date groups on /api/sections/{section}.
# Spec §6.3.
_LABEL_TODAY_AR: str = "اليوم"
_LABEL_YESTERDAY_AR: str = "الأمس"

# Arabic labels for the six pipeline-funnel stages. Spec §7.4.
_FUNNEL_LABELS_AR: dict[str, str] = {
    "fetched":             "المُلتقطة من GDELT",
    "relevant":            "المرتبطة بالقسم",
    "scraped":             "المُسترَدّ محتواها",
    "entities_extracted":  "المُستخرَجة كياناتها",
    "bias_classified":     "المُصنَّفة انحيازياً",
    "in_events":           "المُجمَّعة في أحداث",
}

# Arabic note shown when fetched/relevant counts are unavailable. Spec §7.4.
_FUNNEL_NOTE_AR: str = "بيانات الجلب غير متاحة (تحتاج تشغيل pipeline)"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic response models
# ─────────────────────────────────────────────────────────────────────────────

# ── /api/health ─────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    db: str
    redis: str
    gemini_pool: dict[str, Any]
    last_pipeline_run: Optional[str] = None
    version: str


# ── /api/home ───────────────────────────────────────────────────────────────

class BiasInfo(BaseModel):
    label: str
    score: float
    framing: Optional[str] = None


class NewsItem(BaseModel):
    id: int
    title: str
    url: str
    source: str
    section: str
    published_at: str
    bias: Optional[BiasInfo] = None


class EventArticle(BaseModel):
    id: int
    title: str
    url: str
    source: str
    bias: Optional[BiasInfo] = None


class Recommendation(BaseModel):
    id: int
    title: str
    url: Optional[str] = None
    bias_label: Optional[str] = None
    similarity: float


class BlindspotInfo(BaseModel):
    missing_perspectives: list[str] = Field(default_factory=list)


class EventItem(BaseModel):
    id: int
    headline: Optional[str] = None
    section: str
    article_count: int
    created_at: str
    summary: Optional[str] = None
    bias_assessment: Optional[str] = None
    articles: list[EventArticle] = Field(default_factory=list)
    blindspot: Optional[BlindspotInfo] = None
    recommendations: list[Recommendation] = Field(default_factory=list)


class HomeResponse(BaseModel):
    sections: list[str]
    latest_news: list[NewsItem]
    latest_events: list[EventItem]


# ── /api/events ─────────────────────────────────────────────────────────────

class EventsListResponse(BaseModel):
    total: int
    page: int
    per_page: int
    events: list[EventItem]


# ── /api/sections/{section} ─────────────────────────────────────────────────

class SectionArticle(BaseModel):
    id: int
    title: str
    url: str
    source: str
    published_at: Optional[str] = None
    bias: Optional[BiasInfo] = None


class DateGroup(BaseModel):
    date: str
    label: str
    articles: list[SectionArticle] = Field(default_factory=list)


class SectionResponse(BaseModel):
    section: str
    total: int
    page: int
    per_page: int
    articles_by_date: list[DateGroup] = Field(default_factory=list)


# ── /api/statistics ─────────────────────────────────────────────────────────

class KPIs(BaseModel):
    articles: int
    events: int
    blindspots: int
    sources: int


class FunnelStage(BaseModel):
    stage: str
    label_ar: str
    count: int


class LabelCount(BaseModel):
    label: str
    count: int


class SectionCount(BaseModel):
    section: str
    count: int


class SourceCount(BaseModel):
    source: str
    count: int


class LabelConfidence(BaseModel):
    label: str
    avg_conf: float


class BucketCount(BaseModel):
    bucket: str
    count: int


class StatisticsResponse(BaseModel):
    kpis: KPIs
    pipeline_funnel: list[FunnelStage]
    pipeline_funnel_note: Optional[str] = None
    bias_distribution: list[LabelCount]
    articles_per_section: list[SectionCount]
    articles_per_source: list[SourceCount]
    events_per_section: list[SectionCount]
    blindspots_per_section: list[SectionCount]
    avg_confidence_per_label: list[LabelConfidence]
    articles_per_event_distribution: list[BucketCount]


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app + CORS
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Veritas Agent API",
    description="FastAPI backend for the Streamlit dashboard (Phase 5).",
    version=_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    # Explicit origins per Rule 2.4 — no wildcard. The Streamlit dashboard
    # runs on 8501; 8001 is the API itself (useful for browser-based testing
    # against the API's own docs page).
    allow_origins=["http://localhost:8501", "http://localhost:8001"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_refresh(request: Request) -> bool:
    """Return True only for the literal case-sensitive string ``true``."""
    return request.query_params.get("refresh") == "true"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with timezone. Returns None for None input."""
    if dt is None:
        return None
    return dt.isoformat()


def _bias_from_row(row: dict) -> Optional[dict]:
    """Build a {label, score, framing} dict from a joined article+bias row.

    Returns ``None`` when the article has no bias_scores row (LEFT JOIN
    columns are NULL). Per the Q5 decision, empty-string framing is kept as
    empty string, not coerced to None.
    """
    label = row.get("bias_label")
    if label is None:
        return None
    return {
        "label":   label,
        "score":   float(row.get("bias_score") or 0.0),
        "framing": row.get("bias_framing") if row.get("bias_framing") is not None else "",
    }


def _get_last_pipeline_run() -> Optional[str]:
    """Approximate the last pipeline run via Redis TTL on results:{section}.

    Per Q1 of the pre-work: elapsed = _PIPELINE_TTL - current_ttl. Take the
    max across all three sections. Returns None if every key is missing.

    This is a Phase 6 TODO: enrich the pipeline serialiser with an explicit
    ``_ran_at`` field. The TTL proxy is bounded by the 6-hour cache window —
    accurate enough for the freshness banner in the UI.
    """
    try:
        r = get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.warning("last_pipeline_run: Redis unavailable: %s", exc)
        return None

    now = datetime.now(timezone.utc)
    latest: Optional[datetime] = None

    for section in _SECTIONS:
        try:
            ttl_raw: Any = r.ttl(f"results:{section}")
        except redis_lib.RedisError as exc:
            logger.warning("last_pipeline_run: TTL probe failed: %s", exc)
            continue
        # ttl == -2 → key does not exist. ttl == -1 → key has no TTL set.
        # ttl == None → driver quirk. All three mean "skip this section".
        if ttl_raw is None:
            continue
        try:
            ttl_int = int(ttl_raw)
        except (TypeError, ValueError):
            continue
        if ttl_int < 0:
            continue
        elapsed = max(0, _PIPELINE_TTL_SECS - ttl_int)
        run_time = now - timedelta(seconds=elapsed)
        if latest is None or run_time > latest:
            latest = run_time

    return _iso(latest)


def _get_gemini_pool_status() -> dict[str, Any]:
    """Return the Gemini pool status, or a safe stub on any failure.

    Per Q2 of the pre-work: keep bootstrap_env() (Rule 2.6) but never let a
    pool problem crash the health endpoint. Failures here include:
      * agents.llm_client import path broken by a future refactor
      * GEMINI_API_KEY missing (pool raises RuntimeError on first call)
      * malformed key crashing the underlying SDK client construction
    """
    try:
        from agents.llm_client import _get_gemini_pool  # noqa: PLC0415
        return _get_gemini_pool().status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gemini pool status probe failed: %s", exc)
        return {"available": None, "note": "pool not introspected from API"}


def _is_system_pending(cur, redis_client: redis_lib.Redis) -> bool:
    """Pending = DB has 0 articles AND 0 events AND no results:{section} keys.

    Per the additional decision (b): partial state (e.g., articles>0 but
    events=0) is NOT pending — it just produces an empty events list.
    Pending is strictly "the pipeline has never produced anything."
    """
    cur.execute("SELECT COUNT(*)::int AS n_articles FROM articles")
    n_articles = cur.fetchone()["n_articles"]
    cur.execute("SELECT COUNT(*)::int AS n_events FROM events")
    n_events = cur.fetchone()["n_events"]

    if n_articles > 0 or n_events > 0:
        return False

    try:
        for section in _SECTIONS:
            if redis_client.exists(f"results:{section}"):
                return False
    except redis_lib.RedisError as exc:
        logger.warning("pending probe: Redis exists() failed: %s", exc)

    return True


def _pending_payload() -> dict[str, Any]:
    return {"status": "pending", "message": "Pipeline has not run yet."}


def _enrich_recommendations_with_urls(
    cur,
    raw_recs: list[dict],
) -> list[dict]:
    """Attach a `url` field to each cached recommendation entry.

    The Redis recommend:{article_id} cache stores {id, title, bias_label,
    similarity} — no URL. Per the Q4 decision, we run one tiny lookup per
    event and surface `url: null` for any recommended article that has been
    deleted/missing from the DB (rather than dropping the recommendation).

    Returns a fresh list — never mutates the input.
    """
    enriched: list[dict] = []
    ids: list[int] = []
    for rec in raw_recs:
        rid = rec.get("id")
        if isinstance(rid, int):
            ids.append(rid)

    url_by_id: dict[int, str] = {}
    if ids:
        cur.execute(
            "SELECT id, url FROM articles WHERE id = ANY(%s)",
            (ids,),
        )
        url_by_id = {r["id"]: r["url"] for r in cur.fetchall()}

    for rec in raw_recs:
        rid = rec.get("id")
        enriched.append({
            "id":         rid,
            "title":      rec.get("title", ""),
            "url":        url_by_id.get(rid) if isinstance(rid, int) else None,
            "bias_label": rec.get("bias_label"),
            "similarity": float(rec.get("similarity", 0.0)),
        })
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# /api/health
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> JSONResponse:
    """Health check — no cache.

    Returns 200 with status="ok" when both DB and Redis are reachable.
    Returns 503 with status="degraded" when DB is unreachable.
    A Redis outage alone is not 503 — the API can still serve cached-free
    queries against the DB, so we surface it as degraded-but-200.
    """
    # ── DB probe ────────────────────────────────────────────────────────────
    db_status = "disconnected"
    try:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            db_status = "connected"
        finally:
            conn.close()
    except (psycopg2.Error, KeyError) as exc:
        logger.warning("/api/health: DB probe failed: %s", exc)

    # ── Redis probe ─────────────────────────────────────────────────────────
    redis_status = "disconnected"
    try:
        if get_redis().ping():
            redis_status = "connected"
    except (redis_lib.RedisError, KeyError) as exc:
        logger.warning("/api/health: Redis probe failed: %s", exc)

    # ── Gemini pool status (best-effort) ────────────────────────────────────
    pool_status = _get_gemini_pool_status()

    # ── Last pipeline run (Redis TTL proxy) ─────────────────────────────────
    last_run = _get_last_pipeline_run() if redis_status == "connected" else None

    overall = "ok" if db_status == "connected" else "degraded"
    payload = HealthResponse(
        status=overall,
        db=db_status,
        redis=redis_status,
        gemini_pool=pool_status,
        last_pipeline_run=last_run,
        version=_VERSION,
    ).model_dump()

    http_status = 200 if db_status == "connected" else 503
    return JSONResponse(status_code=http_status, content=payload)


# ─────────────────────────────────────────────────────────────────────────────
# /api/home
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_latest_news(cur) -> list[dict]:
    """Spec §4.4: 1 article per section + 2 newest from rest. Joined with bias."""
    cur.execute(
        f"""
        WITH per_section AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY section
                       ORDER BY published_at DESC, id DESC
                   ) AS rn
            FROM articles
            WHERE section IN ('libya', 'middle_east', 'world')
        ),
        section_winners AS (
            SELECT id FROM per_section WHERE rn = 1
        ),
        extras AS (
            SELECT id
            FROM articles
            WHERE id NOT IN (SELECT id FROM section_winners)
            ORDER BY published_at DESC, id DESC
            LIMIT 2
        ),
        combined AS (
            SELECT id FROM section_winners
            UNION ALL
            SELECT id FROM extras
        )
        SELECT a.id, a.title, a.url,
               {SOURCE_SQL_EXPR} AS source,
               a.section, a.published_at,
               bs.label      AS bias_label,
               bs.score      AS bias_score,
               bs.framing    AS bias_framing
        FROM combined c
        JOIN articles a ON a.id = c.id
        LEFT JOIN bias_scores bs ON bs.article_id = a.id
        ORDER BY a.published_at DESC, a.id DESC
        """
    )
    return cur.fetchall()


def _fetch_recent_events(cur, limit: int = 3) -> list[dict]:
    cur.execute(
        """
        SELECT id, headline, section, summary, bias_assessment, created_at
        FROM events
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cur.fetchall()


def _fetch_event_articles(cur, event_id: int) -> list[dict]:
    cur.execute(
        f"""
        SELECT a.id, a.title, a.url,
               {SOURCE_SQL_EXPR} AS source,
               bs.label   AS bias_label,
               bs.score   AS bias_score,
               bs.framing AS bias_framing
        FROM article_events ae
        JOIN articles a ON a.id = ae.article_id
        LEFT JOIN bias_scores bs ON bs.article_id = a.id
        WHERE ae.event_id = %s
        ORDER BY a.published_at DESC NULLS LAST, a.id
        """,
        (event_id,),
    )
    return cur.fetchall()


def _fetch_event_blindspot(cur, event_id: int) -> Optional[dict]:
    cur.execute(
        """
        SELECT missing_perspectives
        FROM blindspot_reports
        WHERE event_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (event_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"missing_perspectives": list(row["missing_perspectives"] or [])}


def _read_recommend_cache(redis_client: redis_lib.Redis, article_id: int) -> list[dict]:
    """Read the cached recommendation list for a given article id.

    Returns ``[]`` on miss or parse error. The cache value was written by
    agents/recommendation_agent.py as a JSON list of
    ``{id, title, bias_label, similarity}`` dicts.
    """
    try:
        raw: Any = redis_client.get(f"recommend:{article_id}")
    except redis_lib.RedisError:
        return []
    if not raw or not isinstance(raw, (str, bytes, bytearray)):
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return parsed


def _build_event_payload(
    cur,
    redis_client: Optional[redis_lib.Redis],
    ev: dict,
) -> dict[str, Any]:
    """Build the full expanded event payload (spec §5.2).

    Shared by /api/home (latest_events), /api/events, and
    /api/events/{event_id}. The returned dict matches the ``EventItem``
    schema exactly.

    ``ev`` must carry at least ``{id, headline, section, summary,
    bias_assessment, created_at}``.

    When ``redis_client`` is None (Redis unreachable), the recommendations
    list is left empty rather than crashing — degrade-gracefully per Rule
    2.4. The blindspot read is DB-only and unaffected.
    """
    article_rows = _fetch_event_articles(cur, ev["id"])
    articles = [
        {
            "id":     a["id"],
            "title":  a["title"],
            "url":    a["url"],
            "source": a["source"] or "",
            "bias":   _bias_from_row(a),
        }
        for a in article_rows
    ]
    blindspot = _fetch_event_blindspot(cur, ev["id"])

    # Recommendations: read cache for the first article in the event,
    # enrich with URLs. Spec §4.5/§5.2: "recommend:{first_article_id}".
    recommendations: list[dict] = []
    if article_rows and redis_client is not None:
        first_id = article_rows[0]["id"]
        raw_recs = _read_recommend_cache(redis_client, first_id)
        if raw_recs:
            recommendations = _enrich_recommendations_with_urls(cur, raw_recs)

    return {
        "id":              ev["id"],
        "headline":        ev["headline"],
        "section":         ev["section"],
        "article_count":   len(article_rows),
        "created_at":      _iso(ev["created_at"]),
        "summary":         ev["summary"],
        "bias_assessment": ev["bias_assessment"],
        "articles":        articles,
        "blindspot":       blindspot,
        "recommendations": recommendations,
    }


def _build_home_payload(cur, redis_client: redis_lib.Redis) -> dict[str, Any]:
    """Assemble the /api/home payload. Caller must have already ruled out pending."""
    # ── Latest news ─────────────────────────────────────────────────────────
    latest_news: list[dict] = []
    for row in _fetch_latest_news(cur):
        latest_news.append({
            "id":           row["id"],
            "title":        row["title"],
            "url":          row["url"],
            "source":       row["source"] or "",
            "section":      row["section"],
            "published_at": _iso(row["published_at"]),
            "bias":         _bias_from_row(row),
        })

    # ── Latest events ───────────────────────────────────────────────────────
    # Per Step 5.1b extraction: each event is built by the shared
    # _build_event_payload helper, reused by /api/events and
    # /api/events/{event_id}. The only modification permitted to 5.1a code.
    latest_events: list[dict] = [
        _build_event_payload(cur, redis_client, ev)
        for ev in _fetch_recent_events(cur, limit=3)
    ]

    return HomeResponse(
        sections=_SECTIONS,
        latest_news=[NewsItem.model_validate(n) for n in latest_news],
        latest_events=[EventItem.model_validate(e) for e in latest_events],
    ).model_dump()


@app.get("/api/home")
async def home(request: Request) -> JSONResponse:
    """Home page payload. Cache 5 min, key=api:home. Refresh via ?refresh=true."""
    refresh = _is_refresh(request)

    cached = cache_get_json(_CACHE_KEY_HOME, refresh=refresh)
    if cached is not None:
        return JSONResponse(status_code=200, content=cached)

    try:
        conn = get_db_conn()
    except (psycopg2.Error, KeyError) as exc:
        logger.error("/api/home: DB connect failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database unreachable: {exc}"},
        )

    try:
        with conn.cursor() as cur:
            # Pending check before any heavy query.
            try:
                redis_client = get_redis()
            except Exception:  # noqa: BLE001
                redis_client = None  # type: ignore[assignment]

            if redis_client is not None and _is_system_pending(cur, redis_client):
                # Do not cache the pending payload — once the pipeline runs
                # we want the next request to compute fresh.
                return JSONResponse(status_code=200, content=_pending_payload())

            payload = _build_home_payload(cur, redis_client)  # type: ignore[arg-type]
    except psycopg2.Error as exc:
        logger.error("/api/home: SQL error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database error: {exc}"},
        )
    finally:
        conn.close()

    cache_set_json(_CACHE_KEY_HOME, payload, ttl=_CACHE_TTL_SECS)
    return JSONResponse(status_code=200, content=payload)


# ─────────────────────────────────────────────────────────────────────────────
# /api/statistics
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_kpis(cur) -> dict[str, int]:
    cur.execute(
        """
        SELECT
          (SELECT COUNT(*)::int FROM articles)           AS articles,
          (SELECT COUNT(*)::int FROM events)             AS events,
          (SELECT COUNT(*)::int FROM blindspot_reports)  AS blindspots,
          (SELECT COUNT(DISTINCT """ + SOURCE_SQL_EXPR + """)::int FROM articles) AS sources
        """
    )
    return dict(cur.fetchone())


def _fetch_funnel_from_db(cur) -> dict[str, int]:
    """Stages 3–6 (scraped, entities_extracted, bias_classified, in_events)."""
    cur.execute("""SELECT COUNT(*)::int AS n FROM articles WHERE content IS NOT NULL""")
    scraped = cur.fetchone()["n"]

    # Funnel invariant: entities are a downstream stage of scraping, so only
    # count articles whose content was actually retrieved. The ingestion agent
    # falls back to extracting entities from the title when scraping fails
    # (content stays NULL); excluding those keeps the funnel monotonic.
    cur.execute(
        """SELECT COUNT(*)::int AS n FROM articles
           WHERE content IS NOT NULL
             AND entities IS NOT NULL AND jsonb_typeof(entities) = 'object'
             AND entities <> '{}'::jsonb"""
    )
    entities_extracted = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*)::int AS n FROM bias_scores")
    bias_classified = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(DISTINCT article_id)::int AS n FROM article_events")
    in_events = cur.fetchone()["n"]

    return {
        "scraped":            scraped,
        "entities_extracted": entities_extracted,
        "bias_classified":    bias_classified,
        "in_events":          in_events,
    }


def _fetch_funnel_from_redis(
    redis_client: redis_lib.Redis,
) -> tuple[Optional[int], Optional[int]]:
    """Sum (fetched, relevant) across results:{section}. Returns (None, None) if all missing."""
    fetched_total = 0
    relevant_total = 0
    found = False
    for section in _SECTIONS:
        try:
            raw: Any = redis_client.get(f"results:{section}")
        except redis_lib.RedisError:
            continue
        if not raw or not isinstance(raw, (str, bytes, bytearray)):
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        ingestion = (payload.get("stats") or {}).get("ingestion") or {}
        f_val = ingestion.get("fetched")
        r_val = ingestion.get("relevant")
        if f_val is not None:
            fetched_total += int(f_val)
            found = True
        if r_val is not None:
            relevant_total += int(r_val)
            found = True
    if not found:
        return None, None
    return fetched_total, relevant_total


def _fetch_bias_distribution(cur) -> list[dict]:
    cur.execute(
        """
        SELECT label, COUNT(*)::int AS count
        FROM bias_scores
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_count_per_section(cur, query: str) -> list[dict]:
    """Run a section-grouping query and pad missing sections with 0.

    Spec §10: zero-count sections must appear (count=0), not be omitted.
    """
    cur.execute(query)
    by_section = {r["section"]: int(r["count"]) for r in cur.fetchall()}
    return [
        {"section": s, "count": by_section.get(s, 0)}
        for s in _SECTIONS
    ]


def _fetch_articles_per_source(cur) -> list[dict]:
    cur.execute(
        f"""
        SELECT {SOURCE_SQL_EXPR} AS source, COUNT(*)::int AS count
        FROM articles
        GROUP BY {SOURCE_SQL_EXPR}
        ORDER BY count DESC, source ASC
        LIMIT 10
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_avg_confidence_per_label(cur) -> list[dict]:
    cur.execute(
        """
        SELECT label,
               ROUND(AVG(confidence)::numeric, 2)::float AS avg_conf
        FROM bias_scores
        GROUP BY label
        ORDER BY avg_conf DESC, label ASC
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_articles_per_event_distribution(cur) -> list[dict]:
    cur.execute(
        """
        SELECT bucket, COUNT(*)::int AS count
        FROM (
            SELECT CASE
                WHEN cnt = 1 THEN '1'
                WHEN cnt = 2 THEN '2'
                WHEN cnt = 3 THEN '3'
                WHEN cnt = 4 THEN '4'
                ELSE '5+'
            END AS bucket
            FROM (
                SELECT event_id, COUNT(*) AS cnt
                FROM article_events
                GROUP BY event_id
            ) c
        ) t
        GROUP BY bucket
        ORDER BY
            CASE bucket
                WHEN '1' THEN 1
                WHEN '2' THEN 2
                WHEN '3' THEN 3
                WHEN '4' THEN 4
                ELSE 5
            END
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _build_statistics_payload(
    cur,
    redis_client: redis_lib.Redis,
) -> dict[str, Any]:
    """Assemble the /api/statistics payload. Caller has ruled out pending."""
    kpis = _fetch_kpis(cur)

    funnel_db = _fetch_funnel_from_db(cur)
    fetched, relevant = _fetch_funnel_from_redis(redis_client)

    funnel: list[dict] = []
    funnel_note: Optional[str] = None
    if fetched is not None and relevant is not None:
        funnel.append({"stage": "fetched",  "label_ar": _FUNNEL_LABELS_AR["fetched"],  "count": fetched})
        funnel.append({"stage": "relevant", "label_ar": _FUNNEL_LABELS_AR["relevant"], "count": relevant})
    else:
        funnel_note = _FUNNEL_NOTE_AR

    for stage in ("scraped", "entities_extracted", "bias_classified", "in_events"):
        funnel.append({
            "stage":    stage,
            "label_ar": _FUNNEL_LABELS_AR[stage],
            "count":    funnel_db[stage],
        })

    articles_per_section = _fetch_count_per_section(
        cur,
        "SELECT section, COUNT(*)::int AS count FROM articles GROUP BY section",
    )
    events_per_section = _fetch_count_per_section(
        cur,
        "SELECT section, COUNT(*)::int AS count FROM events GROUP BY section",
    )
    blindspots_per_section = _fetch_count_per_section(
        cur,
        """
        SELECT e.section, COUNT(*)::int AS count
        FROM blindspot_reports br
        JOIN events e ON e.id = br.event_id
        GROUP BY e.section
        """,
    )

    payload = StatisticsResponse(
        kpis=KPIs(**kpis),
        pipeline_funnel=[FunnelStage(**f) for f in funnel],
        pipeline_funnel_note=funnel_note,
        bias_distribution=[LabelCount(**r) for r in _fetch_bias_distribution(cur)],
        articles_per_section=[SectionCount(**r) for r in articles_per_section],
        articles_per_source=[SourceCount(**r) for r in _fetch_articles_per_source(cur)],
        events_per_section=[SectionCount(**r) for r in events_per_section],
        blindspots_per_section=[SectionCount(**r) for r in blindspots_per_section],
        avg_confidence_per_label=[LabelConfidence(**r) for r in _fetch_avg_confidence_per_label(cur)],
        articles_per_event_distribution=[BucketCount(**r) for r in _fetch_articles_per_event_distribution(cur)],
    ).model_dump(exclude_none=False)

    # Pydantic includes pipeline_funnel_note=None when funnel_note is None,
    # but the spec only mentions the field when fetched/relevant are
    # unavailable. Drop it when None for a cleaner payload.
    if payload.get("pipeline_funnel_note") is None:
        payload.pop("pipeline_funnel_note", None)

    return payload


@app.get("/api/statistics")
async def statistics(request: Request) -> JSONResponse:
    """Statistics page payload. Cache 5 min, key=api:statistics."""
    refresh = _is_refresh(request)

    cached = cache_get_json(_CACHE_KEY_STATISTICS, refresh=refresh)
    if cached is not None:
        return JSONResponse(status_code=200, content=cached)

    try:
        conn = get_db_conn()
    except (psycopg2.Error, KeyError) as exc:
        logger.error("/api/statistics: DB connect failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database unreachable: {exc}"},
        )

    try:
        with conn.cursor() as cur:
            try:
                redis_client = get_redis()
            except Exception:  # noqa: BLE001
                redis_client = None  # type: ignore[assignment]

            if redis_client is not None and _is_system_pending(cur, redis_client):
                return JSONResponse(status_code=200, content=_pending_payload())

            payload = _build_statistics_payload(cur, redis_client)  # type: ignore[arg-type]
    except psycopg2.Error as exc:
        logger.error("/api/statistics: SQL error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database error: {exc}"},
        )
    finally:
        conn.close()

    cache_set_json(_CACHE_KEY_STATISTICS, payload, ttl=_CACHE_TTL_SECS)
    return JSONResponse(status_code=200, content=payload)


# ─────────────────────────────────────────────────────────────────────────────
# Step 5.1b helpers — shared by /api/events, /api/events/{id}, /api/sections
# ─────────────────────────────────────────────────────────────────────────────

def _md5_short(s: str) -> str:
    """12-hex-char prefix of MD5. Deterministic, short, no realistic collision.

    Used to keep cache keys compact in Redis (`api:events:{12-hex-chars}`).
    """
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _events_filters_hash(
    section: str,
    date_from: Optional[date],
    date_to: Optional[date],
    page: int,
    per_page: int,
) -> str:
    """Deterministic 12-char hash of the /api/events filter tuple.

    ``None`` dates render as the literal string "None" in the f-string —
    that is intentional and identical between requests with the same
    effective filters.
    """
    return _md5_short(f"{section}:{date_from}:{date_to}:{page}:{per_page}")


def _section_filters_hash(
    bias_labels: Optional[list[str]],
    page: int,
    per_page: int,
) -> str:
    """Deterministic 12-char hash of the /api/sections filter tuple.

    Labels are sorted before hashing so that ``?bias_labels=a,b`` and
    ``?bias_labels=b,a`` map to the same cache key. ``None`` means the
    no-filter sentinel "all".
    """
    labels_str = "all" if bias_labels is None else ",".join(sorted(bias_labels))
    return _md5_short(f"{labels_str}:{page}:{per_page}")


def _parse_bias_labels(raw: str) -> Optional[list[str]]:
    """Parse the ?bias_labels query parameter.

    Returns:
      * ``None`` when the filter must NOT be applied (raw is the literal
        sentinel ``"all"``). The endpoint will then LEFT JOIN and return
        articles without a bias row.
      * A possibly-empty list of known labels otherwise. Empty list means
        "user provided labels but none of them are recognised" — the
        endpoint applies INNER JOIN with ``bs.label = ANY('{}')`` which
        yields zero rows (intentional per Step 5.1b decision 4b).

    Edge cases (per Step 5.1b decision 4b):
      * empty/blank fragments (``,, ,pan_arab``) are stripped
      * unknown labels (``foo,pan_arab``) are silently dropped
      * all-invalid (``foo,bar``) yields ``[]`` → empty result set
    """
    if raw == "all":
        return None
    seen: set[str] = set()
    valid: list[str] = []
    for fragment in raw.split(","):
        label = fragment.strip()
        if not label or label not in _VALID_BIAS_LABELS or label in seen:
            continue
        seen.add(label)
        valid.append(label)
    return valid


def _validate_events_section(section: str) -> Optional[JSONResponse]:
    """Return a 400 response if the /api/events ?section value is invalid.

    Accepts ``all`` + the three canonical sections. Anything else → 400.
    """
    if section not in _EVENTS_SECTION_VALUES:
        return JSONResponse(
            status_code=400,
            content={"detail": "invalid section"},
        )
    return None


def _validate_path_section(section: str) -> Optional[JSONResponse]:
    """Return a 400 response if the /api/sections/{section} path value is invalid.

    The path param accepts only the three canonical sections — NOT ``all``,
    since the path param identifies a single section.
    """
    if section not in _SECTIONS:
        return JSONResponse(
            status_code=400,
            content={"detail": "invalid section"},
        )
    return None


# ── /api/events SQL helpers ─────────────────────────────────────────────────

def _build_events_where(
    section: str,
    date_from: Optional[date],
    date_to: Optional[date],
) -> tuple[str, list[Any]]:
    """Build the WHERE clause + param list for /api/events filters.

    Returns (where_sql, params). When no filters apply, where_sql is the
    literal "TRUE" so the caller can interpolate it unconditionally. The
    date comparisons are anchored to Africa/Tripoli per the Step 5.1b
    timezone convention.
    """
    parts: list[str] = []
    params: list[Any] = []

    if section != "all":
        parts.append("section = %s")
        params.append(section)

    if date_from is not None:
        parts.append(
            "DATE(created_at AT TIME ZONE 'Africa/Tripoli') >= %s"
        )
        params.append(date_from)

    if date_to is not None:
        parts.append(
            "DATE(created_at AT TIME ZONE 'Africa/Tripoli') <= %s"
        )
        params.append(date_to)

    where_sql = " AND ".join(parts) if parts else "TRUE"
    return where_sql, params


def _fetch_events_filtered(
    cur,
    section: str,
    date_from: Optional[date],
    date_to: Optional[date],
    page: int,
    per_page: int,
) -> tuple[int, list[dict]]:
    """Return (total, page_rows) for /api/events.

    ``total`` is the unpaginated COUNT under the same filters. The page
    rows carry the columns needed by ``_build_event_payload`` (id,
    headline, section, summary, bias_assessment, created_at). Page rows
    are ordered most-recent-first by ``created_at`` with ``id`` as a
    stable tiebreaker.
    """
    where_sql, params = _build_events_where(section, date_from, date_to)

    cur.execute(
        f"SELECT COUNT(*)::int AS total FROM events WHERE {where_sql}",
        params,
    )
    total = cur.fetchone()["total"]

    offset = (page - 1) * per_page
    cur.execute(
        f"""
        SELECT id, headline, section, summary, bias_assessment, created_at
        FROM events
        WHERE {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        params + [per_page, offset],
    )
    rows = cur.fetchall()
    return total, rows


def _fetch_event_by_id(cur, event_id: int) -> Optional[dict]:
    """Return the single event row by id, or None if no such row exists."""
    cur.execute(
        """
        SELECT id, headline, section, summary, bias_assessment, created_at
        FROM events
        WHERE id = %s
        """,
        (event_id,),
    )
    return cur.fetchone()


# ── /api/sections SQL helpers ───────────────────────────────────────────────

def _fetch_section_articles(
    cur,
    section: str,
    bias_labels: Optional[list[str]],
    page: int,
    per_page: int,
) -> tuple[int, list[dict]]:
    """Return (total, page_rows) for /api/sections/{section}.

    When ``bias_labels`` is None → LEFT JOIN, articles without a bias row
    are included with ``bias: null``. When ``bias_labels`` is a list →
    INNER JOIN with ``bs.label = ANY(%s)``; an empty list yields zero
    rows.

    Rows are ordered ``published_at DESC NULLS LAST, id DESC`` so the
    in-Python grouping that follows produces newest-first date groups.
    Each row carries the columns needed for the ``SectionArticle`` shape
    plus the three ``bias_*`` columns consumed by ``_bias_from_row``.
    """
    join_kw = "LEFT" if bias_labels is None else "INNER"

    where_parts = ["a.section = %s"]
    params: list[Any] = [section]
    if bias_labels is not None:
        where_parts.append("bs.label = ANY(%s)")
        params.append(bias_labels)
    where_sql = " AND ".join(where_parts)

    cur.execute(
        f"""
        SELECT COUNT(*)::int AS total
        FROM articles a
        {join_kw} JOIN bias_scores bs ON bs.article_id = a.id
        WHERE {where_sql}
        """,
        params,
    )
    total = cur.fetchone()["total"]

    offset = (page - 1) * per_page
    cur.execute(
        f"""
        SELECT a.id, a.title, a.url,
               {SOURCE_SQL_EXPR} AS source,
               a.published_at,
               bs.label   AS bias_label,
               bs.score   AS bias_score,
               bs.framing AS bias_framing
        FROM articles a
        {join_kw} JOIN bias_scores bs ON bs.article_id = a.id
        WHERE {where_sql}
        ORDER BY a.published_at DESC NULLS LAST, a.id DESC
        LIMIT %s OFFSET %s
        """,
        params + [per_page, offset],
    )
    rows = cur.fetchall()
    return total, rows


def _group_articles_by_date(rows: list[dict]) -> list[dict]:
    """Group ``rows`` by the Africa/Tripoli date of ``published_at``.

    Spec §6.3:
      * date == Tripoli today      → label = "اليوم"
      * date == Tripoli yesterday  → label = "الأمس"
      * otherwise                  → label = "YYYY-MM-DD"

    Groups are emitted in the order in which their first article appears
    in ``rows`` — and ``rows`` is already ordered ``published_at DESC``
    upstream, so newest-first is preserved without an extra sort. Within
    each group the relative ordering of ``rows`` is preserved.

    Articles with NULL ``published_at`` are skipped defensively — they
    cannot be meaningfully grouped under a date, and they should not
    occur in production data.
    """
    today_local = datetime.now(_TZ_TRIPOLI).date()
    yesterday_local = today_local - timedelta(days=1)

    groups: dict[date, list[dict]] = {}
    order: list[date] = []

    for row in rows:
        published_at: Optional[datetime] = row.get("published_at")
        if published_at is None:
            logger.debug("section group: skipping row id=%s (published_at NULL)", row.get("id"))
            continue

        local_dt = published_at.astimezone(_TZ_TRIPOLI)
        grp_date = local_dt.date()

        if grp_date not in groups:
            groups[grp_date] = []
            order.append(grp_date)

        groups[grp_date].append({
            "id":           row["id"],
            "title":        row["title"],
            "url":          row["url"],
            "source":       row["source"] or "",
            "published_at": _iso(row["published_at"]),
            "bias":         _bias_from_row(row),
        })

    out: list[dict] = []
    for d in order:
        if d == today_local:
            label = _LABEL_TODAY_AR
        elif d == yesterday_local:
            label = _LABEL_YESTERDAY_AR
        else:
            label = d.isoformat()
        out.append({
            "date":     d.isoformat(),
            "label":    label,
            "articles": groups[d],
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# /api/events
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/events")
async def events(
    request: Request,
    section: str = Query("all"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(
        _EVENTS_PER_PAGE_DEFAULT, ge=1, le=_EVENTS_PER_PAGE_MAX,
    ),
) -> JSONResponse:
    """Paginated event list with section + date filters. Spec §8.3.

    Cache 5 min, key=``api:events:{filters_hash}``. Honors ``?refresh=true``.
    Pending payload (HTTP 200) when the system is empty + no Redis cache.
    """
    invalid = _validate_events_section(section)
    if invalid is not None:
        return invalid

    refresh = _is_refresh(request)
    cache_key = _CACHE_KEY_EVENTS_FMT.format(
        filters_hash=_events_filters_hash(section, date_from, date_to, page, per_page),
    )

    cached = cache_get_json(cache_key, refresh=refresh)
    if cached is not None:
        return JSONResponse(status_code=200, content=cached)

    try:
        conn = get_db_conn()
    except (psycopg2.Error, KeyError) as exc:
        logger.error("/api/events: DB connect failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database unreachable: {exc}"},
        )

    try:
        with conn.cursor() as cur:
            try:
                redis_client = get_redis()
            except Exception:  # noqa: BLE001
                redis_client = None  # type: ignore[assignment]

            if redis_client is not None and _is_system_pending(cur, redis_client):
                return JSONResponse(status_code=200, content=_pending_payload())

            total, event_rows = _fetch_events_filtered(
                cur, section, date_from, date_to, page, per_page,
            )
            event_payloads = [
                _build_event_payload(cur, redis_client, ev) for ev in event_rows
            ]
    except psycopg2.Error as exc:
        logger.error("/api/events: SQL error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database error: {exc}"},
        )
    finally:
        conn.close()

    payload = EventsListResponse(
        total=total,
        page=page,
        per_page=per_page,
        events=[EventItem.model_validate(e) for e in event_payloads],
    ).model_dump()

    cache_set_json(cache_key, payload, ttl=_CACHE_TTL_SECS)
    return JSONResponse(status_code=200, content=payload)


# ─────────────────────────────────────────────────────────────────────────────
# /api/events/{event_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/events/{event_id}")
async def event_by_id(request: Request, event_id: int) -> JSONResponse:
    """Single-event detail. Spec §8.4.

    Cache 5 min, key=``api:event:{id}``. 404 when the system has data but
    the requested event id does not exist; pending payload when the
    system is wholly empty (pending precedes 404 per the 5.1b decision).
    Neither pending nor 404 responses are cached.
    """
    refresh = _is_refresh(request)
    cache_key = _CACHE_KEY_EVENT_FMT.format(event_id=event_id)

    cached = cache_get_json(cache_key, refresh=refresh)
    if cached is not None:
        return JSONResponse(status_code=200, content=cached)

    try:
        conn = get_db_conn()
    except (psycopg2.Error, KeyError) as exc:
        logger.error("/api/events/{id}: DB connect failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database unreachable: {exc}"},
        )

    try:
        with conn.cursor() as cur:
            try:
                redis_client = get_redis()
            except Exception:  # noqa: BLE001
                redis_client = None  # type: ignore[assignment]

            if redis_client is not None and _is_system_pending(cur, redis_client):
                return JSONResponse(status_code=200, content=_pending_payload())

            ev = _fetch_event_by_id(cur, event_id)
            if ev is None:
                return JSONResponse(
                    status_code=404,
                    content={"detail": "event not found"},
                )

            event_payload = _build_event_payload(cur, redis_client, ev)
    except psycopg2.Error as exc:
        logger.error("/api/events/{id}: SQL error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database error: {exc}"},
        )
    finally:
        conn.close()

    payload = EventItem.model_validate(event_payload).model_dump()
    cache_set_json(cache_key, payload, ttl=_CACHE_TTL_SECS)
    return JSONResponse(status_code=200, content=payload)


# ─────────────────────────────────────────────────────────────────────────────
# /api/sections/{section}
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/sections/{section}")
async def section_articles(
    request: Request,
    section: str,
    bias_labels: str = Query("all"),
    page: int = Query(1, ge=1),
    per_page: int = Query(
        _SECTION_PER_PAGE_DEFAULT, ge=1, le=_SECTION_PER_PAGE_MAX,
    ),
) -> JSONResponse:
    """Section article list, grouped by Africa/Tripoli date. Spec §8.5.

    Cache 5 min, key=``api:section:{section}:{filters_hash}``.

    Pagination is applied across the whole filtered article set; date
    grouping is then applied within the page. ``total`` is the global
    filtered count, NOT the count of articles on the current page.

    Pending payload (HTTP 200) when the system is wholly empty. A section
    with zero articles while other sections have data is NOT pending —
    the endpoint returns ``articles_by_date: []`` with ``total: 0``.
    """
    invalid = _validate_path_section(section)
    if invalid is not None:
        return invalid

    labels_filter = _parse_bias_labels(bias_labels)

    refresh = _is_refresh(request)
    cache_key = _CACHE_KEY_SECTION_FMT.format(
        section=section,
        filters_hash=_section_filters_hash(labels_filter, page, per_page),
    )

    cached = cache_get_json(cache_key, refresh=refresh)
    if cached is not None:
        return JSONResponse(status_code=200, content=cached)

    try:
        conn = get_db_conn()
    except (psycopg2.Error, KeyError) as exc:
        logger.error("/api/sections/{section}: DB connect failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database unreachable: {exc}"},
        )

    try:
        with conn.cursor() as cur:
            try:
                redis_client = get_redis()
            except Exception:  # noqa: BLE001
                redis_client = None  # type: ignore[assignment]

            if redis_client is not None and _is_system_pending(cur, redis_client):
                return JSONResponse(status_code=200, content=_pending_payload())

            total, article_rows = _fetch_section_articles(
                cur, section, labels_filter, page, per_page,
            )
            articles_by_date = _group_articles_by_date(article_rows)
    except psycopg2.Error as exc:
        logger.error("/api/sections/{section}: SQL error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": f"database error: {exc}"},
        )
    finally:
        conn.close()

    payload = SectionResponse(
        section=section,
        total=total,
        page=page,
        per_page=per_page,
        articles_by_date=[DateGroup.model_validate(g) for g in articles_by_date],
    ).model_dump()

    cache_set_json(cache_key, payload, ttl=_CACHE_TTL_SECS)
    return JSONResponse(status_code=200, content=payload)
