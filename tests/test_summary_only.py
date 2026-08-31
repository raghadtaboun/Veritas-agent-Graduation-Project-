"""
scripts/test_summary_only.py — Regenerate summary for existing event(s).

This script runs ONLY the SummaryAgent on event IDs you provide. It skips
ingestion, clustering, bias, blindspot, and recommendations entirely.

Use cases:
- Verify a SummaryAgent fix on an event that previously had NULL summary
- Compare summary quality before/after a prompt change
- Backfill missing summaries without running the full pipeline

Usage:
    # Single event:
    python test_summary_only.py 255

    # Multiple events:
    python test_summary_only.py 255 256 257

    # All events with NULL summary (no event_ids given):
    python test_summary_only.py --null-only

    # Force regeneration (clear cache first):
    python test_summary_only.py 255 --no-cache
"""

import asyncio
import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.env_bootstrap import bootstrap_env
bootstrap_env()

import psycopg2
import psycopg2.extras

from agents.summary_agent import SummaryAgent
from agents.base import MCPAgent


# ─────────────────────────────────────────────────────────────────────────────
# Direct DB connection (independent of MCP server)
# ─────────────────────────────────────────────────────────────────────────────

def _get_db_conn():
    """Connect directly to PostgreSQL using DATABASE_URL from env."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set in environment.")
    return psycopg2.connect(db_url)


def get_events_with_null_summary_sql(limit: int = 50) -> list[int]:
    """Find event IDs whose summary column is NULL via direct SQL."""
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM events
                WHERE summary IS NULL OR summary = ''
                ORDER BY id DESC
                LIMIT %s
            """, (limit,))
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def verify_in_db_sql(event_id: int) -> dict:
    """Read the event row from DB after the update via direct SQL."""
    conn = _get_db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id, headline, summary, bias_assessment
                FROM events
                WHERE id = %s
            """, (event_id,))
            row = cur.fetchone()
            if not row:
                return {"error": f"event {event_id} not found"}

            summary = row["summary"] or ""
            assessment = row["bias_assessment"] or ""
            headline = row["headline"] or ""

            return {
                "event_id":           row["id"],
                "headline":           headline[:80],
                "summary_len":        len(summary),
                "summary_preview":    summary[:200],
                "assessment_len":     len(assessment),
                "assessment_preview": assessment[:200],
            }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> tuple[list[int], bool, bool]:
    """Parse: positional event_ids + flags --null-only --no-cache."""
    null_only = False
    no_cache = False
    event_ids: list[int] = []

    for arg in argv[1:]:
        if arg == "--null-only":
            null_only = True
        elif arg == "--no-cache":
            no_cache = True
        elif arg.startswith("--"):
            print(f"⚠️  Unknown flag: {arg}")
        else:
            try:
                event_ids.append(int(arg))
            except ValueError:
                print(f"⚠️  Skipping non-integer arg: {arg}")

    return event_ids, null_only, no_cache


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers (via MCP)
# ─────────────────────────────────────────────────────────────────────────────

async def clear_summary_cache_for_event(agent: MCPAgent, event_id: int) -> None:
    """Delete cached facts/neutral_summary/bias_assessment for one event."""
    for key in (
        f"facts:{event_id}",
        f"neutral_summary:{event_id}",
        f"bias_assessment:{event_id}",
    ):
        # Try cache_delete first; fall back to short-TTL set
        try:
            result = await agent.call_tool("cache_delete", {"key": key})
            if "error" in result:
                # Fallback: set with very short TTL
                await agent.call_tool("cache_set", {"key": key, "value": "", "ttl": 1})
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    event_ids, null_only, no_cache = parse_args(sys.argv)

    # Resolve event IDs
    if null_only:
        print("🔍 Finding events with NULL summary (direct SQL)...")
        event_ids = get_events_with_null_summary_sql(limit=50)
        if not event_ids:
            print("✅ No events with NULL summary found. Nothing to do.")
            return
        print(f"   Found {len(event_ids)} event(s): {event_ids[:10]}"
              + (f" ... and {len(event_ids) - 10} more" if len(event_ids) > 10 else "")
              + "\n")
    elif not event_ids:
        print("Usage: python test_summary_only.py <event_id> [event_id ...]")
        print("   or: python test_summary_only.py --null-only")
        print("   or: python test_summary_only.py 255 --no-cache")
        sys.exit(1)

    # Optionally clear cache to force regeneration
    if no_cache:
        helper = MCPAgent()
        print(f"🗑️   Clearing cache for {len(event_ids)} event(s)...")
        for eid in event_ids:
            await clear_summary_cache_for_event(helper, eid)
        print("    Cache cleared.\n")

    # Header
    print("=" * 70)
    print(f"🧪 SummaryAgent Regression Test")
    print(f"   Event IDs:   {event_ids if len(event_ids) <= 10 else f'{event_ids[:10]}... ({len(event_ids)} total)'}")
    print(f"   --no-cache:  {no_cache}")
    print(f"   --null-only: {null_only}")
    print("=" * 70)

    # Run SummaryAgent
    agent = SummaryAgent()
    result = await agent.run(event_ids=event_ids)

    # Report agent-level results
    print(f"\n📊 SummaryAgent results:")
    print(f"   summaries returned:  {len(result.get('summaries', []))}")
    print(f"   stored_count:        {result.get('stored_count', 0)}")
    print(f"   errors:              {len(result.get('errors', []))}")

    if result.get("errors"):
        print(f"\n⚠️   Errors:")
        for err in result["errors"][:10]:
            print(f"   - {err[:150]}")

    # Per-event details (limit output for backfill mode)
    summaries_to_show = result.get("summaries", [])
    show_details = len(summaries_to_show) <= 5

    if show_details:
        for summary_row in summaries_to_show:
            eid = summary_row["event_id"]
            ns = summary_row.get("neutral_summary", "") or ""
            ba = summary_row.get("bias_assessment", "") or ""

            print(f"\n──────────────────────────────────────────────────────────")
            print(f"   Event {eid}")
            print(f"──────────────────────────────────────────────────────────")
            print(f"   neutral_summary: {len(ns)} chars")
            if ns:
                print(f"      preview: {ns[:200]}...")
            else:
                print(f"      ⚠️  EMPTY")

            print(f"\n   bias_assessment: {len(ba)} chars")
            if ba:
                print(f"      preview: {ba[:200]}...")
            else:
                print(f"      ⚠️  EMPTY")

    # DB verification — direct SQL (no MCP)
    print("\n" + "=" * 70)
    print("🔍 DB Verification (final state after UPDATE — direct SQL)")
    print("=" * 70)

    total_success = 0
    total_partial = 0
    total_empty = 0

    for eid in event_ids:
        try:
            db_state = verify_in_db_sql(eid)
        except Exception as e:
            print(f"\n   Event {eid}: ❌ DB error — {e}")
            continue

        if "error" in db_state:
            print(f"\n   Event {eid}: ❌ {db_state['error']}")
            continue

        s_ok = "✅" if db_state["summary_len"] > 0 else "❌"
        a_ok = "✅" if db_state["assessment_len"] > 0 else "❌"

        if db_state["summary_len"] > 0 and db_state["assessment_len"] > 0:
            total_success += 1
        elif db_state["summary_len"] > 0 or db_state["assessment_len"] > 0:
            total_partial += 1
        else:
            total_empty += 1

        if show_details:
            print(f"\n   Event {eid}: {db_state['headline']}")
            print(f"      {s_ok} summary         = {db_state['summary_len']:>4} chars")
            print(f"      {a_ok} bias_assessment = {db_state['assessment_len']:>4} chars")
            if db_state['summary_len'] > 0:
                print(f"      preview: {db_state['summary_preview'][:120]}...")

    # Aggregate summary for backfill mode
    if not show_details:
        print(f"\n   ✅ Both fields populated:  {total_success}/{len(event_ids)}")
        print(f"   ⚠️  Partial (one missing):  {total_partial}/{len(event_ids)}")
        print(f"   ❌ Both fields empty:      {total_empty}/{len(event_ids)}")

    # SQL hint
    if show_details:
        print("\n" + "=" * 70)
        print("📋 To inspect manually:")
        ids_str = ",".join(str(e) for e in event_ids)
        print(f"   psql -U postgres -d newsguard -c \\")
        print(f"     \"SELECT id, LENGTH(summary) AS s_len, "
              f"LENGTH(bias_assessment) AS a_len, "
              f"LEFT(summary, 100) AS summary_preview \\")
        print(f"      FROM events WHERE id IN ({ids_str}) ORDER BY id;\"")


if __name__ == "__main__":
    asyncio.run(main())