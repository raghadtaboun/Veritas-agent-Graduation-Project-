"""
agents/clustering_agent.py — Clustering Agent (Agent 2) [Canonical MCP rewrite]

Groups articles covering the same real-world event into clusters using three
conditions that must all be satisfied simultaneously:

  Condition 1 — Time Window   : |published_at_A - published_at_B| ≤ 72 hours
  Condition 2 — Similarity    : cosine_similarity(embedding_A, embedding_B) ≥ 0.82
  Condition 3 — Entity Overlap: |entities_A ∩ entities_B| ≥ 2

Conditions 1 and 2 are enforced at the SQL level by the `find_similar` MCP
tool. Condition 3 is evaluated in pure Python on entity data returned by
the `get_articles` MCP tool.

Architecture (Phase 4.5 canonical):
  - Deterministic — no LLM calls, no ReAct loop, no `think()`.
  - Every PostgreSQL read/write goes through `self.call_tool()`; no direct
    psycopg2 imports, no direct Redis imports (Rule 2.3 canonical boundary).
  - Public `run(section, article_ids)` signature unchanged from the legacy
    agent, so `agents/graph.py` needs no modification.

Cluster / singleton representation (documented choice per Rule 6.1):
  `event_clusters` contains ONLY multi-article clusters (size ≥ 2) keyed by
  their newly-created `event_id`. Singletons are implicit — they are the
  members of `article_ids` that do not appear anywhere in the flattened
  values of `event_clusters`. The returned `singleton_count` is computed
  as ``len(article_ids) - clustered_count``.

Relevance-score convention for `link_article_event`:
  The representative article (earliest published in the cluster) is linked
  with ``relevance_score = 1.0``. Every other member is linked with the
  cosine similarity returned by `find_similar`, preserving distance-from-
  centroid information for downstream analysis.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.base import MCPAgent

logger = logging.getLogger(__name__)

# ── Clustering thresholds ─────────────────────────────────────────────────────

_SIMILARITY_THRESHOLD: float = 0.82   # Condition 2 — cosine similarity floor
_ENTITY_OVERLAP_MIN:   int   = 2       # Condition 3 — minimum shared entities
_FIND_SIMILAR_LIMIT:   int   = 20      # max candidates returned per find_similar


class ClusteringAgent(MCPAgent):
    """
    Agent 2 — Clustering Agent (canonical MCP, deterministic).

    `run(section, article_ids)` fetches article metadata via `get_articles`,
    finds same-event candidates via `find_similar`, filters by entity-overlap
    in Python, and persists each multi-article cluster through `insert_event`
    + `link_article_event`. Returns a dict compatible with `NewsState`.
    """

    async def run(
        self,
        section: str,
        article_ids: list[int],
    ) -> dict[str, Any]:
        """
        Cluster articles into events for a given section.

        Parameters
        ----------
        section : str
            One of "middle_east", "libya", or "world".
        article_ids : list[int]
            IDs of articles to cluster. Must all belong to *section*.

        Returns
        -------
        dict with keys:
            event_ids        : list[int]              — IDs of newly-created events
            event_clusters   : dict[int, list[int]]   — event_id → sorted member ids
            clustered_count  : int                    — articles assigned to a cluster
            singleton_count  : int                    — articles with no cluster match
            errors           : list[str]              — non-fatal error messages
        """
        if not article_ids:
            return {
                "event_ids":       [],
                "event_clusters":  {},
                "clustered_count": 0,
                "singleton_count": 0,
                "errors":          [],
            }

        errors: list[str] = []

        # ── Step 1: Fetch article metadata via MCP ───────────────────────────
        fetch_result = await self.call_tool("get_articles", {"ids": article_ids})
        if "error" in fetch_result:
            msg = f"get_articles failed: {fetch_result['error']}"
            logger.error("[ClusteringAgent] %s", msg)
            return {
                "event_ids":       [],
                "event_clusters":  {},
                "clustered_count": 0,
                "singleton_count": len(article_ids),
                "errors":          [msg],
            }

        articles: list[dict] = fetch_result.get("articles", []) or []
        if not articles:
            msg = "get_articles returned zero rows — aborting clustering."
            logger.warning("[ClusteringAgent] %s", msg)
            return {
                "event_ids":       [],
                "event_clusters":  {},
                "clustered_count": 0,
                "singleton_count": len(article_ids),
                "errors":          [msg],
            }

        article_by_id: dict[int, dict] = {int(a["id"]): a for a in articles}
        article_id_set: set[int] = set(article_by_id)

        # ── Step 2: Build clusters ───────────────────────────────────────────
        assigned: set[int] = set()
        clusters: list[dict[str, Any]] = []   # each: {members: set[int], scores: dict[int, float]}

        # Iterate in input order so the "earliest-published" tie-breaker below
        # is deterministic even if `articles` is already id-sorted by server.
        for aid in article_ids:
            article = article_by_id.get(int(aid))
            if article is None:
                continue

            article_id = int(article["id"])
            if article_id in assigned:
                continue

            embedding = article.get("embedding")
            published_at = article.get("published_at")
            if not embedding or not published_at:
                logger.debug(
                    "[ClusteringAgent] Article %d missing embedding or published_at — skipping.",
                    article_id,
                )
                continue

            # Conditions 1 & 2 are enforced in SQL by find_similar.
            sim_result = await self.call_tool(
                "find_similar",
                {
                    "embedding":    embedding,
                    "section":      section,
                    "published_at": published_at,
                    "threshold":    _SIMILARITY_THRESHOLD,
                    "limit":        _FIND_SIMILAR_LIMIT,
                    "exclude_id":   article_id,
                },
            )

            if "error" in sim_result:
                msg = f"find_similar error for article {article_id}: {sim_result['error']}"
                logger.warning("[ClusteringAgent] %s", msg)
                errors.append(msg)
                continue

            candidates = sim_result.get("articles", []) or []

            # Restrict to candidates that belong to this run and aren't already
            # assigned to another cluster.
            in_run = [
                c for c in candidates
                if int(c.get("id", -1)) in article_id_set
                and int(c.get("id", -1)) not in assigned
            ]

            # Condition 3 — entity overlap ≥ 2 (Python filter).
            members: set[int] = {article_id}
            scores: dict[int, float] = {article_id: 1.0}
            for cand in in_run:
                cand_id = int(cand["id"])
                cand_article = article_by_id.get(cand_id)
                if cand_article is None:
                    continue
                overlap = _entity_overlap(
                    article.get("entities") or {},
                    cand_article.get("entities") or {},
                )
                if overlap >= _ENTITY_OVERLAP_MIN:
                    members.add(cand_id)
                    try:
                        scores[cand_id] = float(cand.get("similarity", 0.0))
                    except (TypeError, ValueError):
                        scores[cand_id] = 0.0

            if len(members) >= 2:
                clusters.append({"members": members, "scores": scores})
                assigned.update(members)

        # ── Step 3: Persist clusters as events via MCP ───────────────────────
        event_ids: list[int] = []
        event_clusters: dict[int, list[int]] = {}

        for cluster in clusters:
            members = cluster["members"]
            scores = cluster["scores"]

            rep_id = _earliest_id(members, article_by_id)
            headline = (article_by_id[rep_id].get("title") or "").strip() or None

            ev_result = await self.call_tool(
                "insert_event",
                {
                    "section":    section,
                    "title":      headline or "",
                    "created_at": None,
                },
            )
            if "error" in ev_result or "event_id" not in ev_result:
                msg = (
                    f"insert_event failed for cluster rep_id={rep_id}: "
                    f"{ev_result.get('error', 'no event_id returned')}"
                )
                logger.error("[ClusteringAgent] %s", msg)
                errors.append(msg)
                # Unassign this cluster's members — they may find another
                # home on a subsequent run.
                assigned.difference_update(members)
                continue

            event_id = int(ev_result["event_id"])

            linked_members: list[int] = []
            for member_id in sorted(members):
                link_result = await self.call_tool(
                    "link_article_event",
                    {
                        "event_id":        event_id,
                        "article_id":      member_id,
                        "relevance_score": float(scores.get(member_id, 1.0)),
                    },
                )
                if "error" in link_result:
                    msg = (
                        f"link_article_event error event={event_id} "
                        f"article={member_id}: {link_result['error']}"
                    )
                    logger.warning("[ClusteringAgent] %s", msg)
                    errors.append(msg)
                    continue
                linked_members.append(member_id)

            event_ids.append(event_id)
            event_clusters[event_id] = linked_members

        clustered_count = sum(len(v) for v in event_clusters.values())
        singleton_count = len(article_ids) - clustered_count

        logger.info(
            "[ClusteringAgent] section=%s  articles=%d  events=%d  "
            "clustered=%d  singletons=%d  errors=%d",
            section,
            len(article_ids),
            len(event_ids),
            clustered_count,
            singleton_count,
            len(errors),
        )

        return {
            "event_ids":       event_ids,
            "event_clusters":  event_clusters,
            "clustered_count": clustered_count,
            "singleton_count": singleton_count,
            "errors":          errors,
        }


# ── Module-level helpers (pure functions — no DB or MCP access) ──────────────

def _entity_overlap(entities_a: dict, entities_b: dict) -> int:
    """
    Return the number of named entities shared between two entity dicts.

    Combines people + locations + organizations, normalises each string via
    ``.strip().lower()``, and returns the size of the intersection.
    """
    set_a: set[str] = set()
    set_b: set[str] = set()

    for key in ("people", "locations", "organizations"):
        for e in entities_a.get(key, []) or []:
            s = str(e).strip().lower()
            if s:
                set_a.add(s)
        for e in entities_b.get(key, []) or []:
            s = str(e).strip().lower()
            if s:
                set_b.add(s)

    return len(set_a & set_b)


def _earliest_id(members: set[int], article_by_id: dict[int, dict]) -> int:
    """
    Return the member id whose `published_at` is earliest.

    Falls back to the smallest id when timestamps are missing or equal, so the
    result is fully deterministic and never raises.
    """
    def _key(aid: int) -> tuple[int, str, int]:
        pub = article_by_id.get(aid, {}).get("published_at")
        has_pub = 0 if pub else 1   # articles with timestamps sort first
        return (has_pub, str(pub or ""), aid)

    return min(members, key=_key)
