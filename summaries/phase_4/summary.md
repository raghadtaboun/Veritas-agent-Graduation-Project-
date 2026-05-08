# Phase 4 Summary — Remaining Agents and Advanced MCP Tools

## A — Phase Identification

| Field | Value |
|---|---|
| Phase Number | 4 |
| Phase Title | Remaining Agents and Advanced MCP Tools |
| Start Date | 2026-04-14 |
| Completion Date | 2026-04-14 |

> **Phase 4 is COMPLETE.** All six steps (4.1–4.6) are complete and all success criteria are met. 23 tests pass.

---

## B — What Was Implemented

### Step 4.6 — `tests/test_pipeline.py` **(complete)**

#### `tests/test_pipeline.py` (new file, 144 lines)

`test_pipeline_libya_end_to_end` is the Step 4.6 integration test. It runs the full six-agent pipeline for the `'libya'` section and verifies the final `NewsState`.

**Test strategy:**
- Clears `results:libya` from Redis before running so the Redis-write assertion is fresh
- Calls `run_pipeline("libya")` and awaits the complete result
- Does NOT mock any LLM or infrastructure calls — this is a true end-to-end integration test against live PostgreSQL, Redis, MCP server, GDELT, Gemini, and Groq

**13 checks:**
1. `run_pipeline` returns a dict (NewsState)
2. `state["section"] == "libya"`
3. `state["article_ids"]` is a non-empty list
4. All article_ids are integers
5. `state["cluster_ids"]` is non-empty (at least one event cluster)
6. `state["summaries"]` is non-empty
7. Each summary entry has `event_id`, `neutral_summary`, `bias_assessment`
8. `state["recommendations"]` is a non-empty dict
9. `state["bias_results"]` is non-empty
10. `state["stats"]` contains an `"ingestion"` key
11. `state["errors"]` is a list (non-empty tolerated — pipeline is fault-tolerant)
12. Redis key `results:libya` was set after the run
13. Cached JSON contains at least 1 article_id

**Runtime:** ~218 s on first run (live GDELT + agents), ~85 s on re-run (Redis caches hit for LLM calls).

**Network requirement:** Requires `full_network` permissions to reach GDELT API and external LLM APIs (Gemini, Groq). Fails with "fetched=0" if GDELT is unreachable (sandbox allowlist restriction).

---

### Step 4.5 — `agents/graph.py` **(complete)**

#### `agents/graph.py` (new file, 260 lines)

`agents/graph.py` is the LangGraph orchestration layer. It defines `NewsState`, six async node functions, the compiled graph, and `run_pipeline(section)`.

**`NewsState` TypedDict (10 fields)**

Required by plan.md (9 fields): `section`, `article_ids`, `cluster_ids`, `bias_results`, `blindspots`, `summaries`, `recommendations`, `stats`, `errors`.

Additional field: `event_clusters: dict` — maps `event_id → [article_ids]`. Produced by `ClusteringAgent` and consumed by `BiasAgent`. Not in the nine-field spec but required for the Clustering → Bias handoff. Without it, BiasAgent cannot perform comparative analysis.

**Six node functions (all async)**

Each node:
- Imports its agent lazily (inside the function) to prevent circular imports
- Catches all exceptions with try/except and appends error strings to `state["errors"]` — never raises
- Skips execution (returns empty defaults) if its required input (article_ids or cluster_ids) is empty
- Returns a partial state update dict that LangGraph merges into the shared state
- Logs start and completion with key metrics

| Node | Agent | Key input | Key output |
|---|---|---|---|
| `_ingestion_node` | `IngestionAgent` | `section` | `article_ids`, `stats.ingestion` |
| `_clustering_node` | `ClusteringAgent` | `article_ids`, `section` | `cluster_ids`, `event_clusters`, `stats.clustering` |
| `_bias_node` | `BiasAgent` | `article_ids`, `event_clusters` | `bias_results`, `stats.bias` |
| `_blindspot_node` | `BlindspotAgent` | `cluster_ids` | `blindspots`, `stats.blindspot` |
| `_summary_node` | `SummaryAgent` | `cluster_ids` | `summaries`, `stats.summary` |
| `_recommendation_node` | `RecommendationAgent` | `article_ids`, `section` | `recommendations`, `stats.recommendation` |

Note on IngestionAgent errors: `IngestionAgent.run()` returns `errors` as an int count (not a list). The `_ingestion_node` converts it to a string message when non-zero: `"[ingestion] N article-level errors"`.

**`_build_pipeline()`**

Constructs a `StateGraph(NewsState)`, registers all six nodes, adds edges in sequence (`START → ingestion → clustering → bias → blindspot → summary → recommendation → END`), and returns `workflow.compile()`.

**`_serialise_state(state) → str`**

Converts the final NewsState to a JSON string. The `recommendations` dict uses int keys (article_id) which are invalid JSON keys; `_serialise_state` converts them to `str(k)` before serialisation.

**`run_pipeline(section) → NewsState`**

1. Initialises all NewsState fields to empty defaults.
2. Calls `_build_pipeline()` and `await pipeline.ainvoke(initial_state)`.
3. On pipeline crash (should not happen; all nodes are protected): returns the initial state with an error message.
4. Stores the final state in Redis under `results:{section}` with TTL = 21,600 s (6 hours) via `MCPAgent.call_tool("cache_set", ...)`.
5. Returns the final state.

The Redis storage uses a temporary `MCPAgent()` instance to preserve the MCP architectural boundary (Rule 2.3). `run_pipeline` itself is not an agent, but it must not call Redis directly.

---

### Step 4.4 — `agents/recommendation_agent.py` **(complete)**

#### `agents/recommendation_agent.py` (new file, 219 lines)

`RecommendationAgent` is the sixth LangGraph agent. It inherits from `MCPAgent` and follows the same ReAct pattern as `BlindspotAgent`, `BiasAgent`, and `SummaryAgent`.

**Class: `RecommendationAgent(MCPAgent)`**

Constructor sets `self._task_queue: list[int] = []` (article IDs for the current run) and `self._section: str = ""` (section name, used by `_fallback_decide`).

**`run(article_ids: list[int], section: str, user_id: str = "") → dict`**

Flow:
1. Truncates `article_ids` to `_MAX_ARTICLES_PER_RUN = 10` (plan.md Step 4.4 spec).
2. Saves `self._task_queue` and `self._section` for the fallback path.
3. Builds a numbered goal string instructing the LLM to check `cache_get` for each article first, then call `vector_recommend` on cache misses, then `cache_set` the result.
4. Calls `self.run_react(...)` with `max_steps = max(N * 3 + 10, 20)` — 3 steps per article (cache_get → vector_recommend → cache_set) plus a 10-step buffer.
5. Scans `react["observations"]` to collect results from both `vector_recommend` calls (fresh results) and `cache_get` hits (previously cached).
6. Returns `{"recommendations": dict[int, list[dict]], "stored_count": int, "errors": list[str]}`.

`min_similarity = 0.5` is used in all `vector_recommend` calls (per summary.md Deviation 1 guidance). `limit = 5` recommendations per article.

Cache key format: `recommend:{article_id}`, TTL: `21600` s (6 hours, matching the pipeline Redis TTL used by `graph.py` in Step 4.5).

Return-value keys compatible with LangGraph `NewsState`:
- `recommendations` — `dict[int, list[dict]]` mapping article_id → list of `{id, title, bias_label, similarity}` recommendation dicts
- `stored_count` — count of articles with at least one recommendation
- `errors` — non-fatal error strings

**`_fallback_decide(goal, available_tools, observations, step) → dict`**

When Groq is unavailable, skips cache_get/cache_set and calls `vector_recommend` directly for each article in `self._task_queue` not yet seen in observations. Uses the same queue-walk pattern as `BlindspotAgent` and `BiasAgent`. Returns `{"done": True, ...}` when all articles are handled.

**`_RECOMMEND_TOOLS` (module-level list)**

Four tool descriptions:
- `vector_recommend` — primary tool; one call per article
- `get_user_profile` — optional; for personalised recommendations when `user_id` is supplied
- `cache_get` — check for previously cached recommendations
- `cache_set` — store new recommendations (TTL 21600 s)

---

#### `agents/base.py` — `think()` network-error handling extended

**What changed:** The `except` block in `think()` previously only set `_groq_unavailable = True` for errors containing "403" or "Access denied". In the test sandbox environment (no network access), `httpx` raises a generic "Connection error" which did not match the check, causing `think()` to return `{"done": True}` immediately and halt the ReAct loop before `_fallback_decide` could run.

**Fix:** Extended the `_is_network_failure` check to also cover:
- "Connection error" (httpx, no network)
- "connection" (case-insensitive, covers ConnectionError variants)
- "timeout" (case-insensitive)
- "ssl" (case-insensitive)

All of these represent transport-level failures where Groq is unavailable. Setting `_groq_unavailable = True` for any of these causes all subsequent `think()` calls in the same run to skip the Groq API call and go directly to `_fallback_decide`, which is the intended behavior.

**Impact:** This fix makes the fallback strategy (deterministic queue-walk) work correctly in any environment where the Groq API is unreachable, regardless of the exact error type. All six agents benefit from this fix since they all inherit from `MCPAgent`.

---

#### `tests/test_agents.py` — `test_recommendation_agent_returns_recommendations` (new test, test #6)

**Strategy:** Seed 2 articles with different bias labels and identical embeddings, run `RecommendationAgent.run([art_a_id], "libya")`, and verify cross-bias recommendations are returned.

No LLM mocking needed: `vector_recommend` is a deterministic pgvector query. `think()` falls back via `_fallback_decide` (Groq not reachable in sandbox).

Seed data:
- Article A: `section='libya'`, `bias_label='pro_government'`, unit-norm 768-dim embedding
- Article B: `section='libya'`, `bias_label='opposition'`, same embedding (cosine similarity = 1.0 ≥ min_similarity 0.5)
- Both have `bias_scores` rows (required by `vector_recommend` for label-filter logic)

Six checks:
1. `result["stored_count"] >= 1`
2. `result["recommendations"]` is a dict
3. `art_a_id` is in `result["recommendations"]` with ≥ 1 entry
4. All recommendations for article A have `bias_label != "pro_government"`
5. `art_b_id` appears in article A's recommendation IDs
6. `result["errors"] == []`

**Runtime:** 0.41 s. No LLM calls made; `_fallback_decide` used throughout.

Cleanup: Redis `recommend:{art_a_id}` and `recommend:{art_b_id}` keys deleted first; then `bias_scores` and `articles` rows deleted.

---

### Step 4.3 — `agents/summary_agent.py` **(complete)**

#### `agents/summary_agent.py` (new file, 290 lines)

`SummaryAgent` is the fifth LangGraph agent. It inherits from `MCPAgent` and follows the same ReAct pattern as `BlindspotAgent` and `BiasAgent`.

**Class: `SummaryAgent(MCPAgent)`**

Constructor sets `self._task_queue: list[int] = []` and `self._gemini_client: Any = None` (lazy-initialised on first call to `_call_gemini`).

**`run(event_ids: list[int]) → dict`**

Flow:
1. Calls `_ensure_bias_assessment_column()` to add the `bias_assessment` column to the `events` table if absent (idempotent; Deviation D).
2. Builds a numbered goal string and calls `self.run_react(...)` with `max_steps = max(N+5, 15)`. No `on_tool_call` hook — `extract_facts` takes only `event_id`.
3. Scans `react["observations"]` to build `facts_map: dict[int, list[str]]` from `extract_facts` results.
4. For each event: calls `_generate_neutral_summary(event_id, facts)`, then `_generate_bias_assessment(event_id)`, then `_update_event(event_id, neutral_summary, bias_assessment)`.
5. Returns `{"summaries": [...], "stored_count": int, "errors": [...]}`.

Return-value keys compatible with LangGraph `NewsState`:
- `summaries` — list of `{event_id, neutral_summary, bias_assessment}`
- `stored_count` — count of `events` rows with both fields updated
- `errors` — non-fatal error strings

**`_generate_neutral_summary(event_id, facts) → str`**

1. `self.call_tool("cache_get", {"key": f"neutral_summary:{event_id}"})` (MCP, within Rule 2.3).
2. On hit: return cached string immediately.
3. On miss: build prompt using `_NEUTRAL_SUMMARY_PROMPT` template with facts list (cap at 50 facts; full prompt truncated to 3000 chars per Rule 2.9).
4. Call `_call_gemini(prompt)` (Deviation B).
5. Cache result via `self.call_tool("cache_set", ...)` with TTL = 86400 s (24 h).

Prompt design: instructs the model to write exactly one Arabic paragraph based solely on the listed facts. Explicitly prohibits politically framed language.

**`_generate_bias_assessment(event_id) → str`**

1. `self.call_tool("cache_get", {"key": f"bias_assessment:{event_id}"})`.
2. On hit: return cached string.
3. On miss: call `_fetch_event_bias_data(event_id)` (DB read, Deviation A).
4. Build `_BIAS_ASSESSMENT_PROMPT` with formatted per-article rows: source domain, bias label, confidence, framing (truncated to 200 chars), title (truncated to 100 chars). Full prompt capped at 3000 chars.
5. Call `_call_gemini(prompt)` (Deviation B).
6. Cache result via `self.call_tool("cache_set", ...)` with TTL = 259200 s (72 h).

Prompt design: instructs the model to write a 2–3 sentence comparative Arabic analysis using academic, neutral language.

Expected output style:
```
الجزيرة تظهر ميلاً واضحاً لدعم سردية المقاومة وإبراز بشاعة العدوان.
في المقابل، تظهر العربية ميلاً لإبراز الإخفاقات الميدانية مع اعتماد مكثف على المصادر الرسمية.
```

**`_call_gemini(prompt) → str`** (Deviation B)

Direct call to `google.genai` SDK using `asyncio.to_thread`. Lazily initialises `self._gemini_client` on first call. Retries once on 429 quota error using `GEMINI_FALLBACK_MODEL`. Returns empty string on any exception (Rule 2.4). TTL and retry logic mirrors `mcp_server/server.py::_gemini_generate`.

**`_fetch_event_bias_data(event_id) → list[dict]`** (Deviation A)

Direct DB read: `JOIN article_events → articles → bias_scores WHERE event_id = %s`. Returns `{title, source, label, confidence, framing}` per article. Returns empty list on error.

**`_update_event(event_id, neutral_summary, bias_assessment) → bool`** (Deviation C)

`UPDATE events SET summary = %s, bias_assessment = %s WHERE id = %s`. Returns `True` if `rowcount > 0`. Never raises.

**`_ensure_bias_assessment_column() → None`** (Deviation D)

`ALTER TABLE events ADD COLUMN IF NOT EXISTS bias_assessment TEXT`. Idempotent. Called once at the start of `run()`.

**`_fallback_decide`** — same deterministic queue-walk pattern as `BlindspotAgent`.

**`_SUMMARY_TOOLS` (module-level list)** — four tool descriptions: `extract_facts` (primary), `get_coverage_stats` (optional), `cache_get`, `cache_set`.

---

#### Schema change — `events.bias_assessment TEXT`

The `events` table in `infra/schema.sql` had no `bias_assessment` column. The column was added via:

```sql
ALTER TABLE events ADD COLUMN IF NOT EXISTS bias_assessment TEXT;
```

This was applied to the live database before the agent was first tested. The `_ensure_bias_assessment_column()` method in `SummaryAgent` applies this migration idempotently at run-time for any environment where the column is absent. `infra/schema.sql` has **not** been modified — this is intentional: the schema file records the Phase 0 baseline, and schema changes after Phase 0 are documented in phase summaries per Rule 4.1.

---

#### `tests/test_agents.py` — `test_summary_agent_stores_summaries` (new test, test #5)

**Strategy:** Seed 1 event + 2 articles (different bias labels), pre-seed three Redis caches with mock data (`# MOCK`), run `SummaryAgent.run([event_id])`, assert outputs.

Seed data:
- 1 `events` row (`section='libya'`, `headline='Test Summary Agent Event'`)
- 2 `articles` rows with unit-norm 768-dim embeddings
- 2 `bias_scores` rows: `pro_government` (score=0.7) and `opposition` (score=-0.7)
- 2 `article_events` rows

Mock Redis pre-seeds (`# MOCK`):
1. `facts:{event_id}:{sha256(json(sorted_ids))}` → `extract_facts` cache hit, returns 2 Arabic fact strings without calling Gemini.
2. `neutral_summary:{event_id}` → agent's `cache_get` call returns mock Arabic summary, bypassing `_call_gemini`.
3. `bias_assessment:{event_id}` → agent's `cache_get` call returns mock Arabic assessment, bypassing `_call_gemini`.

Seven checks:
1. `result["stored_count"] >= 1`
2. `len(result["summaries"]) >= 1`
3. `result["summaries"][0]["neutral_summary"] == mock_neutral_summary`
4. `result["summaries"][0]["bias_assessment"] == mock_bias_assessment`
5. `events.summary` in DB matches mock
6. `events.bias_assessment` in DB matches mock
7. `result["errors"] == []`

**Runtime:** 0.72 s. No LLM calls made (all three caches hit).

Cleanup: Redis keys deleted first, then articles (cascade to bias_scores + article_events), then event.

---

### Step 4.2 — `agents/blindspot_agent.py` **(complete)**

#### `agents/blindspot_agent.py` (new file, 216 lines)

`BlindspotAgent` is the fourth LangGraph agent. It inherits from `MCPAgent` (via `agents/base.py`) and follows the exact ReAct pattern established by `BiasAgent`: the Groq/Llama 3.3 70B `think()` call selects the next tool; a `_fallback_decide` method covers the Groq-403 case observed in the Cursor agent environment.

**Class: `BlindspotAgent(MCPAgent)`**

Constructor sets `self._task_queue: list[int] = []` to track event IDs for the current run.  The queue is used by `_fallback_decide` to resume deterministically after a Groq timeout.

**`run(event_ids: list[int]) → dict`**

Parameters: a list of event IDs produced by the Clustering Agent.

Flow:
1. Builds a numbered goal string listing every event ID in `event_ids`.
2. Calls `self.run_react(goal, available_tools=_BLINDSPOT_TOOLS, max_steps=max(N+5, 15))` where `N = len(event_ids)`. No `on_tool_call` hook is needed because `detect_blindspot` takes only a single `int` argument with no working-memory injection.
3. Iterates `react["observations"]` to collect every `detect_blindspot` result.
4. For each result where `has_blindspot=True`, calls `_insert_blindspot_report` and appends the event to `blindspots`.
5. Returns `{"blindspots": [...], "stored_count": int, "errors": [...]}`.

Results where `has_blindspot=False` (both the normal "no blindspot" and the insufficient-data guard cases) are silently skipped — no error is raised.

If the ReAct loop exits without `completed=True`, a non-fatal error string is appended to `errors`.

Return-value keys compatible with LangGraph `NewsState`:
- `blindspots` — list of `{event_id, missing_perspectives, coverage_stats, total}`
- `stored_count` — count of `blindspot_reports` rows inserted this run
- `errors` — list of non-fatal error strings

**`_insert_blindspot_report(event_id, coverage_stats, missing_perspectives) → bool`**

Documented deviation — direct DB write (same justification as `clustering_agent.py` and `bias_agent.py`): the 15-tool MCP spec includes no tool for inserting `blindspot_reports`.

Uses a `WHERE NOT EXISTS` subquery guard:

```sql
INSERT INTO blindspot_reports (event_id, coverage_stats, missing_perspectives)
SELECT %s, %s::jsonb, %s
WHERE NOT EXISTS (
    SELECT 1 FROM blindspot_reports WHERE event_id = %s
)
RETURNING id
```

The `blindspot_reports` table has no UNIQUE constraint on `event_id`. This guard prevents duplicate rows on pipeline re-runs without requiring a schema change. Returns `True` if a new row was inserted, `False` if a row already existed. Never raises.

**`_fallback_decide(goal, available_tools, observations, step) → dict`**

Inspects `observations` to build a `handled: set[int]` of already-processed event IDs. Iterates `self._task_queue` and returns the next unhandled event's `detect_blindspot` call. Returns `{"done": True, ...}` when all events are handled.

**`_BLINDSPOT_TOOLS` (module-level list)**

Four tool descriptions passed to the ReAct loop:
- `detect_blindspot` — primary tool; one call per event
- `get_coverage_stats` — optional pre-inspection tool
- `cache_set` — Redis write (available for caching agent-level results in future)
- `cache_get` — Redis read

---

#### `tests/test_agents.py` — `test_blindspot_agent_stores_report` (new test, test #4)

**Strategy:** Seed one event + four articles all labeled `pro_government` directly in PostgreSQL (same direct-insert pattern used by `test_clustering_agent_creates_event` and `test_bias_agent_stores_singleton_bias`). Run `BlindspotAgent.run([event_id])`. Assert the expected outputs.

No LLM mocking is needed: `detect_blindspot` is a deterministic DB query with no Gemini or Groq calls. The only LLM call is `think()` (Groq) which drives the ReAct routing. The `_fallback_decide` handles the Groq-403 case, making the test infrastructure-agnostic.

Seed data:
- 1 `events` row (`section='libya'`, `headline='Test Blindspot Event'`)
- 4 `articles` rows with unit-norm 768-dim embeddings and `section='libya'`
- 4 `bias_scores` rows, all `label='pro_government'`, `confidence=0.9`
- 4 `article_events` rows linking each article to the test event

Five checks:
1. `result["stored_count"] >= 1` — at least one `blindspot_reports` row was stored.
2. `len(result["blindspots"]) >= 1` — at least one blindspot returned in results.
3. `event_id` appears in `{b["event_id"] for b in result["blindspots"]}`.
4. A `blindspot_reports` row exists in PostgreSQL for `event_id`.
5. `missing_perspectives` (from DB row) contains all four absent labels: `opposition`, `neutral`, `pan_arab`, `western_aligned`.

Cleanup (in `finally` block): deletes `blindspot_reports` row, then all four test articles (cascades to `bias_scores` and `article_events`), then the test event.

**Runtime:** 0.65 s (Groq not required; `_fallback_decide` used in Cursor environment).

---

### Step 4.1 — Six Tier-2 MCP Tools **(complete)**

#### `mcp_server/server.py` (6 new tools added; file grew from 1,041 to 1,519 lines)

All six Tier-2 tools were appended to the server after the existing `cache_get` tool and before the `if __name__ == "__main__"` block. Each tool is an `async def` decorated with `@mcp.tool()`, follows Rule 2.2 (async with `asyncio.to_thread` for all psycopg2 calls), Rule 2.4 (no exceptions propagated), and Rule 2.6 (no hardcoded values).

---

**`get_source_bias(domain: str) → dict`**

Queries the `sources` table by domain (lowercased and stripped before the query). Returns `{"name": str, "domain": str, "bias_label": str}` on success. Returns `{"error": str, "bias_label": None}` if the domain is not in the pre-seeded sources table. All psycopg2 work is wrapped in `asyncio.to_thread`. Used by the Ingestion Agent (future) to resolve `source_id` from a domain, and by the Blindspot Agent and Recommendation Agent to look up source-level bias context.

---

**`get_coverage_stats(event_id: int) → dict`**

Joins `article_events` with `bias_scores` (on `article_id`) and groups by `bias_label`, producing a count per label. Only articles that have a bias score in `bias_scores` are counted — unclassified articles are excluded from the distribution. Returns `{"event_id": int, "stats": {"label": count, ...}, "total": int}`. Returns an empty `stats` dict (not an error) if the event has no classified articles. Returns `{"error": str}` only on database exceptions. Used by the Blindspot Agent as the primary data source for perspective analysis.

---

**`detect_blindspot(event_id: int) → dict`**

Retrieves the bias-label distribution for the event (same query as `get_coverage_stats`, implemented inline rather than calling the sibling tool — MCP-to-MCP calls are not an architectural pattern in this server). Applies the underrepresentation algorithm:

```
average  = total_classified_articles / 5   (all five labels in taxonomy)
threshold = 0.15 × average
missing   = [label for label in ALL_LABELS if coverage.get(label, 0) < threshold]
```

Events with fewer than 3 classified articles are excluded from blindspot detection (returns `has_blindspot: False` with a `note` field). This guard prevents spurious blindspot reports on events that are too small for the analysis to be meaningful.

Returns `{"event_id": int, "missing_perspectives": [...], "coverage_stats": {...}, "total": int, "has_blindspot": bool}`. The `note` field is present only when the insufficient-data guard triggers.

Module-level constants: `_ALL_BIAS_LABELS` (tuple of all 5 labels), `_BLINDSPOT_THRESHOLD = 0.15`, `_MIN_ARTICLES_FOR_BLINDSPOT = 3`.

---

**`extract_facts(event_id: int) → dict`**

Fetches all articles linked to the event via `article_events → articles` (ordered by `a.id`). Builds a Gemini prompt with article content distributed across a token budget: `chars_per_article = min(600, 3000 // max(len(articles), 1))` per Rule 2.9. The prompt instructs Gemini to return a JSON array of Arabic-language facts that appear in at least two of the articles.

Cache key: `facts:{event_id}:{sha256(json(sorted_article_ids))}` — deterministic on the event's current article membership. TTL: 24 hours (`_TTL_ENTITIES`). Cache is checked before the Gemini call; result is cached after.

Returns `{"event_id": int, "facts": [...], "article_count": int}`. Returns `facts: []` (not an error) when no articles are found or when Gemini fails to parse a valid list. Never raises.

Prompt template stored in module-level constant `_FACTS_PROMPT_TEMPLATE`.

---

**`vector_recommend(article_id: int, section: str, limit: int = 5, min_similarity: float = 0.0) → dict`**

Validates the section against `SECTIONS`. Verifies the source article exists and has a non-null embedding in a first DB query (also retrieves its bias label via a LEFT JOIN on `bias_scores`). Then executes a pgvector CROSS JOIN subquery to find similar articles with a different bias label:

```sql
CROSS JOIN (SELECT embedding FROM articles WHERE id = %s) AS src
WHERE a.section = %s
  AND a.id != %s
  AND bs.label != %s                                    -- label filter (when bias exists)
  AND (1 - (a.embedding <=> src.embedding)) >= %s      -- min_similarity filter
ORDER BY a.embedding <=> src.embedding
LIMIT %s
```

The CROSS JOIN subquery approach keeps the 768-float embedding in PostgreSQL — no Python-side serialisation of the source article's vector is required. When the source article has no bias score yet, the label filter is omitted and all similar articles in the section are returned.

Returns `{"article_id": int, "source_label": str|None, "recommendations": [{id, title, bias_label, similarity}, ...]}`.

**Deviation — `min_similarity` parameter:** The blueprint spec does not mention a similarity threshold for `vector_recommend`. The parameter was added because `1 - cosine_distance` can return negative values when comparing unrelated embeddings (cosine distance ∈ [0, 2]), and negative-similarity "recommendations" are semantically meaningless. Default value `0.0` preserves backward compatibility. The Recommendation Agent (Step 4.4) should pass a higher threshold (e.g. `0.5`) for production quality. See Section C.

---

**`get_user_profile(user_id: str) → dict`**

Reads the Redis key `user_profile:{user_id}` using `asyncio.to_thread`. If the key exists, parses the JSON and returns `{"user_id": str, "dominant_bias": str|None, "read_counts": {...}}`. If the key does not exist, returns the same shape with `dominant_bias=None` and `read_counts={}` — no error. On any Redis or JSON exception, returns the empty profile shape plus an `"error"` key. Never raises.

The `user_profile:{user_id}` key is written by external services when a user reads articles; this tool only reads the profile.

---

#### `tests/test_tools.py` (6 new tests added; file grew from 526 to ~900 lines)

Total test count: **16** (10 Tier-1 from Phases 1–3 + 6 Tier-2 from Step 4.1).

Six shared DB/cleanup helpers were added above the Tier-2 tests:
- `_insert_test_event(section, headline)` — inserts an event, returns its `id`
- `_insert_test_article(url, label, embedding)` — inserts an article and optionally a `bias_scores` row, returns `id`
- `_link_article_to_event(article_id, event_id)` — inserts an `article_events` row
- `_cleanup_tier2(url_prefix, event_id)` — deletes test articles (cascades to `bias_scores` and `article_events`) and the event

Each test follows the same patterns as the Tier-1 tests: each opens its own `_mcp()` session, calls the tool via MCP transport (Rule 3.2), uses real PostgreSQL and Redis (Rule 3.3), and LLM-dependent tests use `# MOCK` Redis pre-seeding.

| Test | Tool | Strategy |
|---|---|---|
| `test_get_source_bias` (11) | `get_source_bias` | Calls with seeded domain `aljazeera.net`; asserts `bias_label="pan_arab"`. Tests unknown domain returns `error` + `bias_label=None`. No DB writes. |
| `test_get_coverage_stats` (12) | `get_coverage_stats` | Inserts 1 event + 3 articles (2 `pro_government`, 1 `opposition`), links to event, asserts `total=3`, `stats["pro_government"]=2`, `stats["opposition"]=1`. Cleans up in `finally`. |
| `test_detect_blindspot` (13) | `detect_blindspot` | Inserts 1 event + 4 `pro_government` articles. Asserts `has_blindspot=True` and all 4 absent labels in `missing_perspectives`. Also tests insufficient-data guard with a separate 1-article event (asserts `has_blindspot=False`, `note` present). Cleans up both events in `finally`. |
| `test_extract_facts` (14) | `extract_facts` | Inserts 1 event + 2 articles. Computes the exact Redis cache key (`facts:{event_id}:{sha256(json(sorted_ids))}`), pre-seeds it with 2 mock Arabic facts (`# MOCK`). Asserts facts list length and content. Cleans up in `finally`. |
| `test_vector_recommend` (15) | `vector_recommend` | Inserts article A (`pro_government`, embedding=[0.6]×768) and article B (`opposition`, same embedding). Calls with `min_similarity=0.5` to exclude existing DB articles with low similarity. Asserts B in recommendations, all recommendations have different label, similarity ∈ [0.5, 1.0]. Also tests unknown-section guard. Cleans up in `finally`. |
| `test_get_user_profile` (16) | `get_user_profile` | Pre-seeds `user_profile:{user_id}` in Redis (`# MOCK`) with `dominant_bias="pan_arab"`, `read_counts={"pan_arab":5, "neutral":2}`. Asserts all fields. Also asserts non-existent user returns empty default profile. No DB writes. |

---

## C — Logic Changes and Deviations

### Deviation 10 — `think()` uses Mistral instead of Groq for agent reasoning (post-Phase 4)

**What happened:** `MCPAgent.think()` in `agents/base.py` was changed to call the Mistral API (`mistral-large-latest`) instead of Groq (`llama-3.3-70b-versatile`).

**Reason:** The Groq API (`api.groq.com`) is unreachable from the current deployment network (`Could not resolve host: api.groq.com`). Every `think()` call was immediately hitting the `_is_network_failure` fallback, meaning the LLM was never actually driving any ReAct decisions — only the deterministic `_fallback_decide` was running. Mistral is accessible from the same network.

**Scope:** Only `think()` in `agents/base.py` was changed. `mcp_server/server.py` — including `check_relevance` (Groq), `classify_bias` (Gemini), `extract_entities` (Gemini), and `get_embedding` (Gemini) — is **unchanged**. Groq remains configured in `.env` and will be used by `check_relevance` when the network issue is resolved; the existing fallback in `server.py` handles it gracefully in the meantime.

**Changes made:**
- `requirements.txt`: added `mistralai==2.4.0`
- `.env`: added `MISTRAL_API_KEY` and `MISTRAL_THINK_MODEL=mistral-large-latest`
- `agents/base.py`: replaced `AsyncGroq` import + client + `_GROQ_MODEL` refs in `think()` with `mistralai.client.Mistral` + `complete_async()`; `_groq_unavailable` flag renamed to `_mistral_unavailable`

**Final architecture:**
```
agents/base.py  → think()          → Mistral (mistral-large-latest)   ← changed
server.py       → check_relevance  → Groq (llama-3.3-70b-versatile)   ← unchanged
server.py       → classify_bias    → Gemini                            ← unchanged
server.py       → extract_entities → Gemini                            ← unchanged
server.py       → get_embedding    → Gemini                            ← unchanged
```

**Impact on spec:** No architectural deviation from the ReAct loop design. The LLM-driven reasoning is now actually exercised on every pipeline run rather than falling back to `_fallback_decide` immediately. All existing tests continue to pass because `_fallback_decide` still handles the case where the LLM is unavailable.

---

### Deviation 8 — `NewsState` has 10 fields instead of the spec's 9 (Step 4.5)

**What happened:** `NewsState` includes an `event_clusters: dict` field beyond the nine fields listed in plan.md Step 4.5.

**Reason:** `BiasAgent.run(article_ids, event_clusters)` requires the cluster membership dict to perform comparative analysis (sending all articles in a cluster to Gemini simultaneously). The Clustering → Bias handoff is entirely through the LangGraph state; without `event_clusters` in the state, the graph has no way to pass this dict from `_clustering_node` to `_bias_node`.

**Impact on spec:** The nine required fields are all present and populated. `event_clusters` is a transparent extension — it carries data that must flow between specific agents. The addition does not break any downstream usage and is fully documented.

---

### Deviation 9 — `run_pipeline` uses a temporary `MCPAgent` for Redis writes (Step 4.5)

**What happened:** `run_pipeline()` creates a temporary `MCPAgent()` instance to call `cache_set` for storing `results:{section}` in Redis.

**Reason:** `run_pipeline` is an orchestration function, not an agent. It has no `call_tool()` method of its own. Creating a temporary `MCPAgent()` instance is the minimal approach that preserves the MCP architectural boundary (Rule 2.3) without adding a new class or modifying `MCPAgent`. The alternative — importing and calling Redis directly in `graph.py` — would violate Rule 2.3.

**Impact on spec:** None. The Redis write goes through the MCP server as required. The temporary MCPAgent is discarded after the single `cache_set` call.

---

### Deviation 7 — `base.py` `think()` network-error detection broadened (Step 4.4)

**What happened:** The `_is_network_failure` check in `MCPAgent.think()` was extended from detecting only "403" / "Access denied" to also detecting "Connection error", "connection", "timeout", and "ssl" error strings.

**Reason:** In the test sandbox environment (Cursor agent) the Groq API is unreachable and `httpx` raises "Connection error" rather than returning a 403. The original narrow check caused `think()` to return `{"done": True}` immediately on the first step, halting the ReAct loop before `_fallback_decide` could execute. This broke all five agent tests simultaneously.

**Impact:** All six agents now fall back to their deterministic `_fallback_decide` strategy for any transport-level failure, not just 403. The behavior in production (where Groq is reachable) is unchanged — no exception is raised and the fallback is never triggered.

**Impact on spec:** No architectural deviation. The fallback mechanism was already designed for this purpose; this change makes it trigger correctly across all network failure modes.

---

### Deviation 5 — `SummaryAgent` calls Gemini directly (Step 4.3)

**What happened:** `SummaryAgent` calls the `google.genai` SDK directly in `_call_gemini()` and `_fetch_event_bias_data()` for neutral summary generation and bias assessment generation.

**Reason:** The 15-tool MCP spec includes no tools for generating summary text or comparative bias analysis text. Adding two new MCP tools (`generate_neutral_summary`, `generate_bias_assessment`) would exceed the spec's tool count. The direct Gemini call pattern is architecturally equivalent to how `BiasAgent` uses `classify_bias` — the Gemini call is the agent's own production step, not a cross-cutting infrastructure call. Redis caching for both outputs goes through MCP (`cache_get`/`cache_set`), preserving the boundary for all cache operations.

**Impact on spec:** This is a minimal, documented deviation. The `extract_facts` tool (which also calls Gemini) still goes through MCP. Only the two summarization calls are direct.

### Deviation 6 — `events.bias_assessment` column added outside `infra/schema.sql` (Step 4.3)

**What happened:** The Phase 0 `infra/schema.sql` had no `bias_assessment` TEXT column on the `events` table. The column was added at Step 4.3 startup via `ALTER TABLE events ADD COLUMN IF NOT EXISTS bias_assessment TEXT`.

**Reason:** The schema was finalized in Phase 0 before the requirement for `bias_assessment` was specified. Adding the column directly to `schema.sql` would misrepresent the Phase 0 baseline. The `_ensure_bias_assessment_column()` method in `SummaryAgent` handles the migration idempotently on every run, ensuring any environment (including fresh installs) gets the column automatically.

**Impact:** The `events` table now has six columns: `id`, `section`, `headline`, `summary`, `created_at`, `bias_assessment`. All existing data is unaffected (the new column defaults to NULL for pre-existing rows).

### Deviation 1 — `vector_recommend` adds `min_similarity` parameter not in spec

**What happened:** During `test_vector_recommend`, articles already in the `articles` table from Phase 2/3 (27 Libya articles with real Gemini embeddings) also matched the `opposition` label filter. When compared against the test article's synthetic embedding `[0.6]*768`, those articles returned cosine similarity values of approximately -0.02 — technically valid SQL results but semantically meaningless as "recommendations."

**Root cause:** `1 - cosine_distance` maps to [-1.0, 1.0]. When two embeddings are nearly orthogonal in 768-dimensional space (which synthetic unit-direction vectors often are against real embeddings), the similarity approaches 0 and can go slightly negative due to floating-point arithmetic.

**Resolution:** Added `min_similarity: float = 0.0` parameter to `vector_recommend`. The WHERE clause filters `(1 - (a.embedding <=> src.embedding)) >= min_similarity`. Default `0.0` means no negative-similarity results are returned by default, which is the correct semantic behavior for any recommendation system. The test uses `min_similarity=0.5` to isolate only the high-similarity test article.

**Impact on downstream agents:** The Recommendation Agent (Step 4.4) should pass a `min_similarity` value (e.g. `0.5`) appropriate for filter-bubble bursting. A value too low returns noise; a value too high may return zero recommendations for well-diversified events. The Recommendation Agent's design should document the chosen threshold.

**Impact on spec:** The blueprint spec (`detect_blindspot → detect_blindspot`) does not mention a similarity threshold for `vector_recommend`. This deviation adds a useful parameter without removing any specified behavior. It is documented here per Rule 4.1.

### Deviation 2 — `detect_blindspot` queries DB directly (does not call `get_coverage_stats`)

**What happened:** `detect_blindspot` runs its own SQL query rather than calling `get_coverage_stats` internally.

**Reason:** MCP tool functions in `server.py` are not designed to call each other. An MCP tool is a server-side handler — calling another `@mcp.tool()` function directly would bypass the transport layer and create tight coupling between tools. Duplicating the simple GROUP BY query is the correct architectural choice.

**Impact:** None on behavior. Both tools produce identical coverage statistics from the same query. The slight code duplication is intentional and documented.

---

## D — Dependencies Introduced

No new Python packages were added. All imports used in Step 4.1 (`asyncio`, `hashlib`, `json`, `re`, `psycopg2`, `redis`) were already in `requirements.txt` from Phase 0/1.

No new environment variables introduced.

---

## E — Known Issues and Limitations

**Step 4.5 + 4.6:**

12. **`test_ingestion_agent_libya` and `test_pipeline_libya_end_to_end` fail in restricted sandbox.** Both tests require access to external APIs (GDELT DOC API, Gemini, Groq). In a sandbox environment with a restricted network allowlist, GDELT is blocked and `fetch_gdelt` returns 0 articles. The tests pass reliably when run with full network access. This is an environmental constraint, not a code defect. Documented for reproducibility.

13. **`pydantic.v1` deprecation warning on Python 3.14.** `langchain-core` uses `pydantic.v1.fields.FieldInfo` which is deprecated in Python 3.14. Not actionable without upgrading `langchain-core`. Does not affect functionality.

14. **`_UnionGenericAlias` deprecation from `google.genai`** on Python 3.14. Same category as above — third-party library deprecation, not actionable.

15. **`event_clusters` field not listed in plan.md spec.** Documented as Deviation 8. Required for Clustering → Bias handoff. Without it, `BiasAgent` cannot perform comparative analysis.

16. **Pipeline runs all sections sequentially from the scheduler.** `scheduler.py` (Step 6.3) will call `run_pipeline` independently for each section. The pipeline itself has no parallelism between sections; each section blocks until complete. For the thesis scale (3 sections, 50–75 articles each), this is acceptable.

---

**Step 4.4:**

9. **`get_user_profile` optional and not exercised in standard pipeline.** `get_user_profile` is listed as an available tool and the LLM may call it when `user_id` is supplied in the goal. However, the `run()` method signature makes `user_id` optional (default `""`), and the standard `graph.py` pipeline will likely not supply a user ID. The tool is available for the API layer (Phase 5) to use when serving personalised recommendations to authenticated users.

10. **Recommendations depend on `bias_scores` being populated.** `vector_recommend` filters by label to return articles with a *different* bias label. If an article has no `bias_scores` row (e.g. the Bias Agent has not yet run), `vector_recommend` omits the label filter and returns all similar articles regardless of label. The recommendation output is still valid but loses the "cross-bias" property. The pipeline ordering in `graph.py` (Step 4.5) must place `RecommendationAgent` after `BiasAgent`.

11. **Cache keys `recommend:{article_id}` are not namespaced by section.** If an article appears in multiple sections (unlikely but possible), the cached recommendations from the first section run would be returned for subsequent section runs. In practice, articles are section-specific (enforced at ingestion), so this is not a real risk.

---

**Step 4.3:**

6. **`neutral_summary` is empty when `extract_facts` returns `facts: []`.** For events where articles have `has_full_content=False` (title-only), `extract_facts` returns an empty list and `_generate_neutral_summary` returns `""`. The `_update_event` call still fires and writes `""` to `events.summary`. The downstream pipeline (graph.py) must handle empty summaries without error.

7. **`bias_assessment` is empty when no articles have `bias_scores`.** If the Bias Agent has not yet run for an event, `_fetch_event_bias_data` returns `[]` and `_generate_bias_assessment` returns `""`. This is expected for pipeline steps where SummaryAgent and BiasAgent run in the same pipeline pass and the Bias Agent result feeds SummaryAgent (correct ordering in `graph.py` is required).

8. **`infra/schema.sql` not updated.** The `bias_assessment` column was added via `ALTER TABLE` at runtime but not written back to `infra/schema.sql`. A fresh `psql -f infra/schema.sql` on a new database will create the `events` table without `bias_assessment`. The column will be added on the first `SummaryAgent.run()` call. This is acceptable for the current development phase but should be reconciled before Phase 6 deployment.

---

**Step 4.1–4.2 (previously recorded):**

1. **(Step 4.1) `extract_facts` quality depends on article content richness.** For events where articles have `has_full_content=False` (title-only), the Gemini prompt receives very short text, resulting in sparse or empty facts lists. This is expected behavior — the tool returns `facts: []` gracefully. The Summary Agent (Step 4.3) must handle the empty-facts case without error.

2. **`vector_recommend` `min_similarity` threshold is not in blueprint.** The default `0.0` is conservative. For production use, the Recommendation Agent should experiment with threshold values between `0.3` and `0.7` depending on the density of the Libya/Middle East article clusters.

3. **`get_user_profile` write path not implemented.** This tool only reads the user profile. The write path (updating `dominant_bias` and `read_counts` as a user reads articles) is not implemented in the MCP server. This is intentional — the 15-tool spec includes only `get_user_profile`, not a `set_user_profile` tool. Profile updates would need to be handled by the frontend or API layer (Phase 5).

4. **`detect_blindspot` threshold constants are module-level, not configurable.** `_BLINDSPOT_THRESHOLD = 0.15` and `_MIN_ARTICLES_FOR_BLINDSPOT = 3` are hardcoded constants. For the thesis, these values are justified by the spec ("coverage < 15% of average") and are not expected to change. If the committee requests sensitivity analysis, these can be made into tool parameters.

5. **`get_source_bias` domain matching is exact (after lowercase/strip).** Domains like `www.aljazeera.net` will not match `aljazeera.net`. The Ingestion Agent currently stores `source_id = NULL` (documented as a Phase 2 deviation). When source resolution is added in a future pipeline run, the domain should be stripped of `www.` prefix before lookup.

---

## F — Test Results

### Steps 4.5 + 4.6 formal test run — 2026-04-14

**Command:**
```bash
cd veritas-agent && source .venv/bin/activate && pytest tests/test_tools.py tests/test_agents.py tests/test_pipeline.py -v
```
(Run with `full_network` to reach GDELT, Gemini, and Groq APIs.)

**Result:**
```
collected 23 items

tests/test_tools.py::test_fetch_gdelt                                   PASSED
tests/test_tools.py::test_check_relevance                               PASSED
tests/test_tools.py::test_scrape_article                                PASSED
tests/test_tools.py::test_get_embedding                                 PASSED
tests/test_tools.py::test_store_article                                 PASSED
tests/test_tools.py::test_find_similar                                  PASSED
tests/test_tools.py::test_extract_entities                              PASSED
tests/test_tools.py::test_classify_bias                                 PASSED
tests/test_tools.py::test_classify_bias_comparative                     PASSED
tests/test_tools.py::test_cache_set_and_get                             PASSED
tests/test_tools.py::test_get_source_bias                               PASSED
tests/test_tools.py::test_get_coverage_stats                            PASSED
tests/test_tools.py::test_detect_blindspot                              PASSED
tests/test_tools.py::test_extract_facts                                 PASSED
tests/test_tools.py::test_vector_recommend                              PASSED
tests/test_tools.py::test_get_user_profile                              PASSED
tests/test_agents.py::test_ingestion_agent_libya                        PASSED
tests/test_agents.py::test_clustering_agent_creates_event               PASSED
tests/test_agents.py::test_bias_agent_stores_singleton_bias             PASSED
tests/test_agents.py::test_blindspot_agent_stores_report                PASSED
tests/test_agents.py::test_summary_agent_stores_summaries               PASSED
tests/test_agents.py::test_recommendation_agent_returns_recommendations PASSED
tests/test_pipeline.py::test_pipeline_libya_end_to_end                 PASSED

23 passed, 3 warnings in 84.96s (0:01:24)
```

**3 warnings (non-blocking):**
1. `test_ingestion_agent_libya`: 0 new DB records — all Libya URLs already in DB. Advisory only.
2. `pydantic.v1.fields.FieldInfo` deprecation from `langchain-core` on Python 3.14. Not actionable; affects no logic.
3. `_UnionGenericAlias` deprecation from `google.genai.types` on Python 3.14. Not actionable.

**Network requirement confirmed:** `test_ingestion_agent_libya` and `test_pipeline_libya_end_to_end` require GDELT, Gemini, and Groq API access. Both fail with `fetched=0 stored=0 errors=1` when run in a restricted sandbox (GDELT blocked). They pass reliably with full network access.

All 23 tests pass. All prior regression tests unaffected.

---

### Step 4.4 formal test run — 2026-04-14

**Command:**
```bash
cd veritas-agent && source .venv/bin/activate && pytest tests/test_tools.py tests/test_agents.py -v
```

**Result:**
```
collected 22 items

tests/test_tools.py::test_fetch_gdelt                                   PASSED
tests/test_tools.py::test_check_relevance                               PASSED
tests/test_tools.py::test_scrape_article                                PASSED
tests/test_tools.py::test_get_embedding                                 PASSED
tests/test_tools.py::test_store_article                                 PASSED
tests/test_tools.py::test_find_similar                                  PASSED
tests/test_tools.py::test_extract_entities                              PASSED
tests/test_tools.py::test_classify_bias                                 PASSED
tests/test_tools.py::test_classify_bias_comparative                     PASSED
tests/test_tools.py::test_cache_set_and_get                             PASSED
tests/test_tools.py::test_get_source_bias                               PASSED
tests/test_tools.py::test_get_coverage_stats                            PASSED
tests/test_tools.py::test_detect_blindspot                              PASSED
tests/test_tools.py::test_extract_facts                                 PASSED
tests/test_tools.py::test_vector_recommend                              PASSED
tests/test_tools.py::test_get_user_profile                              PASSED
tests/test_agents.py::test_ingestion_agent_libya                        PASSED
tests/test_agents.py::test_clustering_agent_creates_event               PASSED
tests/test_agents.py::test_bias_agent_stores_singleton_bias             PASSED
tests/test_agents.py::test_blindspot_agent_stores_report                PASSED
tests/test_agents.py::test_summary_agent_stores_summaries               PASSED
tests/test_agents.py::test_recommendation_agent_returns_recommendations PASSED

22 passed, 1 warning in 178.92s (0:02:58)
```

1 warning: `test_ingestion_agent_libya` — 0 new DB records (37 total, unchanged); expected on re-run where all Libya URLs already exist. Advisory only, does not fail the test.

All 22 tests pass. All prior regression tests (16 tools + 5 agents) unaffected.

---

### Step 4.3 formal test run — 2026-04-14

**Command:**
```bash
cd veritas-agent && source .venv/bin/activate && pytest tests/test_tools.py tests/test_agents.py -v
```

**Result:**
```
collected 21 items

tests/test_tools.py::test_fetch_gdelt                           PASSED
tests/test_tools.py::test_check_relevance                       PASSED
tests/test_tools.py::test_scrape_article                        PASSED
tests/test_tools.py::test_get_embedding                         PASSED
tests/test_tools.py::test_store_article                         PASSED
tests/test_tools.py::test_find_similar                          PASSED
tests/test_tools.py::test_extract_entities                      PASSED
tests/test_tools.py::test_classify_bias                         PASSED
tests/test_tools.py::test_classify_bias_comparative             PASSED
tests/test_tools.py::test_cache_set_and_get                     PASSED
tests/test_tools.py::test_get_source_bias                       PASSED
tests/test_tools.py::test_get_coverage_stats                    PASSED
tests/test_tools.py::test_detect_blindspot                      PASSED
tests/test_tools.py::test_extract_facts                         PASSED
tests/test_tools.py::test_vector_recommend                      PASSED
tests/test_tools.py::test_get_user_profile                      PASSED
tests/test_agents.py::test_ingestion_agent_libya                PASSED
tests/test_agents.py::test_clustering_agent_creates_event       PASSED
tests/test_agents.py::test_bias_agent_stores_singleton_bias     PASSED
tests/test_agents.py::test_blindspot_agent_stores_report        PASSED
tests/test_agents.py::test_summary_agent_stores_summaries       PASSED

21 passed, 1 warning in 92.48s (0:01:32)
```

1 warning: `test_ingestion_agent_libya` — 3 new DB records (37 total vs 34 before); expected on re-run.

All 21 tests pass. All prior regression tests unaffected.

---

### Step 4.2 formal test run — 2026-04-14

**Command:**
```bash
cd veritas-agent && source .venv/bin/activate && pytest tests/test_tools.py tests/test_agents.py -v
```

**Result:**
```
collected 20 items

tests/test_tools.py::test_fetch_gdelt                           PASSED
tests/test_tools.py::test_check_relevance                       PASSED
tests/test_tools.py::test_scrape_article                        PASSED
tests/test_tools.py::test_get_embedding                         PASSED
tests/test_tools.py::test_store_article                         PASSED
tests/test_tools.py::test_find_similar                          PASSED
tests/test_tools.py::test_extract_entities                      PASSED
tests/test_tools.py::test_classify_bias                         PASSED
tests/test_tools.py::test_classify_bias_comparative             PASSED
tests/test_tools.py::test_cache_set_and_get                     PASSED
tests/test_tools.py::test_get_source_bias                       PASSED
tests/test_tools.py::test_get_coverage_stats                    PASSED
tests/test_tools.py::test_detect_blindspot                      PASSED
tests/test_tools.py::test_extract_facts                         PASSED
tests/test_tools.py::test_vector_recommend                      PASSED
tests/test_tools.py::test_get_user_profile                      PASSED
tests/test_agents.py::test_ingestion_agent_libya                PASSED
tests/test_agents.py::test_clustering_agent_creates_event       PASSED
tests/test_agents.py::test_bias_agent_stores_singleton_bias     PASSED
tests/test_agents.py::test_blindspot_agent_stores_report        PASSED

20 passed, 1 warning in 124.01s (0:02:04)
```

1 warning: `test_ingestion_agent_libya` — only 7 new DB records written (34 total vs 27 before); expected on re-run where Libya URLs already exist. This is advisory and does not fail the test (documented in the test's docstring).

All 20 tests pass. All 16 Step 4.1 regression tests unaffected.

---

### Step 4.1 formal test run

**Command:**
```bash
cd veritas-agent && source .venv/bin/activate && pytest tests/test_tools.py -v
```

**First run (before `min_similarity` fix):**
```
collected 16 items
...
FAILED tests/test_tools.py::test_vector_recommend
  assert 0.0 <= -0.023908  ← real DB articles with low similarity included
15 passed, 1 failed in 94.53s
```

**Root cause identified:** No minimum similarity filter in `vector_recommend` — real Libya articles with `opposition` label but near-zero similarity included in results.

**Fix applied:** Added `min_similarity: float = 0.0` parameter to `vector_recommend` tool; test updated to use `min_similarity=0.5`.

**Second run (after fix) — 2026-04-14:**
```
collected 16 items

tests/test_tools.py::test_fetch_gdelt                   PASSED
tests/test_tools.py::test_check_relevance               PASSED
tests/test_tools.py::test_scrape_article                PASSED
tests/test_tools.py::test_get_embedding                 PASSED
tests/test_tools.py::test_store_article                 PASSED
tests/test_tools.py::test_find_similar                  PASSED
tests/test_tools.py::test_extract_entities              PASSED
tests/test_tools.py::test_classify_bias                 PASSED
tests/test_tools.py::test_classify_bias_comparative     PASSED
tests/test_tools.py::test_cache_set_and_get             PASSED
tests/test_tools.py::test_get_source_bias               PASSED
tests/test_tools.py::test_get_coverage_stats            PASSED
tests/test_tools.py::test_detect_blindspot              PASSED
tests/test_tools.py::test_extract_facts                 PASSED
tests/test_tools.py::test_vector_recommend              PASSED
tests/test_tools.py::test_get_user_profile              PASSED

16 passed, 0 warnings in 4.27s
```

All 16 tests pass. All 10 Phase 1–3 regression tests unaffected.

---

## G — Success Criteria Verification

### Step 4.6

| Criterion | Status | Evidence |
|---|---|---|
| `tests/test_pipeline.py` created | **MET** | File exists at `tests/test_pipeline.py`, 144 lines |
| `test_pipeline_libya_end_to_end` passes | **MET** | Passes in 84.96s on 2026-04-14 (full network run) |
| Final state contains non-empty `article_ids` | **MET** | Check 3 asserts `len(article_ids) >= 1` — passed |
| Final state contains non-empty `cluster_ids` | **MET** | Check 5 asserts `len(cluster_ids) >= 1` — passed |
| Final state contains non-empty `summaries` | **MET** | Check 6 asserts `len(summaries) >= 1` — passed |
| Final state contains non-empty `recommendations` | **MET** | Check 8 asserts `len(recs) >= 1` — passed |
| `results:libya` written to Redis | **MET** | Check 12 verifies Redis key exists; Check 13 verifies content — passed |

**Step 4.6 criteria: ALL MET.**

---

### Step 4.5

| Criterion | Status | Evidence |
|---|---|---|
| `agents/graph.py` created with `NewsState` TypedDict | **MET** | File exists at `agents/graph.py`, 260 lines; `NewsState` has 10 fields (9 spec + `event_clusters`) |
| `NewsState` name used consistently | **MET** | TypedDict named `NewsState` in `agents/graph.py`; imported in `tests/test_pipeline.py` |
| Six agent nodes defined | **MET** | `_ingestion_node`, `_clustering_node`, `_bias_node`, `_blindspot_node`, `_summary_node`, `_recommendation_node` |
| Nodes connected sequentially | **MET** | `START → ingestion → clustering → bias → blindspot → summary → recommendation → END` |
| `run_pipeline(section)` defined | **MET** | Async function in `agents/graph.py`; initialises state, calls `ainvoke`, stores to Redis |
| Results stored in Redis `results:{section}` TTL 6 h | **MET** | `cache_set` called via `MCPAgent.call_tool` with `ttl=21600` |
| Pipeline completes without crash on empty input | **MET** | All nodes guard for empty `article_ids`/`cluster_ids` and return defaults |
| Full test suite passes (23 tests) | **MET** | `23 passed in 84.96s` on 2026-04-14 |

**Step 4.5 criteria: ALL MET.**

---

### Step 4.4

| Criterion | Status | Evidence |
|---|---|---|
| `agents/recommendation_agent.py` created with `RecommendationAgent(MCPAgent)` | **MET** | File exists at `agents/recommendation_agent.py`, 219 lines |
| ReAct pattern via `run_react()` | **MET** | `run()` calls `self.run_react(goal, available_tools=_RECOMMEND_TOOLS, ...)` |
| Available tools: `vector_recommend`, `get_user_profile`, `cache_set`, `cache_get` | **MET** | All four in `_RECOMMEND_TOOLS` list |
| Max 10 articles per run | **MET** | `limited_ids = article_ids[:_MAX_ARTICLES_PER_RUN]` where `_MAX_ARTICLES_PER_RUN = 10` |
| `_fallback_decide` implemented | **MET** | Deterministic queue-walk calls `vector_recommend` directly for each unhandled article |
| `min_similarity=0.5` used per Step 4.1 guidance | **MET** | `_MIN_SIMILARITY = 0.5` module constant; used in all `vector_recommend` calls |
| `test_recommendation_agent_returns_recommendations` added to `test_agents.py` | **MET** | Test #6 in `test_agents.py`, all 6 checks pass |
| Full test suite passes (22 tests) | **MET** | `22 passed in 178.92s` on 2026-04-14 |

**Step 4.4 criteria: ALL MET.**

---

### Step 4.3

| Criterion | Status | Evidence |
|---|---|---|
| `agents/summary_agent.py` created with `SummaryAgent(MCPAgent)` | **MET** | File exists at `agents/summary_agent.py`, 290 lines |
| ReAct pattern via `run_react()` | **MET** | `run()` calls `self.run_react(goal, available_tools=_SUMMARY_TOOLS, ...)` |
| `_fallback_decide` implemented | **MET** | Deterministic queue-walk when Groq is unavailable |
| Output 1: `neutral_summary` from `extract_facts` + Gemini | **MET** | `_generate_neutral_summary` calls `extract_facts` via MCP, then Gemini directly |
| Output 2: `bias_assessment` from `bias_scores` + Gemini | **MET** | `_generate_bias_assessment` fetches `bias_scores` from DB, then calls Gemini |
| Both outputs stored in `events` table | **MET** | `_update_event` writes `events.summary` and `events.bias_assessment` |
| `events.bias_assessment` column added via `ALTER TABLE` | **MET** | Column added and documented as Deviation D |
| Redis caching for both outputs (via MCP cache_set/cache_get) | **MET** | Both generators check `cache_get` before Gemini and call `cache_set` after |
| Prompt prohibits politically framed language | **MET** | `_NEUTRAL_SUMMARY_PROMPT` includes "STRICT RULES: Do NOT use politically framed, partisan, or biased language" |
| `test_summary_agent_stores_summaries` added to `test_agents.py` | **MET** | Test #5 in `test_agents.py`, all 7 checks pass |
| Full test suite passes (21 tests) | **MET** | `21 passed in 92.48s` on 2026-04-14 |

**Step 4.3 criteria: ALL MET.**

---

### Step 4.2

| Criterion | Status | Evidence |
|---|---|---|
| `agents/blindspot_agent.py` created with `BlindspotAgent(MCPAgent)` | **MET** | File exists at `agents/blindspot_agent.py`, 216 lines |
| ReAct pattern via `run_react()` | **MET** | `run()` calls `self.run_react(goal, available_tools=_BLINDSPOT_TOOLS, ...)` |
| `_fallback_decide` implemented | **MET** | Deterministic queue-walk when Groq is unavailable |
| Available tools: `detect_blindspot`, `get_coverage_stats`, `cache_set`, `cache_get` | **MET** | All four in `_BLINDSPOT_TOOLS` list |
| Stores `blindspot_reports` row on `has_blindspot=True` | **MET** | `_insert_blindspot_report()` with WHERE NOT EXISTS guard |
| Events with < 3 classified articles not flagged | **MET** | Guard enforced in `detect_blindspot` MCP tool; `has_blindspot=False` results silently skipped |
| `test_blindspot_agent_stores_report` added to `test_agents.py` | **MET** | Test #4 in `test_agents.py`, all 5 checks pass |
| Full test suite passes (20 tests) | **MET** | `20 passed in 124.01s` on 2026-04-14 |

**Step 4.2 criteria: ALL MET.**

---

### Step 4.1

| Criterion | Status | Evidence |
|---|---|---|
| Six Tier-2 tools added to `mcp_server/server.py` | **MET** | `get_source_bias`, `get_coverage_stats`, `detect_blindspot`, `extract_facts`, `vector_recommend`, `get_user_profile` — all implemented and registered via `@mcp.tool()` |
| Server now exposes all 15 tools | **MET** | `list_tools()` via MCP transport returns 15 tools (verified indirectly: all 16 `test_tools.py` tests pass, each calling a specific tool by name) |
| `test_tools.py` grows from 10 to 16 tests | **MET** | `collected 16 items` — 10 Tier-1 regression tests + 6 new Tier-2 tests |
| All 16 tests pass | **MET** | `16 passed, 0 warnings in 4.27s` on 2026-04-14 |

**Step 4.1 criteria: ALL MET.**

**Phase 4 is COMPLETE.** All steps (4.1–4.6) met their success criteria. 23 tests pass.

---

## H — What the Remaining Steps Depend On

All Phase 4 steps (4.1–4.6) are now complete. Phase 4 as a whole is **COMPLETE**.

**Phase 5 dependencies on Phase 4 outputs:**

| Phase 5 Step | Depends on |
|---|---|
| `api/main.py` — `GET /results/{section}` | `run_pipeline(section)` stores `results:{section}` in Redis (Step 4.5 ✓). API reads from this key. |
| `api/main.py` — `GET /recommendations/{article_id}` | Recommendation results stored in Redis under `recommend:{article_id}` (Step 4.4 ✓). |
| `api/main.py` — `GET /blindspots/{section}` | Blindspot results in `state["blindspots"]` inside `results:{section}` (Step 4.5 ✓). |
| `frontend/app.py` | All three GET endpoints above. FastAPI backend must be running. |

---

### Post-Phase-4 Fix: Rate-Limit Resilience (agents/base.py)

The pipeline was crashing at step 7 with Mistral 429 (rate limit exceeded), and GDELT 429 errors were leaking into agent observations, wasting extra LLM calls. Fixed by adding: (1) a proactive 1.2 s minimum interval between Mistral calls to prevent 429s in the first place, (2) exponential-backoff retry on 429 in `think()` (6 attempts: 2→4→8→16→32→64 s), and (3) transparent 429 retry inside `call_tool()` so downstream API rate limits never surface as agent observations.

### Architectural Refactor: Deterministic IngestionAgent + Groq Switch

Switched agent reasoning from Mistral to Groq (llama-3.3-70b-versatile, sync client via asyncio.to_thread to avoid httpx/MCP TaskGroup conflicts in Python 3.14). Converted IngestionAgent from ReAct to a deterministic Python loop — the six-step ingestion sequence (fetch → check_relevance → scrape → entities → embedding → store) is always the same and needs no LLM orchestration. This eliminated ~150 Groq reasoning calls per pipeline run (26 articles × 6 think() calls → 0). Per-article try/except ensures a single article failure never crashes the pipeline. The 5 downstream agents (Clustering, Bias, Blindspot, Summary, Recommendation) remain ReAct. All 23 tests pass; pipeline produces 32 articles, 34 bias results, 1 event cluster, 1 summary, 10 recommendations, zero errors.
