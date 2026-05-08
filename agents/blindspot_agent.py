"""
agents/blindspot_agent.py — Blindspot Agent (Agent 4) [Canonical MCP rewrite]

Detects underrepresented political perspectives in event coverage by calling
the `detect_blindspot` MCP tool for each event, then persists positive
findings via the `insert_blindspot_report` MCP tool.

Architecture (Phase 4.5 canonical):
  - Deterministic — no LLM calls, no ReAct loop, no reasoning fallback.
  - Every DB and cache operation goes through `self.call_tool()` (Rule 2.3
    canonical boundary). No direct psycopg2, redis, or httpx imports.
  - Idempotency is enforced on the server side: `detect_blindspot` returns
    `has_blindspot=False` for events with fewer than 3 classified articles,
    and `insert_blindspot_report` uses a `WHERE NOT EXISTS` guard so a
    prior pipeline run's report is never duplicated.
  - Public `run(event_ids)` signature unchanged from the legacy agent.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.base import MCPAgent

logger = logging.getLogger(__name__)


class BlindspotAgent(MCPAgent):
    """
    Agent 4 — Blindspot Agent (canonical MCP, deterministic).

    For each event in the input list, invokes `detect_blindspot`; when a
    blindspot is reported, invokes `insert_blindspot_report`. Never raises;
    per-event failures are appended to the returned `errors` list.
    """

    async def run(self, event_ids: list[int]) -> dict[str, Any]:
        """
        Detect and persist blindspots for each event in *event_ids*.

        Parameters
        ----------
        event_ids : list[int]
            Event IDs produced by the Clustering Agent. Events with fewer
            than 3 classified articles are excluded by the server-side
            `detect_blindspot` tool (returns `has_blindspot=False`).

        Returns
        -------
        dict with keys:
            blindspots   : list[dict]  — one entry per event where a blindspot
                           was detected, each containing event_id,
                           missing_perspectives, coverage_stats, total.
            stored_count : int         — rows newly inserted into
                           `blindspot_reports` this run (excludes
                           idempotent no-ops).
            errors       : list[str]   — non-fatal error strings.
        """
        if not event_ids:
            return {"blindspots": [], "stored_count": 0, "errors": []}

        blindspots: list[dict[str, Any]] = []
        stored_count = 0
        errors: list[str] = []

        for raw_eid in event_ids:
            try:
                eid = int(raw_eid)
            except (TypeError, ValueError):
                errors.append(f"invalid event_id in input: {raw_eid!r}")
                continue

            detect_result = await self.call_tool(
                "detect_blindspot",
                {"event_id": eid},
            )

            if "error" in detect_result:
                errors.append(
                    f"detect_blindspot error for event {eid}: "
                    f"{detect_result['error']}"
                )
                continue

            if not detect_result.get("has_blindspot"):
                # Either no blindspot, or insufficient coverage (< 3 articles).
                # The spec says: skip silently, do not store a "no-blindspot" row.
                logger.debug(
                    "[BlindspotAgent] Event %d — no blindspot "
                    "(total=%s, note=%r)",
                    eid,
                    detect_result.get("total", "?"),
                    detect_result.get("note"),
                )
                continue

            coverage_stats = detect_result.get("coverage_stats", {}) or {}
            missing = detect_result.get("missing_perspectives", []) or []

            entry: dict[str, Any] = {
                "event_id":             eid,
                "missing_perspectives": list(missing),
                "coverage_stats":       dict(coverage_stats),
                "total":                int(detect_result.get("total", 0)),
            }
            blindspots.append(entry)

            insert_result = await self.call_tool(
                "insert_blindspot_report",
                {
                    "event_id":             eid,
                    "coverage_stats":       coverage_stats,
                    "missing_perspectives": list(missing),
                },
            )

            if "error" in insert_result:
                errors.append(
                    f"insert_blindspot_report error for event {eid}: "
                    f"{insert_result['error']}"
                )
                continue

            if insert_result.get("inserted"):
                stored_count += 1
            # inserted=False means a prior run already recorded this
            # blindspot — expected idempotent behaviour, NOT an error.

        logger.info(
            "[BlindspotAgent] events=%d  blindspots=%d  stored=%d  errors=%d",
            len(event_ids),
            len(blindspots),
            stored_count,
            len(errors),
        )

        return {
            "blindspots":   blindspots,
            "stored_count": stored_count,
            "errors":       errors,
        }
