# Phase 3 Summary — Clustering Agent and Bias Agent

## A — Phase Identification

| Field | Value |
|---|---|
| Phase Number | 3 |
| Phase Title | Clustering Agent and Bias Agent |
| Start Date | 2026-04-12 |
| Completion Date | 2026-04-12 |

---

## B — What Was Implemented

### `agents/clustering_agent.py` (Step 3.1)

- **`ClusteringAgent(MCPAgent)`** with **`run(section, article_ids)`**.
- Loads article rows from PostgreSQL (`id`, `published_at`, `entities`, `embedding` via `pgvector` decoding).
- For each unclustered article, calls MCP tool **`find_similar`** (Conditions 1–2: 72h window + cosine ≥ 0.82 at SQL level).
- Applies **Condition 3** in Python: **`_entity_overlap()`** ≥ 2 shared entities across `people`, `locations`, `organizations` (normalized strip + lower-case).
- Builds clusters (≥2 members), inserts **`events`**, links **`article_events`** with `ON CONFLICT DO NOTHING` on the join where applicable; event insert uses direct SQL.
- Returns **`event_ids`**, **`event_clusters`**, counts, **`errors`**.
- **Embedding fix:** uses **`numpy.ndarray.tolist()`** so MCP JSON serialization of `find_similar` arguments does not fail on `numpy.float32` (using `list(ndarray)` preserved non-JSON-serializable scalars).

### `agents/bias_agent.py` (Step 3.2)

- **`BiasAgent(MCPAgent)`** with **`run(article_ids, event_clusters)`**.
- **ReAct** loop with **`_inject_bias_articles`**: LLM passes **`target_article_id`** only; hook injects full **`articles`** list for comparative (multi-member events) or singleton fallback.
- Multi-member **event_clusters** only use comparative mode when **≥2** article IDs; single-ID entries are treated as **singletons** for bias (aligned with singleton fallback semantics).
- **`classify_single(text)`** for Phase 6 evaluation: single synthetic payload, text truncated to 3000 chars, **`call_tool("classify_bias", ...)`**.
- **`_fetch_bias_payloads`**: reads `id`, `title`, `content`, `url`**; **`source`** = URL host via **`urlparse`**.
- **`_insert_bias_score`**: **`INSERT INTO bias_scores ... ON CONFLICT (article_id) DO NOTHING`**.
- **`_fallback_decide`**: deterministic queue walk when Groq is unavailable (same family of workaround as ingestion).
- Returns **`bias_results`**, **`stored_count`**, **`errors`**.

### `tests/test_agents.py` (Step 3.3)

- **`test_clustering_agent_creates_event`**: seeds two Libya articles (72h window, identical embeddings, ≥2 entity overlap), runs **`ClusteringAgent`**, asserts **`events`** + **`article_events`**, cleans up.
- **`test_bias_agent_stores_singleton_bias`**: seeds one article, pre-seeds Redis **`bias_comparative:`** cache (**`# MOCK`**) for **`classify_bias`**, runs **`BiasAgent.run([id], {})`**, asserts **`bias_results`** and **`bias_scores`** row, cleans up.

### `tests/test_tools.py`

- No code changes in Phase 3 closure; included in full-suite regression run (10 Tier-1 tool tests + comparative **`classify_bias`** test).

---

## C — Logic Changes and Deviations

1. **Clustering — no `run_react` loop (Deviation C in `clustering_agent.py` docstring):** Clustering is deterministic; **`find_similar`** is invoked via **`call_tool()`** in a fixed loop, not Groq-driven ReAct. Reason: no meaningful LLM routing for this algorithm; MCP similarity search remains the mandated path for Conditions 1–2.

2. **Clustering + Bias — direct PostgreSQL for reads/writes not exposed as MCP tools:** **`events`**, **`article_events`**, full article fields for bias, and **`bias_scores`** inserts use **`psycopg2`** inside **`asyncio.to_thread`**, matching the pragmatic pattern documented in Phase 3 for gaps in the 15-tool surface. MCP is still used for **`find_similar`** and **`classify_bias`**.

3. **`plan.md` Step 3.2 footnote on `classify_single`:** The line *"NOT implemented in the current change"* applied to **Step 3.1**; **`classify_single`** **was** implemented in Step 3.2 as specified.

4. **Automated tests vs. thesis “manual” criteria:** Integration tests use **controlled seed data** and (for bias) **Redis `# MOCK`** to avoid exhausting LLM quotas. They do **not** substitute for human review of 10 real Libya clusters or a full-section bias batch run.

---

## D — Dependencies Introduced

None. No new packages were added to **`requirements.txt`** during Phase 3.

---

## E — Known Issues and Limitations

1. **Manual clustering accuracy (Libya):** Phase 2 data often has **sparse `entities`** on many articles; real Libya runs may form **fewer** multi-article clusters than a thesis “10-cluster” manual review expects. Clustering behavior is **correct on seeded tests**; field accuracy depends on **`extract_entities`** quality and GDELT overlap.

2. **Bias Agent integration test** uses **Redis MOCK** for **`classify_bias`** — live Gemini comparative runs are **not** exercised in **`test_bias_agent_stores_singleton_bias`** (Rule 3.3 LLM exception).

3. **ReAct flakiness:** If Groq returns malformed tool JSON, steps may be wasted; **`_fallback_decide`** mitigates **403**/unavailable Groq. **`max_steps`** cap could still truncate very large batches (unlikely at current article counts).

4. **`bias_scores` re-runs:** **`ON CONFLICT DO NOTHING`** means a second pipeline pass does **not** refresh existing bias rows — by design (Rule 2.8).

5. **`test_pipeline.py`** does not exist yet (Phase 4); full suite is **`test_tools.py` + `test_agents.py`** only.

---

## F — Test Results

**Command (Phase 3 closure):**

```bash
cd veritas-agent && source .venv/bin/activate && pytest tests/ -v --tb=short
```

**Output (representative run, 2026-04-12):**

```
collected 13 items

tests/test_agents.py::test_ingestion_agent_libya PASSED
tests/test_agents.py::test_clustering_agent_creates_event PASSED
tests/test_agents.py::test_bias_agent_stores_singleton_bias PASSED
tests/test_tools.py::test_fetch_gdelt PASSED
tests/test_tools.py::test_check_relevance PASSED
tests/test_tools.py::test_scrape_article PASSED
tests/test_tools.py::test_get_embedding PASSED
tests/test_tools.py::test_store_article PASSED
tests/test_tools.py::test_find_similar PASSED
tests/test_tools.py::test_extract_entities PASSED
tests/test_tools.py::test_classify_bias PASSED
tests/test_tools.py::test_classify_bias_comparative PASSED
tests/test_tools.py::test_cache_set_and_get PASSED

13 passed, 1 warning in ~99s
```

**Warning:** `test_ingestion_agent_libya` emits a **UserWarning** when **0 new** article rows are written (URL deduplication on re-run) — expected, not a failure.

**Prerequisites verified for the run:** PostgreSQL, Redis, MCP server on **`http://127.0.0.1:8000/mcp`**.

---

## G — Success Criteria Verification

Criteria copied from **`plan.md`** (Phase 3 section).

| Criterion | Status | Explanation |
|---|---|---|
| **Goal:** Articles are grouped into events, and each article's political bias is classified and stored. | **MET (architecture)** | **`ClusteringAgent`** persists **`events`/`article_events`**; **`BiasAgent`** persists **`bias_scores`** via **`classify_bias`** + DB insert. |
| **Step 3.3:** Tests in **`tests/test_agents.py`** for **both** Clustering and Bias agents; clustering test creates **≥1** event from seeded articles; bias test ensures a **bias_scores** record for the processed article. | **MET** | **`test_clustering_agent_creates_event`** and **`test_bias_agent_stores_singleton_bias`** satisfy the automated Step 3.3 requirements. |
| **Phase 3 Success Criterion 1:** *Manual review of 10 clusters from the Libya section shows grouping accuracy above 70%.* | **NOT MET (manual review not performed)** | No human annotator session was run in this closure. Automated tests only validate **mechanics** on **seeded** pairs. **Recommendation:** perform the manual 10-cluster review before thesis defense or treat as a separate QA milestone. |
| **Phase 3 Success Criterion 2:** *The **`bias_scores`** table is populated with **valid labels** for **all processed articles**.* | **PARTIALLY MET** | Valid labels + insert path are proven under test for the **singleton** path with **MOCK** cache. A **full Libya batch** processed by **`BiasAgent.run`** over **all** ingested article IDs was **not** executed as part of CI. |

**Phase 3 gate (development):** **Automated success criteria (Step 3.3 + regression suite) are MET.** **Narrative / manual criteria** above are **NOT fully MET** without additional human or batch verification — documented here per Rule 4.1.

---

## H — What the Next Phase Depends On

Phase 4 (**Remaining Agents + Tier 2 MCP tools + LangGraph pipeline**) depends on:

- **`ClusteringAgent.run`** → **`event_ids`**, **`event_clusters`** for **`NewsState`** and downstream agents.
- **`BiasAgent.run`** → **`bias_results`** and populated **`bias_scores`** for blind-spot logic, summaries, recommendations, and API/dashboard reads.
- **`classify_single`** for the future **`evaluation/evaluate.py`** script (Phase 6).
- Stable **`tests/test_tools.py`** + **`tests/test_agents.py`** as regression baselines before adding **`tests/test_pipeline.py`**.
