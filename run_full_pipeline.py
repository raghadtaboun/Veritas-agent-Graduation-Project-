"""
run_full_pipeline.py — Full end-to-end pipeline run via graph.py

This calls run_pipeline() which executes the complete LangGraph:
    GDELT → Ingestion → Clustering → Bias → Blindspot → Summary → Recommendation

Output: NewsState dict + saved to Redis as results:{section}

Usage:
    python run_full_pipeline.py              # default: libya
    python run_full_pipeline.py middle_east  # specific section
    python run_full_pipeline.py world        # world section
"""

import asyncio
import json
import sys
from datetime import datetime

from agents.graph import run_pipeline


async def main():
    # Get section from command line or default to libya
    section = sys.argv[1] if len(sys.argv) > 1 else "libya"
    
    if section not in ("libya", "middle_east", "world"):
        print(f"❌ Invalid section: {section}")
        print("   Valid sections: libya, middle_east, world")
        sys.exit(1)
    
    print("=" * 70)
    print(f"🚀 Starting Veritas Pipeline (FULL END-TO-END)")
    print(f"   Section:    {section}")
    print(f"   Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    start = asyncio.get_event_loop().time()
    
    try:
        state = await run_pipeline(section)
    except Exception as e:
        print(f"\n❌ Pipeline crashed: {e}")
        raise
    
    elapsed = asyncio.get_event_loop().time() - start
    
    print("\n" + "=" * 70)
    print(f"✅ Pipeline complete in {elapsed:.1f}s")
    print("=" * 70)
    
    # ============================================================
    # Final Summary
    # ============================================================
    print(f"\n📊 RESULTS:")
    print(f"   Section:           {state.get('section')}")
    print(f"   Articles fetched:  {len(state.get('article_ids', []))}")
    print(f"   Events created:    {len(state.get('cluster_ids', []))}")
    print(f"   Bias results:      {len(state.get('bias_results', []))}")
    print(f"   Blindspots:        {len(state.get('blindspots', []))}")
    print(f"   Summaries:         {len(state.get('summaries', []))}")
    print(f"   Recommendations:   {len(state.get('recommendations', {}))}")
    print(f"   Errors:            {len(state.get('errors', []))}")
    
    # Per-agent stats
    print(f"\n📈 PER-AGENT STATS:")
    for agent_name, stats in state.get('stats', {}).items():
        print(f"   {agent_name:20s}: {stats}")
    
    # Show sample event if any
    cluster_ids = state.get('cluster_ids', [])
    if cluster_ids:
        print(f"\n🎯 EVENT IDs CREATED: {cluster_ids}")
        print(f"\n   To view events with summaries, run:")
        print(f"   psql -U postgres -d newsguard -c \"\\")
        print(f"   SELECT id, headline, LEFT(summary, 200) FROM events \\")
        print(f"   WHERE id = ANY(ARRAY{cluster_ids});\"")
    
    # Show errors if any
    errors = state.get('errors', [])
    if errors:
        print(f"\n⚠️  ERRORS ({len(errors)} total, showing first 3):")
        for err in errors[:3]:
            print(f"   - {err[:120]}")
        if len(errors) > 3:
            print(f"   ... and {len(errors) - 3} more")
    
    # Recommendations preview
    recs = state.get('recommendations', {})
    if recs:
        first_id = next(iter(recs))
        first_recs = recs[first_id]
        print(f"\n💡 SAMPLE RECOMMENDATIONS for article {first_id}:")
        for r in first_recs[:3]:
            sim = r.get('similarity', 0)
            print(f"   - similarity={sim:.3f}, bias={r.get('bias_label')}, "
                  f"title={r.get('title', '')[:60]}")
    
    # Redis cache verification
    print(f"\n💾 REDIS CACHE:")
    print(f"   Key: results:{section}")
    print(f"   To verify: redis-cli GET results:{section} | head -c 200")


if __name__ == "__main__":
    asyncio.run(main())