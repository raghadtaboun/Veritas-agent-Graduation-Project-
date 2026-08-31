"""
evaluation/evaluate_clustering.py — Phase 6 Step 6.3: Clustering Evaluation Harness

Standalone research script. Tests whether the production clustering logic groups
same-event articles together — articles covering the SAME real-world event but
written with DIFFERENT bias framings (and, for groups 4, 6, 7, different titles)
— into a single event cluster, despite the lexical variation.

Read-only on the production system (the key design constraint). The production
``ClusteringAgent.run()`` cannot be invoked directly here because it reads via
``get_articles`` / ``find_similar`` and WRITES to the production ``events`` and
``article_events`` tables (via ``insert_event`` / ``link_article_event``), and
the dataset article IDs are dataset-local, not DB serial IDs. Inserting the eval
articles into the DB would pollute production tables and is rejected.

Instead, this harness reproduces the clustering ORCHESTRATION in-memory while
reusing the production DECISION LOGIC by import, so production is never touched:

  * Embeddings  — ``agents.llm_client.gemini_embed`` (the exact path
                  ``MCPAgent.call_gemini_embedding`` uses in production).
  * Entities    — production ``_ENTITY_PROMPT_TEMPLATE`` + ``_normalise_entities``
                  (imported from ``agents/ingestion_agent.py``) via
                  ``gemini_generate_with_fallback(task_type="entities")`` — the
                  same path ``IngestionAgent._extract_entities`` uses.
  * Conditions  — reused verbatim from ``agents/clustering_agent.py``:
                    Condition 1: |published_at_A - published_at_B| <= 72h
                    Condition 2: cosine_similarity >= _SIMILARITY_THRESHOLD (0.82)
                    Condition 3: _entity_overlap(...) >= _ENTITY_OVERLAP_MIN (2)
                  plus the same section filter and same greedy in-order
                  assignment ClusteringAgent.run() performs. Cosine is computed
                  exactly in numpy (stricter than pgvector's approximate HNSW
                  search — documented as a deviation in the summary).

No MCP server, no PostgreSQL, no Redis are required or contacted. The only
external calls are the Gemini embedding/entity API calls (expected per the task).
Preprocessing (embeddings + entities) is cached to a clearly-namespaced local
file under evaluation/results/ so re-runs are cheap; this is NOT Redis and NOT
the production DB.

Multi-key quota survival (Phase 6, 2026-06-13). Key rotation is NOT
re-implemented here: both Gemini call sites delegate to ``agents.llm_client``
(``gemini_embed`` / ``gemini_generate_with_fallback``), which already own the
production ``GeminiKeyPool`` — round-robin across ``GEMINI_API_KEY`` ..
``GEMINI_API_KEY_10``, per-key 1-hour quarantine on 429/resource_exhausted,
same-key backoff on transient 503/500, fail-fast on permanent errors. So a
single run already rotates across every configured key. This harness adds only
quota-*awareness*: it detects the pool's "all keys exhausted/quarantined" signal,
stops cleanly after that point, preserves the preprocess cache (saved
incrementally, atomically, per completed article), and does NOT overwrite
clustering_results.json with a degraded partial result. A later run with
refreshed quota resumes from the cache and re-spends quota only on the
articles not yet completed.

Output: evaluation/results/clustering_results.json (per-group outcome + the
cluster assignment of all 60 articles) plus a stdout summary table.

Rule compliance (agent.md): Rule 2.4 (every API call wrapped, recorded not
fatal), Rule 2.6 / bootstrap (bootstrap_env before the Google SDK import).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Project import path + environment bootstrap (Rule 2.6) ────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.env_bootstrap import bootstrap_env  # noqa: E402

bootstrap_env()

import numpy as np  # noqa: E402

from agents import llm_client  # noqa: E402

# Reuse production entity extraction (prompt + normalisation) verbatim.
from agents.ingestion_agent import (  # noqa: E402
    _ENTITY_PROMPT_TEMPLATE,
    _strip_code_fences,
    IngestionAgent,
)

_normalise_entities = IngestionAgent._normalise_entities

# Reuse production clustering decision logic verbatim.
from agents.clustering_agent import (  # noqa: E402
    _ENTITY_OVERLAP_MIN,
    _FIND_SIMILAR_LIMIT,
    _SIMILARITY_THRESHOLD,
    _entity_overlap,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("evaluate_clustering")


# ── Constants mirroring production ────────────────────────────────────────────
# 72-hour window matches mcp_server.find_similar (_72H_SECONDS). Defined locally
# because importing the server module would start FastMCP / DB clients.
_WINDOW_SECONDS: int = 72 * 3600

# Production truncates embedding/entity inputs to 3000 chars (see
# IngestionAgent._generate_embedding / _extract_entities).
_PREPROCESS_CHARS: int = 3000

_HARD_GROUPS: set[int] = {4, 6, 7}  # different titles — the informative cases

_DATASET_PATH = _PROJECT_ROOT / "evaluation" / "dataset.json"
_GROUND_TRUTH_PATH = _PROJECT_ROOT / "evaluation" / "clustering_ground_truth.json"
_RESULTS_DIR = _PROJECT_ROOT / "evaluation" / "results"
_RESULTS_PATH = _RESULTS_DIR / "clustering_results.json"
_SWEEP_RESULTS_PATH = _RESULTS_DIR / "clustering_sweep.json"  # 2-D sweep output only
_CACHE_PATH = _RESULTS_DIR / ".preprocess_cache.json"  # local scratch, not Redis/DB

# Output-token budget for entity extraction. Raised from 2048 because the entity
# model spends part of this budget on internal reasoning before emitting the JSON;
# on long Arabic-heavy articles (e.g. dataset id 34) the 2048 ceiling truncated the
# JSON mid-object (~100 visible chars, no closing brace), so the article yielded no
# entities. 8192 leaves ample room for the reasoning plus the full entity JSON.
_ENTITIES_MAX_TOKENS: int = 8192
# Entity-extraction parse robustness: the entity model occasionally wraps its
# JSON in surrounding prose or emits trailing text, which makes a direct
# json.loads fail and previously dropped the whole article's entities. We salvage
# the embedded JSON object and, if still unparseable, retry the call up to this
# many times total. This affects only how the model's *text output is parsed* —
# the prompt, model, normalisation, and every clustering threshold are unchanged.
_ENTITY_PARSE_ATTEMPTS: int = 3

# ── Experimental merge-policy parameters (OFF by default) ─────────────────────
# These power optional A/B variants that the harness can run WITHOUT changing the
# production decision logic. With strategy="greedy" and adaptive=False the harness
# reproduces ClusteringAgent.run() exactly (cosine >= _SIMILARITY_THRESHOLD AND
# entities >= _ENTITY_OVERLAP_MIN, greedy in-order, claim-once). The adaptive
# branch lets a strong entity signal license a slightly lower cosine floor; it is
# an evaluation experiment only and alters no production threshold or code.
_ADAPTIVE_COSINE_FLOOR: float = 0.80   # lower cosine floor when entity signal is strong
_ADAPTIVE_ENTITY_MIN:   int   = 6      # entity-overlap count that licenses that floor

# ── 2-D threshold-sweep grid (measurement only, behind --sweep) ───────────────
# A read-only diagnostic that re-runs the same-event grouping across a grid of
# (cosine threshold T × entity-overlap minimum E) to empirically justify the
# production gate (0.82 / 2) and to expose exactly where lowering E to 1 starts
# introducing false merges (precision < 1.0). Each cell evaluates the STANDARD
# gate in isolation (merge ⇔ cosine >= T AND overlap >= E, adaptive branch
# disabled), so it measures the pure effect of that (T, E) pair. The sweep mutates
# NO production constant — (T, E) are passed as arguments to a parameterised local
# clusterer (``_sweep_cluster``); ``_SIMILARITY_THRESHOLD`` / ``_ENTITY_OVERLAP_MIN``
# are read for the reference row only, never reassigned. Cache-only, zero API.
_SWEEP_COSINE_GRID: list[float] = [0.70, 0.78, 0.80, 0.82, 0.84, 0.88]
_SWEEP_ENTITY_GRID: list[int] = [1, 2, 3]


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_date(value: str | None) -> datetime | None:
    """Parse the dataset's ``publication_date`` to a tz-aware datetime, or None."""
    if not value:
        return None
    raw = str(value).strip()
    for parser in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
        lambda s: datetime.strptime(s, "%Y/%m/%d"),
    ):
        try:
            dt = parser(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ── Preprocessing (embeddings + entities) with local cache ───────────────────

def _load_cache() -> dict[str, Any]:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read preprocess cache — recomputing.")
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    """Persist the preprocess cache atomically (temp file in same dir + os.replace).

    The atomic write guarantees that an interruption (including a hard kill)
    mid-write can never leave a half-written / corrupt cache file: the previous
    good cache stays in place until the fully-written replacement is swapped in.
    Called incrementally (once per newly-completed article) so already-fetched
    embeddings/entities survive a quota stop and a later run resumes from them.
    """
    try:
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(_RESULTS_DIR), prefix=".preprocess_cache.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False)
            os.replace(tmp_path, _CACHE_PATH)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except OSError as e:
        logger.warning("Could not write preprocess cache: %s", e)


class _PoolExhausted(Exception):
    """Raised when ``agents.llm_client`` reports the whole Gemini key pool is down.

    The shared ``GeminiKeyPool`` handles per-key quota rotation and 1-hour
    quarantine internally; it only surfaces an error to this harness when it
    *cannot proceed at all* — i.e. every configured key is quarantined/tried.
    We translate that specific condition into this exception so ``preprocess``
    can stop cleanly (preserving the cache) instead of looping the remaining
    articles into guaranteed failures and overwriting the results file.
    """

    def __init__(self, where: str, detail: str) -> None:
        super().__init__(f"{where}: {detail}")
        self.where = where
        self.detail = detail


def _is_pool_exhausted(error_msg: str | None) -> bool:
    """True only when an ``llm_client`` error means *all* keys are exhausted.

    Distinguishes the pool-level "no keys left" condition from an ordinary
    single-call error. A lone quota error on one key never reaches the harness —
    the pool rotates past it; only when the rotation is fully spent does
    ``gemini_embed`` / ``gemini_generate_with_fallback`` return one of these
    signatures. Transient (503/500) and permanent (auth/invalid) single-call
    errors are deliberately NOT treated as pool exhaustion: they return None and
    the run continues, mirroring the pool's own quota-vs-transient-vs-permanent
    distinction (``llm_client._is_quota_error`` / ``_is_transient_server_error``).
    """
    msg = (error_msg or "").lower()
    # Direct all-keys-down signatures emitted by agents.llm_client:
    #   gemini_generate_with_fallback → "all Gemini keys are quarantined", "pool exhausted"
    #   gemini_embed                  → "no keys available", "all quarantined"
    pool_down = (
        "all gemini keys are quarantined",
        "pool exhausted",
        "no keys available",
        "all quarantined",
    )
    if any(sig in msg for sig in pool_down):
        return True
    # Fallback chain bottomed out specifically on quota (every available key hit
    # 429 for the only entity model) — also a quota wall. A chain exhausted on a
    # *transient* error is NOT quota and must not stop the run, so require a quota
    # token alongside the chain-exhausted marker (mirrors _is_quota_error tokens).
    if "chain exhausted" in msg and any(
        tok in msg
        for tok in ("quota", "resource_exhausted", "resource exhausted", "429",
                    "rate limit", "rate_limit", "too many requests")
    ):
        return True
    return False


async def _embed(text: str) -> list[float] | None:
    """Embed *text* via the production embedding path.

    Returns None on an ordinary (single-call) error; raises ``_PoolExhausted``
    when the shared key pool reports all keys are exhausted/quarantined so the
    caller can stop cleanly. Never raises for a recoverable error (Rule 2.4).
    """
    result = await llm_client.gemini_embed(text)
    if "error" in result:
        err = result["error"]
        if _is_pool_exhausted(err):
            raise _PoolExhausted("embedding", err)
        logger.warning("  embedding error: %s", err)
        return None
    emb = result.get("embedding")
    return emb if isinstance(emb, list) and emb else None


def _parse_entities_payload(text: str) -> dict[str, Any] | None:
    """Parse the entity model's text output into a JSON object, salvaging if needed.

    Tries the production parse first (``_strip_code_fences`` + ``json.loads``). If
    that fails because the model wrapped the JSON in prose or appended trailing
    text, it salvages the substring from the first ``{`` to the last ``}`` and
    parses that. Returns ``None`` when no JSON object can be recovered or when the
    recovered object is empty (an empty entity set is useless for Condition 3 and
    is treated as a parse failure so the caller can retry). This touches only the
    parsing of the model's string output — not the prompt, model, the
    ``_normalise_entities`` shape, or any clustering threshold.
    """
    raw = _strip_code_fences(text or "")
    if not raw:
        return None

    candidates: list[str] = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = raw[start:end + 1]
        if snippet != raw:
            candidates.append(snippet)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and any(
            parsed.get(k) for k in ("people", "locations", "organizations")
        ):
            return parsed
    return None


async def _extract_entities(text: str) -> dict[str, list[str]] | None:
    """Extract entities via the production prompt+path, with robust parsing.

    Issues the production entity call (same prompt, same ``entities`` task tier,
    same model) and parses the result through ``_parse_entities_payload``. If the
    output cannot be parsed into a non-empty entity object, the call is retried up
    to ``_ENTITY_PARSE_ATTEMPTS`` times (a fresh generation often parses cleanly).

    Returns None when all attempts fail to yield parseable entities; raises
    ``_PoolExhausted`` when the shared key pool reports all keys are
    exhausted/quarantined so the caller can stop cleanly. Never raises for a
    recoverable error (Rule 2.4).
    """
    prompt = _ENTITY_PROMPT_TEMPLATE.format(text=text)

    for attempt in range(1, _ENTITY_PARSE_ATTEMPTS + 1):
        gen = await llm_client.gemini_generate_with_fallback(
            prompt=prompt, task_type="entities",
            max_tokens=_ENTITIES_MAX_TOKENS, temperature=0.0,
        )
        if "error" in gen:
            err = gen["error"]
            if _is_pool_exhausted(err):
                raise _PoolExhausted("entities", err)
            logger.warning("  entity error: %s", err)
            return None

        raw_text = gen.get("text", "") or ""
        parsed = _parse_entities_payload(raw_text)
        if parsed is not None:
            return _normalise_entities(parsed)

        # Diagnostic: show what the model actually returned so a parse failure is
        # explainable (empty text vs. truncated JSON vs. non-JSON prose). Logs a
        # bounded snippet only; the payload is entity text, not secrets.
        snippet = raw_text.replace("\n", "\\n")[:600]
        logger.warning(
            "  entity parse failed (attempt %d/%d) — raw len=%d, model=%s, snippet=%r",
            attempt, _ENTITY_PARSE_ATTEMPTS, len(raw_text),
            gen.get("model_used", "?"), snippet,
        )

    return None


async def preprocess(
    articles: list[dict[str, Any]], use_cache: bool,
) -> tuple[dict[int, dict[str, Any]], bool]:
    """Compute embedding + entities for every article (cached by content hash).

    Returns ``(prepared, stopped_early)``. ``stopped_early`` is True iff the
    shared Gemini key pool reported all keys exhausted/quarantined: in that case
    preprocessing halts at that article, the cache (already saved incrementally)
    is preserved, and the caller must NOT overwrite clustering_results.json with
    the partial (degraded) result. A cached article consumes no key/quota, so a
    refreshed-quota rerun resumes and only re-spends quota on the remainder.
    """
    cache = _load_cache() if use_cache else {}
    out: dict[int, dict[str, Any]] = {}
    stopped_early = False

    for i, article in enumerate(articles, 1):
        aid = int(article["id"])
        text = (article.get("content") or article.get("title") or "")[:_PREPROCESS_CHARS]
        key = _md5(text)
        cached = cache.get(key) if use_cache else None

        if cached and cached.get("embedding") and cached.get("entities") is not None:
            embedding = cached["embedding"]
            entities = cached["entities"]
            logger.info("  [%d/%d] id=%s (cache)", i, len(articles), aid)
        else:
            logger.info("  [%d/%d] id=%s embedding+entities ...", i, len(articles), aid)
            try:
                embedding = await _embed(text)
                entities = await _extract_entities(text)
            except _PoolExhausted as exc:
                logger.error(
                    "Gemini key pool exhausted during %s call (%s). Stopping: "
                    "%d/%d articles completed; preprocess cache preserved for resume.",
                    exc.where, exc.detail, len(out), len(articles),
                )
                stopped_early = True
                break
            if use_cache and embedding is not None and entities is not None:
                # Incremental atomic save: persist this article immediately so a
                # later quota stop (or hard kill) never loses completed work.
                cache[key] = {"embedding": embedding, "entities": entities}
                _save_cache(cache)

        out[aid] = {
            "id": aid,
            "section": article.get("section"),
            "title": article.get("title", ""),
            "published_at": _parse_date(article.get("publication_date")),
            "embedding": embedding,
            "entities": entities or {"people": [], "locations": [], "organizations": []},
            "entities_ok": entities is not None,
            "human_label": article.get("human_label"),
        }

    return out, stopped_early


def preprocess_from_cache_only(
    articles: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Build the prepared dict from the preprocess cache ONLY — never calls the API.

    Returns ``(prepared, missing)`` where ``missing`` lists the dataset ids whose
    embedding/entities are absent from the cache. This is the load path used by the
    ``--sweep`` diagnostic: it guarantees **zero** Gemini/API calls (and consumes no
    quota) because, unlike ``preprocess``, it never invokes ``_embed`` /
    ``_extract_entities`` — a cache miss is recorded in ``missing`` so the caller can
    report and stop rather than fetch. Read-only; touches no production code.
    """
    cache = _load_cache()
    prepared: dict[int, dict[str, Any]] = {}
    missing: list[int] = []

    for article in articles:
        aid = int(article["id"])
        text = (article.get("content") or article.get("title") or "")[:_PREPROCESS_CHARS]
        cached = cache.get(_md5(text))
        if not (cached and cached.get("embedding") and cached.get("entities") is not None):
            missing.append(aid)
            continue
        prepared[aid] = {
            "id": aid,
            "section": article.get("section"),
            "title": article.get("title", ""),
            "published_at": _parse_date(article.get("publication_date")),
            "embedding": cached["embedding"],
            "entities": cached["entities"],
            "entities_ok": True,
            "human_label": article.get("human_label"),
        }
    return prepared, missing


# ── Clustering (reproduces ClusteringAgent.run orchestration in-memory) ───────

def _cosine(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _within_window(a: dict[str, Any], b: dict[str, Any]) -> bool:
    pa, pb = a.get("published_at"), b.get("published_at")
    if pa is None or pb is None:
        return False
    return abs((pa - pb).total_seconds()) <= _WINDOW_SECONDS


def _pair_merges(a: dict[str, Any], b: dict[str, Any], adaptive: bool) -> bool:
    """Whether two articles satisfy the merge conditions.

    Baseline (production-faithful): same section AND within the 72h window AND
    cosine >= ``_SIMILARITY_THRESHOLD`` AND entity_overlap >= ``_ENTITY_OVERLAP_MIN``.
    When *adaptive* is True, a strong entity signal additionally licenses a slightly
    lower cosine floor: cosine >= ``_ADAPTIVE_COSINE_FLOOR`` AND entity_overlap >=
    ``_ADAPTIVE_ENTITY_MIN``. The adaptive branch is an evaluation experiment only;
    it does not change any production threshold or code.
    """
    if not a.get("embedding") or not b.get("embedding"):
        return False
    if a.get("published_at") is None or b.get("published_at") is None:
        return False
    if a.get("section") != b.get("section"):
        return False
    if not _within_window(a, b):
        return False
    sim = _cosine(a["embedding"], b["embedding"])
    ov = _entity_overlap(a.get("entities") or {}, b.get("entities") or {})
    if sim >= _SIMILARITY_THRESHOLD and ov >= _ENTITY_OVERLAP_MIN:
        return True
    if adaptive and sim >= _ADAPTIVE_COSINE_FLOOR and ov >= _ADAPTIVE_ENTITY_MIN:
        return True
    return False


def _cluster_greedy(
    prepared: dict[int, dict[str, Any]], order: list[int], adaptive: bool,
) -> list[set[int]]:
    """Production-faithful greedy, in-order, claim-once clustering.

    With ``adaptive=False`` this is byte-for-byte the original
    ``ClusteringAgent.run()`` reproduction: cosine >= 0.82 candidate filter, cap at
    ``_FIND_SIMILAR_LIMIT`` by similarity, then the entity-overlap >= 2 gate. With
    ``adaptive=True`` the candidate floor drops to ``_ADAPTIVE_COSINE_FLOOR`` and a
    pair may also merge via the strong-entity branch of ``_pair_merges``.
    """
    assigned: set[int] = set()
    clusters: list[set[int]] = []
    cosine_floor = _ADAPTIVE_COSINE_FLOOR if adaptive else _SIMILARITY_THRESHOLD

    for aid in order:
        if aid in assigned:
            continue
        a = prepared.get(aid)
        if a is None or not a.get("embedding") or a.get("published_at") is None:
            continue

        # Conditions 1 & 2 (section + 72h window + cosine >= floor), mirroring
        # the find_similar SQL filters, computed in-memory over the eval pool.
        candidates: list[tuple[int, float]] = []
        for bid, b in prepared.items():
            if bid == aid or bid in assigned:
                continue
            if not b.get("embedding") or b.get("published_at") is None:
                continue
            if b.get("section") != a.get("section"):
                continue
            if not _within_window(a, b):
                continue
            sim = _cosine(a["embedding"], b["embedding"])
            if sim < cosine_floor:
                continue
            candidates.append((bid, sim))

        # Order by descending similarity and cap at the production limit.
        candidates.sort(key=lambda x: -x[1])
        candidates = candidates[:_FIND_SIMILAR_LIMIT]

        # Condition 3 — entity overlap (production _entity_overlap), with the
        # optional adaptive strong-entity branch.
        members: set[int] = {aid}
        for bid, sim in candidates:
            ov = _entity_overlap(a.get("entities") or {}, prepared[bid].get("entities") or {})
            base = sim >= _SIMILARITY_THRESHOLD and ov >= _ENTITY_OVERLAP_MIN
            adpt = adaptive and sim >= _ADAPTIVE_COSINE_FLOOR and ov >= _ADAPTIVE_ENTITY_MIN
            if base or adpt:
                members.add(bid)

        if len(members) >= 2:
            clusters.append(members)
            assigned.update(members)

    return clusters


def _cluster_components(
    prepared: dict[int, dict[str, Any]], adaptive: bool,
) -> list[set[int]]:
    """Order-independent clustering via connected components over qualifying pairs.

    Builds an undirected graph whose edges are article pairs that satisfy
    ``_pair_merges`` and returns its connected components of size >= 2. Unlike the
    greedy policy, an article is never "claimed" out of reach: if A↔B and B↔C both
    qualify, A, B and C land in one cluster even when A↔C does not directly qualify.
    This removes the in-order, claim-once artefact; it is an evaluation experiment
    and does not alter production logic.
    """
    ids = [
        aid for aid, a in prepared.items()
        if a.get("embedding") and a.get("published_at") is not None
    ]
    parent: dict[int, int] = {aid: aid for aid in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _pair_merges(prepared[ids[i]], prepared[ids[j]], adaptive):
                union(ids[i], ids[j])

    components: dict[int, set[int]] = {}
    for aid in ids:
        components.setdefault(find(aid), set()).add(aid)
    return [members for members in components.values() if len(members) >= 2]


def cluster(
    prepared: dict[int, dict[str, Any]],
    order: list[int],
    strategy: str = "greedy",
    adaptive: bool = False,
) -> dict[int, str]:
    """Cluster articles into events and return ``{article_id: cluster_label}``.

    Defaults (``strategy="greedy"``, ``adaptive=False``) reproduce
    ``ClusteringAgent.run()`` exactly. ``strategy="components"`` and/or
    ``adaptive=True`` enable evaluation-only experiments (see the respective
    helpers). Multi-article clusters get labels ``cluster_1, cluster_2, ...``;
    unclustered articles get ``singleton_<id>``.
    """
    if strategy == "components":
        clusters = _cluster_components(prepared, adaptive)
    else:
        clusters = _cluster_greedy(prepared, order, adaptive)

    assignment: dict[int, str] = {}
    for idx, members in enumerate(clusters, 1):
        for m in members:
            assignment[m] = f"cluster_{idx}"
    for aid in prepared:
        assignment.setdefault(aid, f"singleton_{aid}")
    return assignment


def _sweep_cluster(
    prepared: dict[int, dict[str, Any]],
    order: list[int],
    cos_t: float,
    ent_min: int,
    skip_window: bool = False,
    use_components: bool = False,
) -> dict[int, str]:
    """Greedy (or components) clustering with a PARAMETERISED standard gate.

    Used exclusively by the ``--sweep`` diagnostic path; never called by the
    default run. Parameters:

    * ``cos_t`` / ``ent_min`` — the cosine floor and entity-overlap minimum for
      this grid cell. Neither production constant is read or mutated.
    * ``skip_window`` — when True, the 72-hour publication-date check is omitted,
      revealing the false-merge risk of the cosine/entity thresholds alone.
    * ``use_components`` — when True, uses order-independent connected components
      (union-find) instead of the greedy in-order assignment, removing the claim-
      once ordering artefact from the measurement.

    Combining ``skip_window=True`` and ``use_components=True`` gives the purest
    view: only the cosine and entity thresholds determine which pairs merge,
    with no help from temporal or ordering constraints.

    Returns ``{article_id: cluster_label}`` in the same shape as ``cluster()``
    so the existing ``score_groups`` / ``pairwise_metrics`` consume it unchanged.
    """
    if use_components:
        # ── Connected-components path (order-independent) ─────────────────────
        ids = [
            aid for aid, a in prepared.items()
            if a.get("embedding") and (skip_window or a.get("published_at") is not None)
        ]
        parent: dict[int, int] = {aid: aid for aid in ids}

        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(x: int, y: int) -> None:
            rx, ry = _find(x), _find(y)
            if rx != ry:
                parent[rx] = ry

        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = prepared[ids[i]], prepared[ids[j]]
                if a.get("section") != b.get("section"):
                    continue
                if not skip_window and not _within_window(a, b):
                    continue
                sim = _cosine(a["embedding"], b["embedding"])
                if sim < cos_t:
                    continue
                ov = _entity_overlap(a.get("entities") or {}, b.get("entities") or {})
                if ov >= ent_min:
                    _union(ids[i], ids[j])

        components: dict[int, set[int]] = {}
        for aid in ids:
            components.setdefault(_find(aid), set()).add(aid)
        clusters = [members for members in components.values() if len(members) >= 2]

    else:
        # ── Greedy in-order path (production-faithful orchestration) ──────────
        assigned: set[int] = set()
        clusters = []

        for aid in order:
            if aid in assigned:
                continue
            a = prepared.get(aid)
            if a is None or not a.get("embedding"):
                continue
            if not skip_window and a.get("published_at") is None:
                continue

            candidates: list[tuple[int, float]] = []
            for bid, b in prepared.items():
                if bid == aid or bid in assigned:
                    continue
                if not b.get("embedding"):
                    continue
                if not skip_window and b.get("published_at") is None:
                    continue
                if b.get("section") != a.get("section"):
                    continue
                if not skip_window and not _within_window(a, b):
                    continue
                sim = _cosine(a["embedding"], b["embedding"])
                if sim < cos_t:
                    continue
                candidates.append((bid, sim))

            candidates.sort(key=lambda x: -x[1])
            candidates = candidates[:_FIND_SIMILAR_LIMIT]

            members: set[int] = {aid}
            for bid, sim in candidates:
                ov = _entity_overlap(
                    a.get("entities") or {}, prepared[bid].get("entities") or {}
                )
                if sim >= cos_t and ov >= ent_min:
                    members.add(bid)

            if len(members) >= 2:
                clusters.append(members)
                assigned.update(members)

    assignment: dict[int, str] = {}
    for idx, members in enumerate(clusters, 1):
        for m in members:
            assignment[m] = f"cluster_{idx}"
    for aid in prepared:
        assignment.setdefault(aid, f"singleton_{aid}")
    return assignment


# ── Scoring against ground truth ──────────────────────────────────────────────

def score_groups(
    groups: list[dict[str, Any]],
    assignment: dict[int, str],
) -> list[dict[str, Any]]:
    """Score each ground-truth group: grouped / partial / split."""
    results: list[dict[str, Any]] = []
    for g in groups:
        gid = g["group_id"]
        ids = g["article_ids"]
        present = [i for i in ids if i in assignment]
        missing = [i for i in ids if i not in assignment]

        clusters_of = {i: assignment[i] for i in present}
        distinct = set(clusters_of.values())
        # A real (multi-article) cluster id is shared; singleton ids are unique.
        is_real = {c for c in distinct if not c.startswith("singleton_")}

        if missing or not present:
            status = "split"
        elif len(distinct) == 1 and is_real:
            status = "grouped"
        elif len(distinct) == len(present):
            status = "split"
        else:
            status = "partial"

        # Identify separated members (those not in the largest shared cluster).
        cluster_counts: dict[str, list[int]] = {}
        for i, c in clusters_of.items():
            cluster_counts.setdefault(c, []).append(i)
        largest = max(cluster_counts.values(), key=len) if cluster_counts else []
        separated = sorted(i for i in present if i not in largest) if status != "grouped" else []

        results.append({
            "group_id": gid,
            "article_ids": ids,
            "labels": g.get("labels"),
            "title_similarity": g.get("title_similarity"),
            "event_topic": g.get("event_topic"),
            "is_hard_case": gid in _HARD_GROUPS,
            "status": status,
            "cluster_assignment": clusters_of,
            "separated_members": separated,
            "missing_from_dataset": missing,
        })
    return results


def pairwise_metrics(
    groups: list[dict[str, Any]],
    assignment: dict[int, str],
) -> dict[str, Any]:
    """Pairwise precision/recall/F1 on same-event pairs over grouped articles."""
    grouped_ids = sorted({i for g in groups for i in g["article_ids"] if i in assignment})

    # Ground-truth same-event pairs.
    gt_pairs: set[tuple[int, int]] = set()
    for g in groups:
        ids = [i for i in g["article_ids"] if i in assignment]
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                gt_pairs.add(tuple(sorted((ids[x], ids[y]))))

    # Predicted same-cluster pairs, restricted to the grouped article set.
    pred_pairs: set[tuple[int, int]] = set()
    for x in range(len(grouped_ids)):
        for y in range(x + 1, len(grouped_ids)):
            a, b = grouped_ids[x], grouped_ids[y]
            ca, cb = assignment[a], assignment[b]
            if ca == cb and not ca.startswith("singleton_"):
                pred_pairs.add((a, b))

    tp = len(gt_pairs & pred_pairs)
    fp = len(pred_pairs - gt_pairs)
    fn = len(gt_pairs - pred_pairs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "articles_considered": len(grouped_ids),
        "true_positive_pairs": tp,
        "false_positive_pairs": fp,
        "false_negative_pairs": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


# ── Pair-level gate diagnostics ───────────────────────────────────────────────

def diagnose_groups(
    groups: list[dict[str, Any]],
    prepared: dict[int, dict[str, Any]],
    assignment: dict[int, str],
) -> list[dict[str, Any]]:
    """Explain, per ground-truth pair, exactly which clustering gate passed/failed.

    For every unordered pair of articles inside each ground-truth group this
    records the raw values the decision rests on — cosine similarity, the count
    of shared (normalised) entities, the same-section flag, and the within-72h
    flag — plus which of the three conditions each pair clears. This is what the
    headline metrics cannot show: *why* a same-event pair did or did not merge
    (cosine just under 0.82 vs. fewer than 2 shared entities vs. a section/time
    mismatch vs. a missing embedding/entity).

    Read-only and side-effect-free: it reuses the already-computed embeddings and
    entities in *prepared*, calls the same ``_cosine`` / ``_entity_overlap`` /
    ``_within_window`` functions and the same ``_SIMILARITY_THRESHOLD`` /
    ``_ENTITY_OVERLAP_MIN`` constants the clustering uses, makes **no** API call,
    and does **not** alter the clustering decision logic or the metrics.

    ``would_merge`` reflects the three gates evaluated on the pair in isolation.
    ``same_cluster_predicted`` reflects the actual greedy assignment outcome — a
    pair can pass every gate yet still be apart if one member was claimed by an
    earlier cluster first, so the two fields are reported separately.
    """
    diagnostics: list[dict[str, Any]] = []
    for g in groups:
        gid = g["group_id"]
        ids = [i for i in g["article_ids"] if i in prepared]
        pairs: list[dict[str, Any]] = []

        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a_id, b_id = ids[x], ids[y]
                a, b = prepared[a_id], prepared[b_id]
                a_emb, b_emb = a.get("embedding"), b.get("embedding")
                has_emb = bool(a_emb) and bool(b_emb)
                sim = _cosine(a_emb, b_emb) if a_emb and b_emb else None
                overlap = _entity_overlap(a.get("entities") or {}, b.get("entities") or {})
                entities_ok = bool(a.get("entities_ok", True)) and bool(b.get("entities_ok", True))
                same_section = a.get("section") == b.get("section")
                in_window = _within_window(a, b)

                cond2_pass = bool(has_emb and sim is not None and sim >= _SIMILARITY_THRESHOLD)
                cond3_pass = overlap >= _ENTITY_OVERLAP_MIN

                # Ordered list of the gate(s) that block this pair from merging.
                failed_gates: list[str] = []
                if not same_section:
                    failed_gates.append("section_mismatch")
                if not in_window:
                    failed_gates.append("outside_72h_window")
                if not has_emb:
                    failed_gates.append("missing_embedding")
                elif not cond2_pass:
                    failed_gates.append("cosine_below_threshold")
                if not entities_ok:
                    failed_gates.append("entities_extraction_failed")
                elif not cond3_pass:
                    failed_gates.append("entity_overlap_below_min")

                would_merge = same_section and in_window and cond2_pass and cond3_pass
                ca, cb = assignment.get(a_id), assignment.get(b_id)
                same_cluster_predicted = bool(
                    ca is not None and ca == cb and not str(ca).startswith("singleton_")
                )

                pairs.append({
                    "pair": [a_id, b_id],
                    "cosine": round(sim, 4) if sim is not None else None,
                    "cosine_pass": cond2_pass,
                    "shared_entities": overlap,
                    "entity_overlap_pass": cond3_pass,
                    "same_section": same_section,
                    "within_72h": in_window,
                    "would_merge": would_merge,
                    "same_cluster_predicted": same_cluster_predicted,
                    "failed_gates": failed_gates,
                })

        diagnostics.append({
            "group_id": gid,
            "is_hard_case": gid in _HARD_GROUPS,
            "title_similarity": g.get("title_similarity"),
            "pairs": pairs,
        })
    return diagnostics


# ── Output ────────────────────────────────────────────────────────────────────

def print_summary(per_group: list[dict[str, Any]], overall: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print("CLUSTERING EVALUATION — same-event grouping")
    print("=" * 72)
    print(f"{'Grp':<4} {'Hard':<5} {'TitleSim':<11} {'Status':<9} Members")
    print("-" * 72)
    for r in per_group:
        hard = "yes" if r["is_hard_case"] else ""
        print(f"{r['group_id']:<4} {hard:<5} {str(r['title_similarity']):<11} "
              f"{r['status']:<9} {r['article_ids']}")
    print("-" * 72)
    print(f"Groups correctly clustered: {overall['groups_correct']}/{overall['groups_total']}")
    hg = overall["hard_groups"]
    print(f"Hard groups (4,6,7) correct: {hg['correct']}/{hg['total']}  -> {hg['detail']}")
    pw = overall["pairwise"]
    print(f"Pairwise (over {pw['articles_considered']} grouped articles): "
          f"P={pw['precision']:.2f} R={pw['recall']:.2f} F1={pw['f1']:.2f} "
          f"(TP={pw['true_positive_pairs']} FP={pw['false_positive_pairs']} FN={pw['false_negative_pairs']})")
    print("=" * 72)


def print_diagnostics(diagnostics: list[dict[str, Any]]) -> None:
    """Print the per-pair gate values so missed merges are explainable at a glance.

    cos column shows cosine vs. the 0.82 floor; ent shows shared-entity count vs.
    the 2 minimum; sec/72h are the section and time gates; merge is the gate-only
    verdict; the trailing text lists the gate(s) that blocked a non-merging pair.
    """
    print("\n" + "=" * 72)
    print(f"PAIR-LEVEL DIAGNOSTICS (threshold: cos>={_SIMILARITY_THRESHOLD}, "
          f"shared entities>={_ENTITY_OVERLAP_MIN})")
    print("=" * 72)
    print(f"{'Grp':<4} {'Pair':<11} {'cos':<7} {'cos>=':<6} {'ent':<4} {'ent>=':<6} "
          f"{'sec':<4} {'72h':<4} {'merge':<6} reason")
    print("-" * 72)
    for d in diagnostics:
        gid = d["group_id"]
        for p in d["pairs"]:
            cos = f"{p['cosine']:.3f}" if p["cosine"] is not None else "n/a"
            reason = "" if p["would_merge"] else ", ".join(p["failed_gates"])
            print(
                f"{gid:<4} {str(tuple(p['pair'])):<11} "
                f"{cos:<7} {('yes' if p['cosine_pass'] else 'no'):<6} "
                f"{p['shared_entities']:<4} {('yes' if p['entity_overlap_pass'] else 'no'):<6} "
                f"{('yes' if p['same_section'] else 'no'):<4} "
                f"{('yes' if p['within_72h'] else 'no'):<4} "
                f"{('yes' if p['would_merge'] else 'NO'):<6} {reason}"
            )
    print("=" * 72)


# ── 2-D threshold sweep (cosine × entity-min) — measurement only ──────────────

def _metrics_for_assignment(
    groups: list[dict[str, Any]], assignment: dict[int, str],
) -> dict[str, Any]:
    """Compute the full metric set for one assignment via the existing scorers.

    Reuses ``score_groups`` and ``pairwise_metrics`` verbatim so a swept cell is
    scored identically to the normal run. No API call, no logic change.
    """
    per_group = score_groups(groups, assignment)
    groups_correct = sum(1 for r in per_group if r["status"] == "grouped")
    hard = [r for r in per_group if r["is_hard_case"]]
    hard_correct = sum(1 for r in hard if r["status"] == "grouped")
    return {
        "groups_correct": groups_correct,
        "groups_total": len(groups),
        "hard_correct": hard_correct,
        "hard_total": len(hard),
        "pairwise": pairwise_metrics(groups, assignment),
    }


def _print_sweep(
    cells: list[dict[str, Any]],
    reference: dict[str, Any],
    title: str = "THRESHOLD SWEEP — standard gate in isolation (cosine T × entity-min E)",
    window_note: str = "72h window: ON",
) -> None:
    """Print the T×E grid (F1 + precision per cell), flagging precision < 1.0.

    A ``*`` marks any cell whose pairwise precision dropped below 1.0 (i.e. the
    relaxation introduced a false merge among the ground-truth articles) — the key
    decision signal, most visible in the entity-min = 1 column. A detailed table
    with the full per-cell metric set and the production-rule reference row follow.
    """
    by = {(c["cosine_threshold"], c["entity_min"]): c for c in cells}

    print("\n" + "=" * 72)
    print(title)
    print(f"[{window_note}]  per cell: F1 (P=precision);  * = precision < 1.0 (false merge)")
    print("=" * 72)
    header = f"{'cos | ent':<10}" + "".join(f"E={e:<14}" for e in _SWEEP_ENTITY_GRID)
    print(header)
    print("-" * 72)
    for t in _SWEEP_COSINE_GRID:
        row = f"{t:<10.2f}"
        for e in _SWEEP_ENTITY_GRID:
            pw = by[(t, e)]["pairwise"]
            flag = "*" if pw["precision"] < 1.0 else " "
            cell = f"F1={pw['f1']:.3f} P={pw['precision']:.2f}{flag}"
            row += f"{cell:<16}"
        print(row)
    print("-" * 72)

    # Detailed per-cell metrics.
    print(f"\n{'cosT':<6}{'entE':<6}{'grp':<6}{'hard':<6}"
          f"{'TP':<5}{'FP':<5}{'FN':<5}{'P':<8}{'R':<8}{'F1':<8}")
    print("-" * 72)
    for t in _SWEEP_COSINE_GRID:
        for e in _SWEEP_ENTITY_GRID:
            c = by[(t, e)]
            pw = c["pairwise"]
            flag = " *FALSE-MERGE" if pw["precision"] < 1.0 else ""
            print(f"{t:<6.2f}{e:<6}"
                  f"{str(c['groups_correct'])+'/'+str(c['groups_total']):<6}"
                  f"{str(c['hard_correct'])+'/'+str(c['hard_total']):<6}"
                  f"{pw['true_positive_pairs']:<5}{pw['false_positive_pairs']:<5}"
                  f"{pw['false_negative_pairs']:<5}"
                  f"{pw['precision']:<8.4f}{pw['recall']:<8.4f}{pw['f1']:<8.4f}{flag}")
    print("-" * 72)

    rpw = reference["pairwise"]
    print(f"PRODUCTION RULE (reference): {reference['gate']}")
    print(f"  groups {reference['groups_correct']}/{reference['groups_total']}, "
          f"hard {reference['hard_correct']}/{reference['hard_total']}, "
          f"P={rpw['precision']:.4f} R={rpw['recall']:.4f} F1={rpw['f1']:.4f} "
          f"(TP={rpw['true_positive_pairs']} FP={rpw['false_positive_pairs']} "
          f"FN={rpw['false_negative_pairs']})")

    # Best F1 among cells that keep perfect precision.
    clean = [c for c in cells if c["pairwise"]["precision"] >= 1.0]
    if clean:
        best = max(clean, key=lambda c: c["pairwise"]["f1"])
        print(f"Best F1 with precision=1.0: F1={best['pairwise']['f1']:.4f} at "
              f"cosine>={best['cosine_threshold']}, entity-min>={best['entity_min']}")
    dropped = [c for c in cells if c["pairwise"]["precision"] < 1.0]
    if dropped:
        cells_str = ", ".join(
            f"(cos={c['cosine_threshold']}, ent={c['entity_min']}: "
            f"FP={c['pairwise']['false_positive_pairs']} P={c['pairwise']['precision']:.2f})"
            for c in dropped
        )
        print(f"Cells with precision < 1.0 (false merges introduced): {cells_str}")
    else:
        print("No cell dropped below precision 1.0 on this grid.")
    print("=" * 72)


def run_sweep(
    articles: list[dict[str, Any]], groups: list[dict[str, Any]],
) -> int:
    """Run the read-only 2-D threshold sweep from cache. Returns a process exit code.

    Cache-only (zero API calls): if any article is missing from the preprocess
    cache it reports the ids and returns 2 without fetching. Otherwise it evaluates
    every ``(cosine, entity-min)`` grid cell with the parameterised standard gate,
    plus one production-rule reference row, prints the comparison, and writes
    ``clustering_sweep.json``. It never writes ``clustering_results.json`` and
    changes no production constant.
    """
    logger.info(
        "Sweep mode: loading %d articles from preprocess cache only (no API calls) ...",
        len(articles),
    )
    prepared, missing = preprocess_from_cache_only(articles)
    if missing:
        logger.error(
            "Preprocess cache is incomplete: %d/%d articles missing (ids: %s). "
            "The sweep makes ZERO API calls — run the normal harness first to "
            "populate the cache, then re-run --sweep. Nothing was written.",
            len(missing), len(articles), missing,
        )
        return 2

    order = [int(a["id"]) for a in articles]
    n_ready = sum(1 for a in prepared.values() if a.get("embedding") and a.get("published_at"))
    n_cells = len(_SWEEP_COSINE_GRID) * len(_SWEEP_ENTITY_GRID)
    logger.info(
        "Sweeping %d cells × 2 passes (with/without 72h window) over %d articles ...",
        n_cells, n_ready,
    )

    # ── Pass 1: standard sweep WITH the 72h time window (production-faithful) ──
    cells_with_window: list[dict[str, Any]] = []
    for t in _SWEEP_COSINE_GRID:
        for e in _SWEEP_ENTITY_GRID:
            assignment = _sweep_cluster(prepared, order, t, e, skip_window=False)
            cells_with_window.append({
                "cosine_threshold": t,
                "entity_min": e,
                "gate": "standard_isolated_with_window",
                **_metrics_for_assignment(groups, assignment),
            })

    # ── Pass 2: same grid WITHOUT the 72h time window (greedy) ───────────────
    # Reveals the true false-merge risk of the cosine/entity thresholds alone,
    # removing the structural protection the time window provides on this dataset
    # (where no two ground-truth groups share both section AND 72h window, making
    # FP structurally impossible in Pass 1 regardless of threshold).
    cells_no_window: list[dict[str, Any]] = []
    for t in _SWEEP_COSINE_GRID:
        for e in _SWEEP_ENTITY_GRID:
            assignment = _sweep_cluster(prepared, order, t, e, skip_window=True)
            cells_no_window.append({
                "cosine_threshold": t,
                "entity_min": e,
                "gate": "standard_isolated_no_window_greedy",
                **_metrics_for_assignment(groups, assignment),
            })

    # ── Pass 3: WITHOUT 72h window AND using connected components ─────────────
    # Removes BOTH the time-window protection AND the greedy ordering artefact.
    # Connected components (union-find) is order-independent: A merging with B and
    # B merging with C always puts A, B and C in one cluster, regardless of which
    # article was processed first. Combined with no time filter, this gives the
    # purest measurement: only the (cosine T, entity E) thresholds determine merges.
    cells_components_no_window: list[dict[str, Any]] = []
    for t in _SWEEP_COSINE_GRID:
        for e in _SWEEP_ENTITY_GRID:
            assignment = _sweep_cluster(
                prepared, order, t, e, skip_window=True, use_components=True,
            )
            cells_components_no_window.append({
                "cosine_threshold": t,
                "entity_min": e,
                "gate": "standard_isolated_no_window_components",
                **_metrics_for_assignment(groups, assignment),
            })

    # Production-rule reference (with window, as production runs).
    ref_assignment = cluster(prepared, order, strategy="greedy", adaptive=True)
    reference = {
        "cosine_threshold": _SIMILARITY_THRESHOLD,
        "entity_min": _ENTITY_OVERLAP_MIN,
        "gate": (
            f"standard (cos>={_SIMILARITY_THRESHOLD} AND ent>={_ENTITY_OVERLAP_MIN}) "
            f"OR adaptive (cos>={_ADAPTIVE_COSINE_FLOOR} AND ent>={_ADAPTIVE_ENTITY_MIN})"
        ),
        **_metrics_for_assignment(groups, ref_assignment),
    }

    output = {
        "_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "threshold_sweep",
            "dataset": str(_DATASET_PATH.relative_to(_PROJECT_ROOT)),
            "ground_truth": str(_GROUND_TRUTH_PATH.relative_to(_PROJECT_ROOT)),
            "grid": {
                "cosine_thresholds": _SWEEP_COSINE_GRID,
                "entity_mins": _SWEEP_ENTITY_GRID,
            },
            "gate_per_cell": "standard gate in isolation: merge iff cosine >= T AND "
                             "overlap >= E (adaptive branch disabled)",
            "production_constants": {
                "similarity_min": _SIMILARITY_THRESHOLD,
                "entity_overlap_min": _ENTITY_OVERLAP_MIN,
                "adaptive_cosine_floor": _ADAPTIVE_COSINE_FLOOR,
                "adaptive_entity_min": _ADAPTIVE_ENTITY_MIN,
            },
            "articles_total": len(prepared),
            "note": (
                "Three passes. Pass 1 'cells_with_window': production 72h filter on, "
                "greedy — FP structurally impossible on this dataset. "
                "Pass 2 'cells_no_window': time filter removed, greedy — exposes "
                "cosine/entity false-merge risk but greedy ordering still present. "
                "Pass 3 'cells_components_no_window': time filter removed AND "
                "connected-components (order-independent union-find) — the purest "
                "measurement: only (cosine T, entity E) determine merges. "
                "Measurement only — no API calls, no production change."
            ),
        },
        "production_reference": reference,
        "cells_with_window": cells_with_window,
        "cells_no_window": cells_no_window,
        "cells_components_no_window": cells_components_no_window,
    }
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _SWEEP_RESULTS_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("Wrote %s", _SWEEP_RESULTS_PATH.name)

    _print_sweep(
        cells_with_window, reference,
        title="SWEEP (1/3) — WITH 72h window, greedy  [production-faithful]",
        window_note="72h window: ON, strategy: greedy — FP structurally impossible here",
    )
    _print_sweep(
        cells_no_window, reference,
        title="SWEEP (2/3) — WITHOUT 72h window, greedy  [threshold stress-test]",
        window_note="72h window: OFF, strategy: greedy — cosine/entity risk exposed; "
                    "greedy ordering artefact still present",
    )
    _print_sweep(
        cells_components_no_window, reference,
        title="SWEEP (3/3) — WITHOUT 72h window, components  [purest stress-test]",
        window_note="72h window: OFF, strategy: components — both time AND ordering "
                    "artefacts removed; only (cosine T, entity E) determine merges",
    )
    print(
        "\nNOTE: Pass 1 FP is structurally impossible (no two labelled groups share\n"
        "section AND 72h window). Pass 2 removes the time filter (greedy). Pass 3\n"
        "removes BOTH the time filter AND the greedy ordering artefact (union-find)\n"
        "for the purest view of what (cosine T, entity E) alone can and cannot do.\n"
    )
    return 0


async def main_async(args: argparse.Namespace) -> None:
    dataset = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
    articles = dataset.get("articles", [])
    ground_truth = json.loads(_GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    groups = ground_truth.get("groups", [])

    if not articles or not groups:
        logger.error("Dataset or ground truth empty — aborting.")
        sys.exit(1)

    # Read-only 2-D threshold sweep (cache-only, zero API). Runs entirely before
    # the normal path and exits, so the default run below is unaffected.
    if args.sweep:
        sys.exit(run_sweep(articles, groups))

    logger.info("Preprocessing %d articles (embeddings + entities) ...", len(articles))
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    prepared, stopped_early = await preprocess(articles, use_cache=not args.no_cache)

    if stopped_early:
        n_done = sum(
            1 for a in prepared.values() if a.get("embedding") and a.get("entities_ok")
        )
        logger.error(
            "Quota wall reached: all Gemini keys exhausted/quarantined. "
            "%d/%d articles are fully preprocessed and saved in the cache (%s). "
            "clustering_results.json was NOT modified (no degraded overwrite). "
            "Re-run after the daily quota resets to resume from the cache.",
            n_done, len(articles), _CACHE_PATH.name,
        )
        # Non-zero exit code 2 signals "incomplete due to quota" (distinct from
        # the config/empty-dataset exit code 1) without raising (Rule 2.4).
        sys.exit(2)

    order = [int(a["id"]) for a in articles]  # dataset order = greedy order
    n_ready = sum(1 for a in prepared.values() if a.get("embedding") and a.get("published_at"))
    strategy = args.merge_strategy
    adaptive = args.adaptive_threshold
    is_experiment = strategy != "greedy" or adaptive
    logger.info(
        "Clustering %d/%d articles ready (strategy=%s, adaptive=%s%s) ...",
        n_ready, len(prepared), strategy, adaptive,
        " — EXPERIMENT (production-faithful default is greedy/non-adaptive)" if is_experiment else "",
    )
    assignment = cluster(prepared, order, strategy=strategy, adaptive=adaptive)

    per_group = score_groups(groups, assignment)
    groups_correct = sum(1 for r in per_group if r["status"] == "grouped")

    hard = [r for r in per_group if r["is_hard_case"]]
    hard_correct = sum(1 for r in hard if r["status"] == "grouped")
    overall = {
        "groups_total": len(groups),
        "groups_correct": groups_correct,
        "hard_groups": {
            "ids": sorted(_HARD_GROUPS),
            "total": len(hard),
            "correct": hard_correct,
            "detail": {str(r["group_id"]): r["status"] for r in hard},
        },
        "pairwise": pairwise_metrics(groups, assignment),
    }

    # Pair-level gate diagnostics (cosine + shared-entity counts per GT pair) so
    # every missed merge is attributable to a specific failed condition. Reuses
    # already-computed embeddings/entities — no API call, no logic change.
    diagnostics = diagnose_groups(groups, prepared, assignment)

    n_clusters = len({c for c in assignment.values() if not c.startswith("singleton_")})
    n_singletons = sum(1 for c in assignment.values() if c.startswith("singleton_"))
    n_with_entities = sum(1 for a in prepared.values() if a.get("entities_ok"))

    output = {
        "_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset": str(_DATASET_PATH.relative_to(_PROJECT_ROOT)),
            "ground_truth": str(_GROUND_TRUTH_PATH.relative_to(_PROJECT_ROOT)),
            "thresholds": {
                "similarity_min": _SIMILARITY_THRESHOLD,
                "entity_overlap_min": _ENTITY_OVERLAP_MIN,
                "time_window_hours": _WINDOW_SECONDS // 3600,
            },
            "embedding_model": llm_client._EMBEDDING_MODEL,
            "entity_task": "entities",
            "merge_policy": {
                "strategy": strategy,
                "adaptive_threshold": adaptive,
                "is_production_faithful": not is_experiment,
                "adaptive_cosine_floor": _ADAPTIVE_COSINE_FLOOR,
                "adaptive_entity_min": _ADAPTIVE_ENTITY_MIN,
                "note": "Default (greedy / non-adaptive) reproduces ClusteringAgent.run() "
                        "exactly. 'components' (order-independent connected components) and "
                        "adaptive_threshold (strong-entity lower cosine floor) are "
                        "evaluation-only experiments and change no production code.",
            },
            "note": "Read-only harness. Reuses production thresholds, _entity_overlap, "
                    "entity prompt, and embedding path by import; clustering "
                    "orchestration reproduced in-memory (no DB writes). Cosine "
                    "computed exactly (vs pgvector HNSW approximate).",
            "preprocessing": {
                "articles_total": len(prepared),
                "articles_with_embedding": n_ready,
                "articles_with_entities": n_with_entities,
                "clusters_formed": n_clusters,
                "singletons": n_singletons,
                "note": "Clustering requires BOTH an embedding (Conditions 1-2) and "
                        "extracted entities (Condition 3). Articles missing either "
                        "cannot cluster. A low articles_with_entities count indicates "
                        "the Gemini free-tier quota was exhausted during preprocessing "
                        "— re-run after the daily quota resets; the local preprocess "
                        "cache preserves completed work.",
            },
        },
        "overall": overall,
        "per_group": per_group,
        "pairwise_diagnostics": diagnostics,
        "cluster_assignments": {str(aid): assignment[aid] for aid in sorted(assignment)},
    }

    # Experiments write to a distinct file so the production-faithful baseline
    # (clustering_results.json) is never clobbered by an A/B variant.
    if is_experiment:
        suffix = strategy + ("_adaptive" if adaptive else "")
        results_path = _RESULTS_DIR / f"clustering_results__{suffix}.json"
    else:
        results_path = _RESULTS_PATH

    results_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s", results_path.name)
    print_summary(per_group, overall)
    print_diagnostics(diagnostics)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 6 clustering evaluation harness.")
    p.add_argument(
        "--no-cache", action="store_true",
        help="Recompute embeddings/entities, ignoring the local preprocess cache.",
    )
    p.add_argument(
        "--merge-strategy", choices=["greedy", "components"], default="greedy",
        help="Cluster-formation policy. 'greedy' (default) reproduces production "
             "ClusteringAgent.run() exactly (in-order, claim-once). 'components' "
             "forms order-independent connected components over qualifying pairs "
             "(evaluation experiment). Experiments write to a separate results file.",
    )
    p.add_argument(
        "--sweep", action="store_true",
        help=(
            "Measurement only: re-run grouping across a grid of "
            f"cosine {_SWEEP_COSINE_GRID} × entity-min {_SWEEP_ENTITY_GRID} "
            "(standard gate in isolation per cell) plus one production-rule "
            "reference row, to justify the thresholds and show where lowering "
            "entity-min costs precision. Cache-only (zero API calls); writes "
            "clustering_sweep.json and never touches clustering_results.json or "
            "production code."
        ),
    )
    p.add_argument(
        "--adaptive-threshold", action="store_true",
        help=(
            "Evaluation experiment: also merge a pair when cosine >= "
            f"{_ADAPTIVE_COSINE_FLOOR} AND shared entities >= {_ADAPTIVE_ENTITY_MIN} "
            "(a strong entity signal compensates a slightly lower cosine). Default "
            f"off = production fixed gate (cosine >= {_SIMILARITY_THRESHOLD} AND "
            f"entities >= {_ENTITY_OVERLAP_MIN})."
        ),
    )
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
