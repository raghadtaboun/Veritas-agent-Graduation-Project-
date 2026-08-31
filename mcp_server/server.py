"""
mcp_server/server.py — Veritas Agent MCP Server

Single FastMCP server exposing 15 pure tools (canonical MCP — only the tools
actually consumed by the agents are retained).
Runs as an independent process on port 8000 using Streamable HTTP transport.
All agents call tools through this server exclusively — no agent touches
PostgreSQL, Redis, GDELT, or scraping infrastructure directly. LLM dispatch
lives entirely in the agents via `agents/llm_client.py` (Rule 2.3).
"""

import os
import sys

# Ensure the project root (veritas-agent/) is on sys.path so that
# `config`, `agents`, etc. are importable when the server runs as a script.
# This MUST precede the bootstrap_env() import below.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Phase 4.5 Step 4.5.2: centralised environment bootstrap. Pops conflicting
# shell-level GOOGLE_API_KEY variants, loads .env with override=True, and
# validates required keys. Retained post-4.5.4 because agent-side code
# (which shares process boundaries with tests) still depends on it; the
# server itself no longer imports any LLM SDK.
from config.env_bootstrap import bootstrap_env  # noqa: E402
bootstrap_env()

import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

import httpx  # noqa: E402
import psycopg2  # noqa: E402
import redis as redis_lib  # noqa: E402
import trafilatura  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

# ── Environment variables ────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

# ── Redis client ─────────────────────────────────────────────────────────────
_redis_client = redis_lib.from_url(REDIS_URL, decode_responses=True)

# ── FastMCP server instance ──────────────────────────────────────────────────
mcp = FastMCP("veritas-agent")


# ── Database helper ──────────────────────────────────────────────────────────
def _db_connect() -> psycopg2.extensions.connection:
    """Open a fresh psycopg2 connection. Caller is responsible for closing."""
    return psycopg2.connect(DATABASE_URL)


# ─────────────────────────────────────────────────────────────────────────────
# TIER-1 TOOLS  (Steps 1.2 – 1.10)
# ─────────────────────────────────────────────────────────────────────────────

# ── Step 1.2 — fetch_gdelt ───────────────────────────────────────────────────

from config.sections import SECTIONS  # noqa: E402

GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_TIMESPAN = "2d"   # Look back 1 day for each pipeline run


_TTL_GDELT: int = 3_600   # 1 hour — GDELT data changes slowly within a pipeline run

# Retry configuration for GDELT 429 responses.
# GDELT enforces ~1 request per second per IP; rapid repeated calls from the
# same session will trigger rate limiting. Exponential waits (30 s, 60 s)
# are long enough for the rate limit to clear between retries.
_GDELT_MAX_RETRIES: int = 3
_GDELT_RETRY_WAIT_BASE: int = 30   # seconds — multiplied by attempt number


@mcp.tool()
async def fetch_gdelt(section: str, limit: int = 75) -> dict:
    """
    Fetch Arabic-language news articles from GDELT DOC API for a given section.

    Uses the English query string from config/sections.py with a
    'sourcelang:arabic' filter — GDELT returns more reliable results with
    English query terms than with Arabic query terms directly.

    Results are cached in Redis for 1 hour (TTL = _TTL_GDELT) so that
    repeated pipeline or test invocations within the same hour do not
    trigger GDELT rate limiting. The cache key encodes section and limit.

    Returns a list of articles with: title, url, domain, seendate.
    Discards articles whose title is shorter than 15 characters.
    On GDELT 429 retries up to 3 times with exponential wait (30 s, 60 s).
    """
    if section not in SECTIONS:
        return {"error": f"Unknown section: '{section}'", "articles": [], "count": 0}

    cfg = SECTIONS[section]
    max_records = min(limit, cfg["max_articles_per_run"])

    # ── 1. Redis cache check ──────────────────────────────────────────────────
    gdelt_cache_key = f"gdelt:{section}:{max_records}"
    try:
        cached: str | None = _redis_client.get(gdelt_cache_key)  # type: ignore[assignment]
        if cached:
            cached_result: dict = json.loads(cached)
            return cached_result
    except Exception:
        pass  # Redis unavailable — fall through to GDELT API

    # ── 2. Build GDELT query ──────────────────────────────────────────────────
    # Country filter is only added for sections with a single country (Libya)
    # to avoid long OR chains that cause GDELT to time out or 429.
    query_parts = [cfg["query_gdelt"], "sourcelang:arabic"]

    if cfg["countries"]:
        # GDELT supports OR in sourcecountry: limit to top 5 to avoid 429
        top_countries = cfg["countries"][:5]
        if len(top_countries) == 1:
            query_parts.append(f"sourcecountry:{top_countries[0]}")
        else:
            country_filter = " OR ".join(f"sourcecountry:{c}" for c in top_countries)
            query_parts.append(f"({country_filter})")
            
    query = " ".join(query_parts)

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": max_records,
        "format": "json",
        "timespan": GDELT_TIMESPAN,
    }

    # ── 3. GDELT API call with retry on 429 ───────────────────────────────────
    last_exc: Exception | None = None
    data: dict = {}
    for attempt in range(_GDELT_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.get(GDELT_API_URL, params=params)
                if response.status_code == 429 and attempt < _GDELT_MAX_RETRIES - 1:
                    wait_secs = _GDELT_RETRY_WAIT_BASE * (attempt + 1)
                    await asyncio.sleep(wait_secs)
                    continue
                response.raise_for_status()
                data = response.json() if response.text.strip() else {}
            break   # success — exit retry loop
        except Exception as e:
            last_exc = e
            if attempt < _GDELT_MAX_RETRIES - 1:
                wait_secs = _GDELT_RETRY_WAIT_BASE * (attempt + 1)
                await asyncio.sleep(wait_secs)
    else:
        return {"error": str(last_exc), "articles": [], "count": 0, "section": section}

    # ── 4. Parse and filter articles ──────────────────────────────────────────
    raw_articles: list[dict] = data.get("articles") or []
    articles: list[dict] = []

    for art in raw_articles:
        title = (art.get("title") or "").strip()
        url = (art.get("url") or "").strip()
        domain = (art.get("domain") or "").strip()
        seendate = (art.get("seendate") or "").strip()

        if len(title) < 15 or not url:
            continue

        articles.append({
            "title": title,
            "url": url,
            "domain": domain,
            "seendate": seendate,
        })

    result: dict = {"articles": articles, "count": len(articles), "section": section}

    # ── 5. Cache the result ───────────────────────────────────────────────────
    try:
        _redis_client.setex(gdelt_cache_key, _TTL_GDELT, json.dumps(result))
    except Exception:
        pass  # Redis write failure is non-fatal

    return result


# ── Step 1.4 — scrape_article ────────────────────────────────────────────────

# CSS selectors tried in priority order when extracting the article body.
_ARTICLE_SELECTORS: list[str] = [
    "article",
    "main",
    ".article-body",
    ".article-content",
    "#content",
]

# Tags stripped before text extraction — they carry no editorial content.
_STRIP_TAGS: set[str] = {"script", "style", "nav", "footer", "header", "aside"}

# Arabic-language request headers for Layer 1 HTTP requests.
_AR_HEADERS: dict[str, str] = {
    "Accept-Language": "ar,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_MIN_CONTENT_LEN: int = 300   # minimum characters for extracted text to be accepted


def _extract_text_from_html(html: str) -> str:
    """
    Parse HTML with BeautifulSoup, strip non-content tags, and attempt to
    locate the article body using known CSS selectors. Falls back to the
    full <body> text if no selector matches. Returns stripped plain text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content tags in-place
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    # Try each article-body selector in priority order
    for selector in _ARTICLE_SELECTORS:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(separator=" ", strip=True)
            if len(text) >= _MIN_CONTENT_LEN:
                return text

    # Fall back to full body text
    body = soup.body
    if body:
        return body.get_text(separator=" ", strip=True)
    return soup.get_text(separator=" ", strip=True)


# Trafilatura extraction configuration. ``favor_precision=True`` is the
# critical setting — it removes JS, sidebars, "اقرأ أيضاً" / "الأكثر قراءة"
# blocks, and other non-article content automatically. The Arabic target
# language hint biases the precision-mode heuristics toward Arabic prose.
# Empirically verified on jo24.net, wafa.ps, and shorouknews.com.
_TRAFILATURA_KWARGS: dict = {
    "favor_precision": True,
    "target_language": "ar",
    "include_comments": False,
    "include_tables": False,
    "include_formatting": False,
}


def _trafilatura_extract(html: str) -> str:
    """
    Apply the project's standard trafilatura extraction settings to a raw
    HTML string. Returns the extracted text (possibly empty), or empty
    string when trafilatura returns None / fails entirely. Never raises.
    """
    try:
        text = trafilatura.extract(html, **_TRAFILATURA_KWARGS)
    except Exception:
        return ""
    return (text or "").strip()


# Regex that captures the original source name from libyaakhbar.com articles.
# The site renders attribution as: مصدر الخبر / <a href="...">SOURCE NAME</a>
# Tested against live HTML of https://www.libyaakhbar.com/libya-news/2794437.html.
_AGGREGATOR_SOURCE_RE: re.Pattern = re.compile(
    r'مصدر الخبر\s*/\s*<a[^>]*>\s*([^<]+?)\s*</a>'
)

# Domains whose HTML carries a مصدر الخبر attribution line that Trafilatura
# strips. Extend this list when additional aggregator domains are confirmed.
_AGGREGATOR_DOMAINS: tuple[str, ...] = ("libyaakhbar.com",)


def _extract_aggregator_source(html: str, url: str) -> str | None:
    """
    Return the original source name embedded in aggregator pages, or None.

    Checks whether ``url`` belongs to a known aggregator domain (simple
    substring match, handles www. prefix automatically). If so, searches
    ``html`` for the first ``مصدر الخبر / <a>…</a>`` pattern and returns
    the captured source name stripped of surrounding whitespace.

    Returns None when the URL is not from an aggregator, when the pattern
    is absent (aggregator HTML changed), or on any error — callers must
    treat None as "no attribution available" and proceed without prepending.
    Never raises.
    """
    try:
        if not any(domain in url for domain in _AGGREGATOR_DOMAINS):
            return None
        match = _AGGREGATOR_SOURCE_RE.search(html)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return None


def _playwright_fetch_html(url: str) -> str:
    """
    Synchronous Playwright HTML fetch shared between Layer 2 (trafilatura
    on a JS-rendered page) and Layer 3 (BS4 safety-net on the same page).
    Returns the rendered HTML or raises on navigation / launch failure;
    the caller wraps the call in ``asyncio.to_thread`` and try/except.
    Browser is always closed in a ``finally`` block.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.content()
        finally:
            browser.close()


@mcp.tool()
async def scrape_article(url: str) -> dict:
    """
    Fetch the full text of an article using a four-layer fallback strategy.

    Layer 1 — Plain HTTP + Trafilatura (precision mode):
        ``httpx`` GET with Arabic-language headers and a 12-second timeout.
        The raw HTML is passed to ``trafilatura.extract`` with
        ``favor_precision=True``, which strips JS, sidebars, and "اقرأ
        أيضاً" / "الأكثر قراءة" blocks. Returns ``method="trafilatura_http"``
        if the extracted text is ≥ 300 chars.

    Layer 2 — Playwright + Trafilatura (precision mode):
        Used when Layer 1 fails to fetch, or when trafilatura returns
        less than 300 chars (typical for JS-rendered sites like
        shorouknews.com). The same precision-mode extraction is applied
        to the Playwright-rendered HTML. Returns
        ``method="trafilatura_playwright"``.

    Layer 3 — BeautifulSoup safety-net fallback:
        Reuses ``_extract_text_from_html`` on whichever HTML was already
        fetched in Layer 1 and/or Layer 2 (no redundant network calls).
        This is the safety net for sites where trafilatura returns None
        but the heuristic CSS-selector path still finds the body.
        Returns ``method="bs4_fallback"``.

    Layer 4 — Failed (title-only):
        If every layer either failed to fetch or produced < 300 chars,
        returns ``{"success": false, "content": null, "method": "failed",
        "layer1_error": ..., "layer2_error": ..., "layer3_error": ...}``.
        The calling agent must store the article with
        ``has_full_content = false``. Never raises an exception.

    The minimum-content threshold (``_MIN_CONTENT_LEN`` = 300) is applied
    to every layer's output; below-threshold output triggers the next
    layer. Success-shape (``success``, ``content``, ``method``) is
    unchanged from the previous three-layer implementation — only the
    ``method`` value set expanded to four distinct identifiers
    (``trafilatura_http``, ``trafilatura_playwright``, ``bs4_fallback``,
    ``failed``) for observability.
    """
    # HTML caches — populated by Layers 1 and 2, consumed by Layer 3 to
    # avoid redundant network calls.
    http_html: str = ""
    playwright_html: str = ""

    # ── Layer 1 — HTTP + Trafilatura (precision) ──────────────────────────────
    layer1_error: str = ""
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            headers=_AR_HEADERS,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            http_html = response.text

        text = _trafilatura_extract(http_html)

        if len(text) >= _MIN_CONTENT_LEN:
            source = _extract_aggregator_source(http_html, url)
            content = f"المصدر: {source}\n\n{text}" if source else text
            return {"success": True, "content": content, "method": "trafilatura_http"}

        layer1_error = f"insufficient content ({len(text)} chars)"
    except Exception as e:
        layer1_error = str(e)

    # ── Layer 2 — Playwright + Trafilatura (precision) ────────────────────────
    layer2_error: str = ""
    try:
        playwright_html = await asyncio.to_thread(_playwright_fetch_html, url)
        text = _trafilatura_extract(playwright_html)

        if len(text) >= _MIN_CONTENT_LEN:
            source = _extract_aggregator_source(playwright_html, url)
            content = f"المصدر: {source}\n\n{text}" if source else text
            return {"success": True, "content": content, "method": "trafilatura_playwright"}

        layer2_error = f"insufficient content ({len(text)} chars)"
    except Exception as e:
        layer2_error = str(e)

    # ── Layer 3 — BeautifulSoup safety-net fallback ───────────────────────────
    # Operates on HTML already fetched above — no new network calls. Tries
    # the HTTP HTML first (cheaper, plain markup), then falls back to the
    # Playwright HTML if available.
    layer3_error: str = ""
    try:
        bs4_text: str = ""
        if http_html:
            candidate = _extract_text_from_html(http_html)
            if len(candidate) >= _MIN_CONTENT_LEN:
                bs4_text = candidate
        if not bs4_text and playwright_html:
            candidate = _extract_text_from_html(playwright_html)
            if len(candidate) >= _MIN_CONTENT_LEN:
                bs4_text = candidate

        if bs4_text:
            return {"success": True, "content": bs4_text, "method": "bs4_fallback"}

        if not http_html and not playwright_html:
            layer3_error = "no HTML available (Layers 1 and 2 both failed to fetch)"
        else:
            layer3_error = "insufficient content from BeautifulSoup extraction"
    except Exception as e:
        layer3_error = str(e)

    # ── Layer 4 — Title-only failure ──────────────────────────────────────────
    # Every layer either failed to fetch or produced < 300 chars. Return
    # the canonical failure dict so the calling agent stores the article
    # with has_full_content = false instead of discarding it.
    return {
        "success": False,
        "content": None,
        "method": "failed",
        "layer1_error": layer1_error,
        "layer2_error": layer2_error,
        "layer3_error": layer3_error,
    }


# ── Step 1.6 — store_article ─────────────────────────────────────────────────

import re as _re  # already imported at top; alias to satisfy the local scope ref
from datetime import datetime, timezone as _tz


def _parse_published_at(raw: str) -> datetime:
    """
    Parse the published_at string into an aware datetime.
    Accepts two formats:
      - GDELT seendate:  20240408T132500Z
      - ISO-8601 string: 2024-04-08T13:25:00Z  (or +00:00 offset)
    Raises ValueError if neither format matches.
    """
    raw = raw.strip()
    if _re.match(r"^\d{8}T\d{6}Z$", raw):
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=_tz.utc)
    # Normalise the Z suffix so fromisoformat accepts it
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


@mcp.tool()
async def store_article(
    title: str,
    url: str,
    section: str,
    published_at: str,
    embedding: list[float],
    content: str | None = None,
    entities: dict | None = None,
    has_full_content: bool = True,
) -> dict:
    """
    Insert a new article into the articles table.

    Uses INSERT ... ON CONFLICT (url) DO NOTHING.
    If the URL already exists the existing record is preserved unchanged —
    DO UPDATE is never used for this table (Rule 2.8).

    Returns {"id": int, "is_new": bool} on success.
    Returns {"error": str} on failure. Never raises an exception.

    published_at must be either the raw GDELT seendate (YYYYMMDDTHHMMSSZ)
    or any ISO-8601 timestamp string. The Ingestion Agent is responsible
    for passing this field; articles without it cannot be clustered.
    """
    # ── 1. Parse and validate published_at ───────────────────────────────────
    try:
        published_at_dt: datetime = _parse_published_at(published_at)
    except Exception as e:
        return {"error": f"invalid published_at '{published_at}': {e}"}

    # ── 2. Normalise entities ─────────────────────────────────────────────────
    entities_json: str = json.dumps(entities or {"people": [], "locations": [], "organizations": []})

    # ── 3. Format embedding for pgvector ─────────────────────────────────────
    # psycopg2 does not natively adapt list[float] to the vector type.
    # Passing a bracket-formatted string with ::vector cast is the safe,
    # dependency-free approach that works with any pgvector version.
    embedding_pg: str = "[" + ",".join(str(v) for v in embedding) + "]"

    def _sync_store() -> dict:
        conn = _db_connect()
        try:
            with conn:
                cur = conn.cursor()

                # Attempt the insert; ON CONFLICT (url) DO NOTHING means
                # a duplicate URL is silently skipped and RETURNING yields
                # no rows — handled below.
                cur.execute(
                    """
                    INSERT INTO articles
                        (title, content, url, section,
                         published_at, embedding, entities, has_full_content)
                    VALUES
                        (%s, %s, %s, %s,
                         %s, %s::vector, %s::jsonb, %s)
                    ON CONFLICT (url) DO NOTHING
                    RETURNING id
                    """,
                    (
                        title,
                        content,
                        url,
                        section,
                        published_at_dt,
                        embedding_pg,
                        entities_json,
                        has_full_content,
                    ),
                )
                row = cur.fetchone()

                if row:
                    # New record was inserted
                    return {"id": row[0], "is_new": True}

                # URL conflict — fetch the existing record's id
                cur.execute("SELECT id FROM articles WHERE url = %s", (url,))
                existing = cur.fetchone()
                if existing:
                    return {"id": existing[0], "is_new": False}

                return {"error": "insert returned no row and url lookup failed"}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync_store)
    except Exception as e:
        return {"error": str(e)}


# ── Step 1.7 — find_similar ──────────────────────────────────────────────────

_72H_SECONDS: int = 72 * 3600   # 259 200 — used in the SQL time-window clause


@mcp.tool()
async def find_similar(
    embedding: list[float],
    section: str,
    published_at: str,
    threshold: float = 0.82,
    limit: int = 10,
    exclude_id: int | None = None,
) -> dict:
    """
    Find articles similar to a query embedding using pgvector cosine similarity.

    All three filters are enforced in a single SQL query — none in application code:

      1. section   — must equal the supplied section value.
      2. Time window — ABS difference between article published_at and the
                       reference published_at must be ≤ 72 hours. Evaluated
                       with EXTRACT(EPOCH ...) at the database level.
      3. Similarity threshold — 1 - (embedding <=> query_embedding) ≥ threshold.

    Results are ordered by descending similarity (ascending cosine distance).
    Returns {"articles": [{id, title, published_at, similarity}, ...], "count": int}.
    Never raises an exception.

    exclude_id: optional article id to exclude from results (typically the
                source article being compared against itself).
    """
    # Validate inputs before touching the database
    if section not in SECTIONS:
        return {"error": f"Unknown section: '{section}'", "articles": [], "count": 0}
    if not (0.0 <= threshold <= 1.0):
        return {"error": f"threshold must be in [0, 1], got {threshold}",
                "articles": [], "count": 0}

    # Parse the reference timestamp — reuses the helper from store_article
    try:
        ref_dt: datetime = _parse_published_at(published_at)
    except Exception as e:
        return {"error": f"invalid published_at: {e}", "articles": [], "count": 0}

    # Format embedding for pgvector
    embedding_pg: str = "[" + ",".join(str(v) for v in embedding) + "]"

    def _sync_query() -> dict:
        conn = _db_connect()
        try:
            cur = conn.cursor()

            # Build optional exclude clause
            exclude_clause = "AND id != %s" if exclude_id is not None else ""
            params_extra   = (exclude_id,) if exclude_id is not None else ()

            # All three spec conditions are in the WHERE clause.
            # The time window uses EXTRACT(EPOCH FROM ...) to compute the
            # absolute second-difference between timestamps at the DB level.
            # Cosine distance operator <=> returns 0 for identical vectors
            # and 2 for maximally dissimilar; similarity = 1 - distance.
            cur.execute(
                f"""
                SELECT
                    id,
                    title,
                    published_at,
                    (1 - (embedding <=> %s::vector)) AS similarity
                FROM articles
                WHERE section = %s
                  AND ABS(EXTRACT(EPOCH FROM (published_at - %s::timestamptz)))
                      <= {_72H_SECONDS}
                  AND (1 - (embedding <=> %s::vector)) >= %s
                  {exclude_clause}
                ORDER BY embedding <=> %s::vector   -- ascending distance = descending similarity
                LIMIT %s
                """,
                (
                    embedding_pg,           # similarity calc
                    section,
                    ref_dt,                 # time window reference
                    embedding_pg,           # threshold filter
                    threshold,
                    *params_extra,
                    embedding_pg,           # ORDER BY
                    limit,
                ),
            )
            rows = cur.fetchall()

            articles = [
                {
                    "id":           row[0],
                    "title":        row[1],
                    "published_at": row[2].isoformat() if row[2] else None,
                    "similarity":   round(float(row[3]), 6),
                }
                for row in rows
            ]
            return {"articles": articles, "count": len(articles)}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync_query)
    except Exception as e:
        return {"error": str(e), "articles": [], "count": 0}


# ── Step 1.9 — classify_bias (LLM-embedded) removed in Step 4.5.4.
#    `_VALID_BIAS_LABELS` below is retained because `insert_bias_score`
#    (Step 4.5.3) reuses it for server-side label validation. ─────────────────

_VALID_BIAS_LABELS: frozenset[str] = frozenset({
    "pro_government", "opposition", "neutral", "pan_arab", "western_aligned",
})


# ── Step 1.10 — cache_set / cache_get ────────────────────────────────────────

@mcp.tool()
async def cache_set(key: str, value: str, ttl: int) -> dict:
    """
    Write a string value to Redis with a TTL in seconds.

    Returns {"success": True} on success, {"success": False, "error": str}
    on any Redis failure. Never raises an exception.
    ttl must be > 0; a zero or negative TTL is rejected with success=False.
    """
    if ttl <= 0:
        return {"success": False, "error": f"ttl must be > 0, got {ttl}"}
    try:
        def _set() -> bool:
            return bool(_redis_client.setex(key, ttl, value))
        result = await asyncio.to_thread(_set)
        return {"success": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def cache_get(key: str) -> dict:
    """
    Read a string value from Redis by key.

    Returns {"value": str} if the key exists, {"value": None} if it does not
    or has expired. Never raises an exception.
    """
    try:
        cached: str | None = await asyncio.to_thread(  # type: ignore[assignment]
            _redis_client.get, key
        )
        return {"value": cached}
    except Exception as e:
        return {"value": None, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# TIER-2 TOOLS  (Step 4.1)
# ─────────────────────────────────────────────────────────────────────────────

# ── Tier-2 Tool 1 — detect_blindspot ─────────────────────────────────────────

_ALL_BIAS_LABELS: tuple[str, ...] = (
    "pro_government", "opposition", "neutral", "pan_arab", "western_aligned",
)
_BLINDSPOT_THRESHOLD: float = 0.15   # fraction of per-label average below which a label is underrepresented
_MIN_ARTICLES_FOR_BLINDSPOT: int = 3  # events with fewer scored articles are excluded


@mcp.tool()
async def detect_blindspot(event_id: int) -> dict:
    """
    Detect underrepresented bias perspectives in an event's coverage.

    Retrieves the bias-label distribution for the event (via article_events
    + bias_scores). If the event has fewer than 3 articles with bias scores,
    returns no blindspot (insufficient data for a meaningful analysis).

    Algorithm:
      average = total_classified_articles / 5   (all five labels in taxonomy)
      threshold = 0.15 × average
      missing  = [label for label in ALL_LABELS if count(label) < threshold]

    Returns:
      {"event_id": int, "missing_perspectives": [...], "coverage_stats": {...},
       "total": int, "has_blindspot": bool}
    Never raises an exception.
    """
    def _sync() -> dict:
        conn = _db_connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT bs.label, COUNT(*) AS cnt
                FROM article_events ae
                JOIN bias_scores bs ON bs.article_id = ae.article_id
                WHERE ae.event_id = %s
                GROUP BY bs.label
                """,
                (event_id,),
            )
            rows = cur.fetchall()
            coverage: dict[str, int] = {label: int(cnt) for label, cnt in rows}
            total = sum(coverage.values())

            if total < _MIN_ARTICLES_FOR_BLINDSPOT:
                return {
                    "event_id":             event_id,
                    "missing_perspectives": [],
                    "coverage_stats":       coverage,
                    "total":                total,
                    "has_blindspot":        False,
                    "note": (
                        f"Insufficient data ({total} articles with bias scores,"
                        f" minimum {_MIN_ARTICLES_FOR_BLINDSPOT})"
                    ),
                }

            # Average per label across all 5 taxonomy labels (including absent ones)
            avg = total / len(_ALL_BIAS_LABELS)
            threshold = _BLINDSPOT_THRESHOLD * avg

            missing = [
                label for label in _ALL_BIAS_LABELS
                if coverage.get(label, 0) < threshold
            ]

            return {
                "event_id":             event_id,
                "missing_perspectives": missing,
                "coverage_stats":       coverage,
                "total":                total,
                "has_blindspot":        len(missing) > 0,
            }
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {
            "error":                str(e),
            "event_id":             event_id,
            "missing_perspectives": [],
            "coverage_stats":       {},
            "total":                0,
            "has_blindspot":        False,
        }


# ── Tier-2 Tool 5 — vector_recommend ─────────────────────────────────────────

@mcp.tool()
async def vector_recommend(
    article_id: int,
    section: str,
    limit: int = 5,
    min_similarity: float = 0.0,
) -> dict:
    """
    Find semantically similar articles with a different bias label.

    Retrieves the source article's bias label. Then uses a pgvector cosine-
    similarity search (via a CROSS JOIN subquery — no Python-side embedding
    serialisation required) to find articles in the same section that are
    most similar but have a different bias label.

    min_similarity filters out results whose cosine similarity falls below the
    specified value. Default is 0.0 (no negative-similarity results).
    Cosine similarity = 1 - cosine_distance; range is [-1.0, 1.0].

    If the source article has no bias score yet, the label filter is omitted
    and all similar articles in the section are returned.

    Returns {"article_id": int, "source_label": str|None,
             "recommendations": [{id, title, bias_label, similarity}, ...]}.
    Returns {"error": str} if the article is not found or has no embedding.
    Never raises an exception.
    """
    if section not in SECTIONS:
        return {
            "error":           f"Unknown section: '{section}'",
            "article_id":      article_id,
            "recommendations": [],
        }

    def _sync() -> dict:
        conn = _db_connect()
        try:
            cur = conn.cursor()

            # ── 1. Verify source article exists and has an embedding ──────────
            cur.execute(
                """
                SELECT a.id, bs.label
                FROM articles a
                LEFT JOIN bias_scores bs ON bs.article_id = a.id
                WHERE a.id = %s AND a.embedding IS NOT NULL
                """,
                (article_id,),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "error":           f"Article {article_id} not found or has no embedding",
                    "article_id":      article_id,
                    "recommendations": [],
                }
            source_label: str | None = row[1]

            # ── 2. Find similar articles via pgvector CROSS JOIN subquery ─────
            # Using a subquery keeps the embedding in PostgreSQL — no need to
            # serialise the 768-float vector through Python.
            # min_similarity filter excludes low-quality (near-zero or negative)
            # cosine similarity results, ensuring recommendations are genuinely
            # related to the source article.
            if source_label:
                cur.execute(
                    """
                    SELECT
                        a.id,
                        a.title,
                        bs.label  AS bias_label,
                        (1 - (a.embedding <=> src.embedding)) AS similarity
                    FROM articles a
                    JOIN bias_scores bs ON bs.article_id = a.id
                    CROSS JOIN (SELECT embedding FROM articles WHERE id = %s) AS src
                    WHERE a.section = %s
                      AND a.id != %s
                      AND bs.label != %s
                      AND (1 - (a.embedding <=> src.embedding)) >= %s
                    ORDER BY a.embedding <=> src.embedding
                    LIMIT %s
                    """,
                    (article_id, section, article_id, source_label, min_similarity, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        a.id,
                        a.title,
                        bs.label  AS bias_label,
                        (1 - (a.embedding <=> src.embedding)) AS similarity
                    FROM articles a
                    JOIN bias_scores bs ON bs.article_id = a.id
                    CROSS JOIN (SELECT embedding FROM articles WHERE id = %s) AS src
                    WHERE a.section = %s
                      AND a.id != %s
                      AND (1 - (a.embedding <=> src.embedding)) >= %s
                    ORDER BY a.embedding <=> src.embedding
                    LIMIT %s
                    """,
                    (article_id, section, article_id, min_similarity, limit),
                )

            rows = cur.fetchall()
            recommendations = [
                {
                    "id":         int(r[0]),
                    "title":      r[1],
                    "bias_label": r[2],
                    "similarity": round(float(r[3]), 6),
                }
                for r in rows
            ]

            return {
                "article_id":      article_id,
                "source_label":    source_label,
                "recommendations": recommendations,
            }
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {
            "error":           str(e),
            "article_id":      article_id,
            "recommendations": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4.5.3 — PURE DATA-ACCESS TOOLS (canonical MCP migration)
# No LLM calls — all psycopg2 work wrapped in asyncio.to_thread per Rule 2.2.
# Every tool returns a structured dict and never raises (Rule 2.4).
# ─────────────────────────────────────────────────────────────────────────────

from urllib.parse import urlparse  # noqa: E402


def _parse_embedding_column(raw: object) -> list[float]:
    """
    Convert the value returned by psycopg2 for a pgvector(768) column into a
    list[float]. Depending on the pgvector adapter registration, the driver
    may return either a bracketed string like "[0.1,0.2,...]" or a list of
    floats. Both shapes are handled; any other shape returns an empty list.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            return [float(x) for x in json.loads(raw)]
        except Exception:
            return []
    return []


def _parse_entities_column(raw: object) -> dict:
    """
    Convert the value returned by psycopg2 for a JSONB column into a dict.
    psycopg2 normally returns JSONB as a dict already; fall back to json.loads
    on strings and to an empty dict on anything unexpected.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


# ── Group B — Article Operations ─────────────────────────────────────────────

@mcp.tool()
async def get_articles(ids: list[int]) -> dict:
    """
    Fetch article rows by id.

    Empty input list short-circuits with no DB round-trip.

    Returns {"articles": [...], "count": int}. Each article dict contains:
      id, title, content, url, entities (dict), embedding (list[float]),
      published_at (ISO string or None), section.
    Never raises an exception (Rule 2.4).
    """
    if not ids:
        return {"articles": [], "count": 0}

    def _sync() -> dict:
        conn = _db_connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, title, content, url, entities, embedding,
                       published_at, section
                FROM articles
                WHERE id = ANY(%s)
                ORDER BY id
                """,
                (list(ids),),
            )
            rows = cur.fetchall()
            articles = [
                {
                    "id":           int(r[0]),
                    "title":        r[1] or "",
                    "content":      r[2] or "",
                    "url":          r[3] or "",
                    "entities":     _parse_entities_column(r[4]),
                    "embedding":    _parse_embedding_column(r[5]),
                    "published_at": r[6].isoformat() if r[6] else None,
                    "section":      r[7] or "",
                }
                for r in rows
            ]
            return {"articles": articles, "count": len(articles)}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e), "articles": [], "count": 0}


@mcp.tool()
async def get_articles_for_event(event_id: int) -> dict:
    """
    Fetch all articles linked to an event, joined with bias_scores when present.

    SQL path: article_events (event_id) -> articles (article_id) left joined
    to bias_scores (article_id). Articles without a bias_scores row return
    label=None, confidence=None, framing=None.

    Returns {"articles": [...]} where each item has:
      id, title, url, source (URL hostname via urlparse),
      label, confidence, framing.
    Never raises an exception (Rule 2.4).
    """
    def _sync() -> dict:
        conn = _db_connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.id, a.title, a.url,
                       bs.label, bs.confidence, bs.framing
                FROM article_events ae
                JOIN articles a ON a.id = ae.article_id
                LEFT JOIN bias_scores bs ON bs.article_id = a.id
                WHERE ae.event_id = %s
                ORDER BY a.id
                """,
                (event_id,),
            )
            rows = cur.fetchall()
            articles: list[dict] = []
            for r in rows:
                url = r[2] or ""
                try:
                    source = urlparse(url).hostname or ""
                except Exception:
                    source = ""
                articles.append({
                    "id":         int(r[0]),
                    "title":      r[1] or "",
                    "url":        url,
                    "source":     source,
                    "label":      r[3],
                    "confidence": float(r[4]) if r[4] is not None else None,
                    "framing":    r[5],
                })
            return {"articles": articles}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e), "articles": []}


# ── Group C — Event Operations ───────────────────────────────────────────────

@mcp.tool()
async def insert_event(
    section: str,
    title: str,
    created_at: str | None = None,
) -> dict:
    """
    Create a new row in the events table for a discovered cluster.

    Maps the `title` parameter to the `headline` column (deviation from
    plan.md Step 4.5.3 spec — see progress_log.md Step 4.5.3 Deviations).
    `representative_article_id` is intentionally NOT part of the signature:
    the live events table has no column to persist it.

    When created_at is None, NOW() is used.
    Events have no natural unique key — duplicate-prevention lives in the
    Clustering Agent, so this tool has no ON CONFLICT clause.

    Returns {"event_id": int} on success.
    Never raises an exception (Rule 2.4).
    """
    if section not in SECTIONS:
        return {"error": f"Unknown section: '{section}'"}

    def _sync() -> dict:
        conn = _db_connect()
        try:
            with conn:
                cur = conn.cursor()
                if created_at is None:
                    cur.execute(
                        """
                        INSERT INTO events (section, headline, created_at)
                        VALUES (%s, %s, NOW())
                        RETURNING id
                        """,
                        (section, title),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO events (section, headline, created_at)
                        VALUES (%s, %s, %s)
                        RETURNING id
                        """,
                        (section, title, created_at),
                    )
                row = cur.fetchone()
                if not row:
                    return {"error": "insert_event returned no row"}
                return {"event_id": int(row[0])}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def link_article_event(
    event_id: int,
    article_id: int,
    relevance_score: float = 1.0,
) -> dict:
    """
    Insert a row into article_events linking an article to an event.

    Uses ON CONFLICT (article_id, event_id) DO NOTHING (Rule 2.8 — idempotent).
    Returns {"linked": True} when a new row was inserted, {"linked": False}
    when the pair already existed. Never raises an exception (Rule 2.4).
    """
    def _sync() -> dict:
        conn = _db_connect()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO article_events (event_id, article_id, relevance_score)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (article_id, event_id) DO NOTHING
                    RETURNING id
                    """,
                    (event_id, article_id, relevance_score),
                )
                row = cur.fetchone()
                return {"linked": bool(row)}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e), "linked": False}


@mcp.tool()
async def update_event_summary(
    event_id: int,
    neutral_summary: str,
    bias_assessment: str,
) -> dict:
    """
    Update events.summary and events.bias_assessment for the given event_id.

    Short-circuit: if BOTH inputs are empty after stripping whitespace, the
    tool returns {"updated": False, "reason": "both inputs empty"} without
    touching the database.

    Otherwise uses COALESCE(NULLIF(%s, ''), existing_column) for BOTH fields
    so an empty new value preserves the prior column value (Rule 2.8 /
    blueprint.md Section 4). Do NOT simplify either wrapper — NULLIF
    converts '' to NULL, COALESCE keeps the existing column on NULL.

    Returns {"updated": bool} based on cursor.rowcount.
    Never raises an exception (Rule 2.4).
    """
    if not neutral_summary.strip() and not bias_assessment.strip():
        return {"updated": False, "reason": "both inputs empty"}

    def _sync() -> dict:
        conn = _db_connect()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE events
                       SET summary          = COALESCE(NULLIF(%s, ''), summary),
                           bias_assessment  = COALESCE(NULLIF(%s, ''), bias_assessment)
                     WHERE id = %s
                    """,
                    (neutral_summary, bias_assessment, event_id),
                )
                return {"updated": cur.rowcount > 0}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e), "updated": False}


# ── Group D — Bias Storage ───────────────────────────────────────────────────

@mcp.tool()
async def insert_bias_score(
    article_id: int,
    score: float,
    label: str,
    confidence: float,
    framing: str,
) -> dict:
    """
    Insert a row into bias_scores.

    Validates `label` against the five canonical labels before the DB call
    (returns a structured error without touching the DB on mismatch).

    Uses ON CONFLICT (article_id) DO NOTHING per Rule 2.8 — a bias score
    already stored for an article must not be overwritten by a re-run.

    Returns {"inserted": bool, "article_id": int}:
      inserted=True only when fetchone() returned a row (new insert).
      inserted=False when the article_id already had a bias score.
    Never raises an exception (Rule 2.4).
    """
    if label not in _VALID_BIAS_LABELS:
        return {"error": f"invalid label: {label}"}

    def _sync() -> dict:
        conn = _db_connect()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO bias_scores
                        (article_id, score, label, confidence, framing)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (article_id) DO NOTHING
                    RETURNING id
                    """,
                    (article_id, score, label, confidence, framing),
                )
                row = cur.fetchone()
                return {"inserted": bool(row), "article_id": article_id}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e), "inserted": False, "article_id": article_id}


# ── Group F — Blindspot Storage ──────────────────────────────────────────────

@mcp.tool()
async def insert_blindspot_report(
    event_id: int,
    coverage_stats: dict,
    missing_perspectives: list[str],
) -> dict:
    """
    Insert a row into blindspot_reports with an idempotency guard.

    The blindspot_reports table has no UNIQUE constraint on event_id, so we
    use an INSERT ... SELECT ... WHERE NOT EXISTS construct to keep re-runs
    idempotent (Rule 2.8). coverage_stats is serialized with json.dumps()
    before the %s::jsonb cast.

    Returns {"inserted": bool} based on whether fetchone() returned a row.
    Never raises an exception (Rule 2.4).
    """
    try:
        coverage_json = json.dumps(coverage_stats or {})
    except Exception as e:
        return {"error": f"coverage_stats not JSON-serializable: {e}", "inserted": False}

    def _sync() -> dict:
        conn = _db_connect()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO blindspot_reports
                        (event_id, coverage_stats, missing_perspectives)
                    SELECT %s, %s::jsonb, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM blindspot_reports WHERE event_id = %s
                    )
                    RETURNING id
                    """,
                    (event_id, coverage_json, list(missing_perspectives or []), event_id),
                )
                row = cur.fetchone()
                return {"inserted": bool(row)}
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        return {"error": str(e), "inserted": False}


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        stateless_http=True,
        json_response=True,
    )
