import asyncio, os, sys
sys.path.insert(0, '/Users/raghad_taboun/Desktop/veritas-agent')
from dotenv import load_dotenv
load_dotenv('/Users/raghad_taboun/Desktop/veritas-agent/.env')

import psycopg2
from agents.bias_agent import BiasAgent

def fetch_inputs():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()

    # All Libya article IDs
    cur.execute("SELECT id FROM articles WHERE section = 'libya' ORDER BY id")
    article_ids = [r[0] for r in cur.fetchall()]

    # Libya event clusters: event_id -> [article_id, ...]
    cur.execute("""
        SELECT ae.event_id, ae.article_id
        FROM article_events ae
        JOIN events e ON e.id = ae.event_id
        WHERE e.section = 'libya'
        ORDER BY ae.event_id, ae.article_id
    """)
    event_clusters: dict[int, list[int]] = {}
    for event_id, article_id in cur.fetchall():
        event_clusters.setdefault(event_id, []).append(article_id)

    conn.close()
    return article_ids, event_clusters

def verify_results(article_ids):
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    cur.execute("""
        SELECT bs.article_id, a.title, bs.score, bs.label, bs.confidence, bs.framing
        FROM bias_scores bs
        JOIN articles a ON a.id = bs.article_id
        WHERE a.section = 'libya'
          AND bs.article_id = ANY(%s)
        ORDER BY bs.article_id
    """, (article_ids,))
    rows = cur.fetchall()
    conn.close()
    print(f"\n=== bias_scores verification: {len(rows)} rows ===")
    for r in rows:
        print(f"  article {r[0]} | {r[3]:20s} | score={r[2]:+.2f} | conf={r[4]:.2f} | {r[1][:60]}")

async def main():
    article_ids, event_clusters = fetch_inputs()
    print(f"article_ids ({len(article_ids)}): {article_ids}")
    print(f"event_clusters ({len(event_clusters)}): {event_clusters}")

    result = await BiasAgent().run(article_ids, event_clusters)

    print(f"\n=== BiasAgent results ===")
    print(f"stored_count={result['stored_count']}")
    print(f"bias_results={len(result['bias_results'])}")
    print(f"errors={result['errors']}")
    for r in result['bias_results']:
        print(f"  article {r['article_id']} | {r['label']:20s} | score={r['score']:+.2f} | conf={r['confidence']:.2f}")

    verify_results(article_ids)

asyncio.run(main())
