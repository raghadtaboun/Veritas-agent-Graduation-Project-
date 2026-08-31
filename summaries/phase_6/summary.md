# Phase 6 Summary — Evaluation Harnesses (Steps 6.2 & 6.3)

> **Scope of this session.** This document records the build of the two Phase 6
> evaluation deliverables specified in `plan.md`: the bias model-comparison
> harness (Step 6.2, extended to a multi-size Gemma comparison) and the
> clustering evaluation harness (Step 6.3). Phase 6 as a whole (Steps 6.1–6.5)
> is **not** closed by this session — Step 6.1 (dataset) was completed by the
> annotator beforehand, and Steps 6.4 (final testing pass) and 6.5 (final docs)
> remain. Per `agent.md` Rule 6.2, the partial completion is documented honestly
> below. This file is a new per-phase summary; no earlier summary or
> `progress_log.md` was modified.

---

## Section A — Phase Identification

- **Phase:** 6 (Evaluation and Documentation) — Steps 6.2 and 6.3 only
- **Title:** Evaluation Harnesses — Bias Model Comparison + Clustering Test
- **Start date:** 2026-06-09
- **Completion date:** 2026-06-09 (harnesses built and verified; full live runs
  pending Gemini quota — see Section E)

---

## Section B — What Was Implemented

Two **standalone** research scripts under `evaluation/`. Neither touches the
production pipeline, the MCP server, the live DB, Redis, or any
agent/API/frontend code. Both reuse production logic **by import only**.

### `evaluation/evaluate_bias.py` — Bias Model Comparison (Step 6.2)

- Runs the bias-classification task across **four Gemma model sizes** over the
  60-article human-labelled dataset and compares each model's predictions to the
  human labels.

  | Label      | Backend                | Model identifier |
  | ---------- | ---------------------- | ---------------- |
  | `gemma-4b`  | Ollama (local HTTP)   | `gemma4:e4b-mlx` |
  | `gemma-12b` | Ollama (local HTTP)   | `gemma4:12b-mlx` |
  | `gemma-26b` | Ollama (local HTTP)   | `gemma4:26b`     |
  | `gemma-31b` | Google AI Studio API  | `GEMMA_31B_AISTUDIO_MODEL` constant (default `gemma-4-31b-it`, annotator-editable) |

- **Prompt reuse:** imports `_BIAS_SINGLE_PROMPT_TEMPLATE` (the exact template
  `BiasAgent.classify_single` uses), plus the production parsing helper
  `_strip_code_fences`, the valid-label set `_VALID_BIAS_LABELS`, the char
  budgets `_MAX_SINGLE_CHARS`/`_MAX_TITLE_CHARS`, and `_source_from_url` — all
  from `agents/bias_agent.py`. The prompt is **not** re-implemented.
- **Backends:** Ollama is called over its HTTP API via `httpx`
  (`POST /api/generate`, `format:"json"`, `think:false`, temperature 0); AI
  Studio via the `google.genai` client. Each call has a per-call timeout and
  transient-error retry; a single failure is recorded (`predicted_label =
  "parse_error"`), never fatal (Rule 2.4).
- **Label salvage:** valid JSON is parsed first; if a model emits malformed JSON
  (e.g. unescaped quotes inside the Arabic `framing`), a regex salvages an
  unambiguous label from one of the five valid labels. Only when no valid label
  can be found is the row recorded as `parse_error` (counts as incorrect).
- **Metrics (manual, no new dependency):** 5×5 confusion matrix, per-label
  precision/recall/F1, macro-averaged F1, and overall accuracy.
- **Outputs** (`evaluation/results/`): `bias_predictions.json` (every
  model/article/human/predicted/raw row, auditable) and `bias_comparison.json`
  (per-model metrics summary). Both are **merged by model** on each run so the
  annotator can run one model at a time without losing prior results.
- **CLI:** `--models` (comma-separated subset), `--limit N`, `--timeout`,
  `--retries`. Prints the comparison table to stdout in the `plan.md` format.

### `evaluation/evaluate_clustering.py` — Clustering Test (Step 6.3)

- Tests whether the production clustering logic groups same-event articles
  (`evaluation/clustering_ground_truth.json` — 10 groups, 23 articles) into one
  cluster despite differing bias labels and, for groups 4/6/7, different titles.
- **Read-only design (the key constraint):** `ClusteringAgent.run()` cannot be
  invoked directly because it reads via `get_articles`/`find_similar` and
  **writes** to the `events`/`article_events` production tables, and the dataset
  IDs are dataset-local (not DB serial IDs). The harness therefore reproduces
  the clustering **orchestration in-memory** while reusing the production
  **decision logic by import**:
  - Embeddings via `agents.llm_client.gemini_embed` (the exact path
    `MCPAgent.call_gemini_embedding` uses).
  - Entities via the production `_ENTITY_PROMPT_TEMPLATE` +
    `IngestionAgent._normalise_entities` through
    `gemini_generate_with_fallback(task_type="entities")`.
  - Conditions reused verbatim from `agents/clustering_agent.py`:
    `_SIMILARITY_THRESHOLD` (0.82), `_ENTITY_OVERLAP_MIN` (2), `_entity_overlap`,
    `_FIND_SIMILAR_LIMIT` (20), the same `section` filter, the 72-hour window,
    and the same greedy in-order assignment. Cosine is computed exactly in numpy.
- **Scoring:** per-group `grouped` / `partial` / `split`; groups correctly
  grouped out of 10; the 3 hard groups (4, 6, 7) summarized separately; pairwise
  precision/recall/F1 over same-event pairs across the 23 grouped articles.
- **Output** (`evaluation/results/clustering_results.json`): per-group outcome,
  the cluster assignment of every one of the 60 articles, overall + hard-group +
  pairwise metrics, and preprocessing diagnostics.
- A clearly-namespaced local cache (`evaluation/results/.preprocess_cache.json`)
  speeds re-runs. This is **not** Redis and **not** the production DB.

---

## Section C — Logic Changes and Deviations

- **Filenames.** `plan.md` Step 6.2 names the bias script `evaluation/evaluate.py`.
  Per the session instruction, two purpose-named scripts were created
  (`evaluate_bias.py`, `evaluate_clustering.py`). This matches `plan.md`'s
  Step 6.3 name `evaluate_clustering.py` and is clearer for a two-harness build.
- **sklearn → manual metrics.** `plan.md` 6.2 says "using sklearn"; the session
  constraint forbids new heavyweight dependencies. Metrics are computed manually
  (exact for a 5-class problem); no dependency was added. Approved during pre-work.
- **Ollama via HTTP.** Ollama is accessed through its HTTP API with `httpx`
  (already pinned) rather than adding the `ollama` client. Approved during pre-work.
- **Clustering reuse vs. re-implementation.** To honor the read-only constraint,
  the clustering *decision logic* is reused by import (thresholds, entity-overlap
  function, entity prompt, embedding path) while the *orchestration* is reproduced
  in-memory. Cosine is computed exactly (numpy) versus pgvector's approximate
  HNSW search — a stricter, more correct comparison for evaluation. Surfaced and
  approved in pre-work as the way to avoid polluting production tables.
- No production file (`agents/`, `mcp_server/`, `api/`, `frontend/`, `config/`)
  and no DB row was modified. `evaluation/dataset.json` is byte-identical to its
  session-start state (md5 verified before/after).

## Section D — Dependencies Introduced

None. The harnesses use only already-pinned packages (`httpx`, `numpy`,
`google-genai`, `python-dotenv`). No additions to `requirements.txt`.

## Section E — Known Issues and Limitations

- **Gemini free-tier quota exhaustion during the live clustering run.**
  **RESOLVED (see Sections M and N).** The original concern was that a single
  free-tier key exhausted mid-run (the heavier entity model first). Multi-key
  rotation via the existing `GeminiKeyPool` (Section M) plus the local preprocess
  cache allowed a **full 60/60 run to complete**; the authoritative results are in
  Section N (baseline P=1.0, R=0.7500, 8/10 groups; adaptive R=0.8125, 8/10). The
  clustering decision logic was additionally verified correct offline.
- **`gemma-31b` model string is a placeholder.** `GEMMA_31B_AISTUDIO_MODEL`
  defaults to `gemma-4-31b-it` (the bias model already used in
  `agents/llm_client.py`); the annotator should confirm/edit it for their AI
  Studio account.
- The bias harness was verified on a subset (`gemma-4b`, 5 articles); the full
  4-model × 60-article run (Step 6.4) has not yet been executed end-to-end.

## Section F — Test Results

- `evaluate_bias.py --models gemma-4b --limit 5 --timeout 180` → ran end-to-end,
  0 parse errors, accuracy 0.60 / macro-F1 0.15 on the 5-article subset, wrote
  `bias_predictions.json` (5 rows) and `bias_comparison.json`.
- `evaluate_clustering.py` → ran end-to-end. The full 60/60 run later completed
  (Sections M, N): baseline P=1.0 / R=0.7500 / F1=0.8571 (8/10 groups, hard 2/3);
  adaptive R=0.8125 / F1=0.8966 (8/10 groups, hard 2/3); zero false merges.
- Offline clustering-logic test (no API): same-event articles cluster together
  (`grouped`, pairwise F1 = 1.0), unrelated article → singleton. **PASS.**
- `dataset.json` md5 identical before/after; `git status` shows only new files
  under `evaluation/`; no `psycopg2`/`redis`/`call_tool`/DB access in either
  script (grep-verified). Both output schemas validated.

## Section G — Success Criteria Verification (session scope)

- **6.2 — bias evaluation across model sizes:** harness MET (runs, produces
  citable per-model metrics). Full 4-model run pending (Step 6.4 / quota).
- **6.3 — clustering same-event grouping:** harness MET (runs end-to-end,
  scores all 10 groups, hard cases reported separately, pairwise metric
  included). Full live numbers now available (Section N): baseline 8/10 groups,
  P=1.0/R=0.7500; adaptive 8/10, R=0.8125.
- **Read-only on production:** MET — verified no DB/MCP access, `dataset.json`
  immutable, only new `evaluation/` files in `git status`.
- **Prompt parity:** MET — bias prompt imported verbatim from
  `agents/bias_agent.py`; clustering thresholds/entity-overlap/prompt imported
  from `agents/clustering_agent.py` + `agents/ingestion_agent.py`.

## Section I — gemma-31b Local Ollama Backend (added 2026-06-10)

`evaluation/evaluate_bias.py` was updated so `gemma-31b` can run locally via Ollama
in addition to the existing Google AI Studio path. Only that file was edited; no
production code, no DB, no other evaluation file was touched.

### What changed

| Constant | Value | Purpose |
|---|---|---|
| `GEMMA_31B_BACKEND` | `"ollama"` (default) or `"aistudio"` | Selects the backend for `gemma-31b` at the top of the file |
| `GEMMA_31B_OLLAMA_MODEL` | `"gemma4:31b-mlx"` | Local Ollama tag (annotator-editable; verified present with `ollama list`) |
| `GEMMA_31B_AISTUDIO_MODEL` | `"gemma-4-31b-it"` | AI Studio model string (existing constant, preserved) |

The `MODELS["gemma-31b"]` dict entry reads `backend` and `model` from these constants.
The `classify()` dispatcher and `call_ollama()` helper are **unchanged** — `gemma-31b`
flows through the identical Ollama HTTP code path as `gemma-4b`, `gemma-12b`, and
`gemma-26b` when `GEMMA_31B_BACKEND == "ollama"`. The AI Studio path (`call_aistudio`)
is fully preserved and re-activated by setting `GEMMA_31B_BACKEND = "aistudio"`.

### Verification results

- **Check 1 (local Ollama default):** `--models gemma-31b --limit 3` routed through
  `call_ollama` → `POST http://localhost:11434/api/generate` with model `gemma4:31b-mlx`.
  No `google.genai` / AI Studio call fired. 3 rows written to `bias_predictions.json`
  (article 1: `pro_government` correct; articles 2–3: `ReadTimeout` recorded as
  `parse_error` per Rule 2.4 — the 31b model is slow; use `--timeout 600` or higher
  for the full run). Exit code 0.
- **Check 2 (prompt parity):** All six production helpers (`_BIAS_SINGLE_PROMPT_TEMPLATE`,
  `_strip_code_fences`, `_VALID_BIAS_LABELS`, `_source_from_url`, `_MAX_SINGLE_CHARS`,
  `_MAX_TITLE_CHARS`) are still imported verbatim from `agents/bias_agent.py`.
- **Check 3 (AI Studio regression):** Flipping `GEMMA_31B_BACKEND = "aistudio"` sets
  `MODELS["gemma-31b"]` to `{backend: "aistudio", model: "gemma-4-31b-it"}` and
  `classify()` routes to `call_aistudio` — identical to the original behaviour. Verified
  programmatically; AI Studio path was not modified.
- **Check 4 (other models unaffected):** `gemma-4b`, `gemma-12b`, `gemma-26b` retain
  `backend: "ollama"` and their original model tags; their `MODELS` entries were not
  touched.

### Thesis note

Running `gemma-31b` locally (quantized, via Ollama `gemma4:31b-mlx`) makes the
four-model comparison **consistent in backend and quantization regime** — all four
models are served by the same Ollama HTTP path with identical call parameters
(temperature 0, `format:"json"`, `think:false`). This differs from the production
AI Studio `gemma-4-31b-it` (unquantized, different serving infrastructure). Results
from the all-local run should therefore be read as a **like-for-like size comparison**
(4B vs 12B vs 26B vs 31B, same backend). When the AI Studio path is used instead,
the 31b numbers reflect a different quantization and serving stack than the other
three models. Both modes are available; the backend used must be documented alongside
any reported metrics.

---

## Section H — What the Next Steps Depend On

- **Step 6.4 (final testing pass)** depends on these harnesses: run all four
  bias models over the full 60 articles and re-run the clustering harness once
  Gemini quota is available, then record the F1 numbers and clustering accuracy.
- **Step 6.5 (final documentation)** will cite `bias_comparison.json` and
  `clustering_results.json` produced by these harnesses for the thesis tables.

---

## Section J — Phase 6 Harness Extension: Two Additional Models (2026-06-11)

`evaluation/evaluate_bias.py` was extended to compare **two additional models**
alongside the four existing Gemma sizes. Only that file was edited; no production
code, no DB, no other evaluation file, and no `evaluation/dataset.json` was
touched. The change is **read-only on production** and was **static-verified only**
(the annotator will run the model tests — see "Verification" below).

### What changed

#### A. `gemma-12b-qat` — local Ollama QAT variant

| Constant | Value | Purpose |
|---|---|---|
| `GEMMA_QAT_12B_MODEL` | `"gemma4:12b-it-qat"` | Local Ollama tag (annotator-editable; confirm with `ollama list`) |

- Result label: **`gemma-12b-qat`** — distinct from the existing `gemma-12b` (`gemma4:12b-mlx`) in all outputs and the stdout table.
- Backend: `"ollama"` — flows through the **identical** `call_ollama()` path, prompt, parsing, `parse_error` handling, metrics, and output schema as all other local models. No new helper.

#### B. `gemini-3.5-flash` — Google AI Studio API with resilient retry loop

| Constant | Value | Purpose |
|---|---|---|
| `GEMINI_FLASH_MODEL` | `"gemini-3.5-flash"` | AI Studio model string (**confirmed available** on annotator's account 2026-06-11; update if API rejects it) |
| `GEMINI_FLASH_MAX_RETRIES` | `30` | Maximum retry attempts for the transient/quota retry loop (annotator-editable) |

- Result label: **`gemini-3.5-flash``**.
- Backend tag: `"aistudio_flash"` — routes via `classify()` to a new `call_aistudio_flash()` function; `call_aistudio()` (used by `gemma-31b` aistudio path) is **unchanged**.
- Reuses `_get_aistudio_client()` singleton (same `GEMINI_API_KEY`, same `google.genai.Client`).

#### Flash resilient retry loop (the key requirement)

`call_aistudio_flash()` implements a **bounded exponential-backoff retry loop**:

- **Retryable** (keep retrying with backoff): transient server errors (503, 500 INTERNAL, "model overloaded") **and** quota / rate-limit errors (429, resource_exhausted) — both clear quickly under congestion.
- **Permanent / fail-fast** (return `{"error": ...}` immediately, no further retries): auth errors, invalid API key, invalid argument, bad request (`api_key_invalid`, `invalid_argument`, `401`, `403 forbidden`, etc.).
- **Everything else** (content policy, SDK bug, etc.): also fail-fast.
- **Backoff formula:** `min(60, _RETRY_BACKOFF × 2^attempt + jitter[0, 2])` seconds per attempt. At the default `_RETRY_BACKOFF = 3.0 s`, the ceiling of 30 retries spans roughly 10–25 minutes of cumulative wait.
- `asyncio.TimeoutError` (per-attempt `--timeout` exceeded) is treated as retryable — the endpoint may just be slow under load.
- On **ceiling exhaustion** (all 30 attempts failed): returns `{"error": "Flash exhausted N retries..."}`, which `evaluate_model()` records as `parse_error`. The run **continues** (Rule 2.4 — nothing is fatal).
- Error-classification fragments mirror `agents/llm_client._is_transient_server_error()` and `_is_quota_error()` exactly, inlined in `evaluate_bias.py` to avoid importing the heavy production module.

### Updated MODELS registry (six entries)

| Label | Backend | Model identifier |
|---|---|---|
| `gemma-4b` | `ollama` | `gemma4:e4b-mlx` |
| `gemma-12b` | `ollama` | `gemma4:12b-mlx` |
| `gemma-26b` | `ollama` | `gemma4:26b` |
| `gemma-31b` | `ollama` (default) or `aistudio` | `GEMMA_31B_OLLAMA_MODEL` / `GEMMA_31B_AISTUDIO_MODEL` |
| `gemma-12b-qat` | `ollama` | `GEMMA_QAT_12B_MODEL` (`gemma4:12b-it-qat`) |
| `gemini-3.5-flash` | `aistudio_flash` | `GEMINI_FLASH_MODEL` (`gemini-3.5-flash`) |

### Constants the annotator must confirm before running

1. **`GEMINI_FLASH_MODEL`** — `"gemini-3.5-flash"` confirmed on annotator's account. If the API rejects it, check `google.genai.list_models()` and update the constant.
2. **`GEMMA_QAT_12B_MODEL`** — `"gemma4:12b-it-qat"` must appear in `ollama list` before running `--models gemma-12b-qat`.
3. **`GEMINI_FLASH_MAX_RETRIES`** — default 30. Lower to e.g. 5 for quick test runs; raise if the endpoint is congested for longer than ~25 minutes.

### Verification (static only — harness not executed)

All verification was **static only**. The harness was **not run** against Ollama or the Gemini API; no live model calls were made; no background process was started. The annotator will run the model tests.

1. **AST parse** — `python3 -c "import ast, pathlib; ast.parse(pathlib.Path('evaluation/evaluate_bias.py').read_text())"` → **OK** (exit 0, no SyntaxError).
2. **MODELS registry** — all six labels present; `gemma-12b-qat` resolves `backend="ollama"` / `model=GEMMA_QAT_12B_MODEL`; `gemini-3.5-flash` resolves `backend="aistudio_flash"` / `model=GEMINI_FLASH_MODEL`. Verified by regex inspection of the compiled source.
3. **`classify()` dispatch** — three branches confirmed: `"ollama"` → `call_ollama`; `"aistudio_flash"` → `call_aistudio_flash`; fallthrough → `call_aistudio`. `gemma-12b-qat` routes to `call_ollama`; `gemini-3.5-flash` routes to `call_aistudio_flash`.
4. **Flash retry loop** — code inspection confirmed: (a) `_is_flash_retryable` and `isinstance(exc, asyncio.TimeoutError)` are retried with `min(60, _RETRY_BACKOFF × 2^attempt + jitter)` backoff; (b) `_is_flash_permanent` returns immediately without retrying; (c) ceiling exhaustion (`range(GEMINI_FLASH_MAX_RETRIES)` terminates) returns `{"error": ...}` → `parse_error` recorded, run continues.
5. **Prompt parity** — all six production helpers (`_BIAS_SINGLE_PROMPT_TEMPLATE`, `_strip_code_fences`, `_VALID_BIAS_LABELS`, `_source_from_url`, `_MAX_SINGLE_CHARS`, `_MAX_TITLE_CHARS`) still imported verbatim from `agents/bias_agent.py`. New models use the same `build_prompt` / `extract_label` path.
6. **Existing models unaffected** — `gemma-4b`, `gemma-12b`, `gemma-26b`, `gemma-31b` entries and the `GEMMA_31B_BACKEND` switch are byte-identical to their pre-change state. `call_aistudio()` is unchanged.
7. **No new dependencies** — `random` is stdlib; no new packages added to `requirements.txt`.

---

## Section K — Incremental Save + Inclusive `--start-id`/`--stop-id` Batching (2026-06-11)

`evaluation/evaluate_bias.py` was made **resilient to interruption** and given an
**inclusive id-range batch selector**, so a long, slow `gemini-3.5-flash` run (with
its bounded server-busy retry loop) can be split into small resumable batches
(e.g. 11–30, then 31–50, then 51–60) that join with no gaps or overlaps. Only that
file was edited; this note is the sole change to `summaries/phase_6/summary.md`. The
change is **read-only on production** (no production code, no DB, no MCP, no
`evaluation/dataset.json` edit) and was **static-verified only** — the harness was
**not executed**; the annotator runs all model tests.

### What changed

- **Incremental save (upsert by `(model, article_id)`, crash-safe).** Each article's
  prediction row is now persisted to `bias_predictions.json` the moment it completes,
  inside the per-article loop of `evaluate_model()`, via `_upsert_prediction()`:
  it loads the existing file, replaces the row with the same `(model, article_id)`
  if present (else appends), and writes back. So an interruption never loses
  completed work, and re-running a sub-range of a model replaces **only** those rows.
  The write goes through `_atomic_write_json()` — a `tempfile.NamedTemporaryFile` in
  the **same directory** followed by `os.replace()` — so a crash mid-write can never
  corrupt the JSON. `ensure_ascii=False` + `indent=2` are preserved (Arabic stays
  readable, format unchanged).
- **Removed the coarse `merge_predictions()`.** The old end-of-run merge wiped **all**
  of a run model's rows before writing the batch, which would have erased earlier
  batches of the same model. It is gone; the incremental upsert is now the single
  source of truth for `bias_predictions.json`.
- **`--start-id N` / `--stop-id M` (inclusive `[N, M]`).** Two new argparse integer
  options (both default `None` = unbounded; either may be given alone). The id-range
  filter is applied **first**, to the ordered usable-article list inside
  `load_dataset()`, and **`--limit` is applied after** the range filter. So
  `--start-id 11 --stop-id 30 --limit 5` evaluates the first 5 articles whose `id`
  is in `[11, 30]`. An inverted range (`start-id > stop-id`) logs a clear
  "empty range — no articles to evaluate" message and **exits 0** (nothing-to-do is
  valid, not fatal); skipped articles are never classified or written.
- **End-of-run aggregation now cumulative.** `bias_comparison.json` is recomputed at
  end-of-run from **all rows now present in `bias_predictions.json` for the evaluated
  model(s)** — not just the current batch — so a batched Flash run reflects the full
  accumulated set (e.g. all 60 rows after 11–30 + 31–50 + 51–60), not only the last
  batch. The `(human_label, predicted_label)` pairs are rebuilt from the file rows;
  the metric math, confusion-matrix logic, the `parse_error`-counts-as-incorrect
  rule, and the output schema are **unchanged**. `merge_comparison()` still merges by
  model key, preserving other models' metrics.

### Batching motivation

`gemini-3.5-flash` is slow and its resilient retry loop (`GEMINI_FLASH_MAX_RETRIES`,
exponential backoff up to ~10–25 min) makes a full 60-article run prone to
interruption. Before this change an interrupted run lost every in-progress
prediction even though they had already printed to stdout. Incremental save plus the
inclusive range selector let the annotator run small, resumable batches that
accumulate into a complete, correctly-scored result.

### Verification (static only — harness NOT executed)

1. **Module parses** — `ast.parse` and `python3 -m py_compile evaluation/evaluate_bias.py`
   both exit 0; no import / `bootstrap_env` / model call was triggered.
2. **Range flags wired, applied before `--limit`** — `--start-id`/`--stop-id` parse to
   `args.start_id`/`args.stop_id` (argparse dest verified in isolation); the inclusive
   filter runs in `load_dataset()` before the `--limit` slice:
   `usable = [a for a in usable if a.get("id") is not None and a.get("id") >= start_id]`,
   the symmetric `<= stop_id`, then `usable = usable[:limit]`.
3. **`start-id > stop-id` graceful** — guarded in `main_async()` with a clear log
   message and `return` (exit 0), before any classification.
4. **Incremental crash-safe upsert** — `_upsert_prediction(row)` is called every loop
   iteration; it upserts by `(model, article_id)` (replace-in-place else append) and
   writes via `_atomic_write_json()` (temp file in same dir + `os.replace`); other
   models' rows and earlier articles are preserved.
5. **Idempotency + cumulative comparison** — re-evaluating an existing
   `(model, article_id)` replaces rather than duplicates; `bias_comparison.json` is
   still written at end-of-run, recomputed over all of each model's file rows, with
   the metric/confusion-matrix code untouched.

`progress_log.md` and earlier summaries were not modified.

---

## Section L — Three Additional Local QAT Models: 4B / 26B / 31B (2026-06-13)

`evaluation/evaluate_bias.py` was extended with **three more local Ollama models** —
the QAT (quantization-aware-training) variants of the 4B, 26B, and 31B sizes — so the
thesis can compare **mlx vs QAT quantization across every Gemma size** (it already had
`gemma-12b` mlx vs `gemma-12b-qat`). Only that file was edited; this note is the sole
change to `summaries/phase_6/summary.md`. The change is **read-only on production** (no
production code, no DB, no MCP, no other evaluation file, no `evaluation/dataset.json`)
and was **static-verified only** — the harness was **NOT executed**; the annotator runs
all model tests.

### What changed

Three new editable tag constants next to `GEMMA_QAT_12B_MODEL`, and three new `MODELS`
registry rows (placed after `gemma-12b-qat`, before `gemini-3.5-flash`), each
`backend: "ollama"` referencing its constant — following the **exact** `gemma-12b-qat`
pattern. The module docstring's model table was updated to include the three rows.

| Result label | Editable tag constant | Constant value | `params` |
|---|---|---|---|
| `gemma-4b-qat` | `GEMMA_QAT_4B_MODEL` | `gemma4:e4b-it-qat` | `4B-QAT` |
| `gemma-26b-qat` | `GEMMA_QAT_26B_MODEL` | `gemma4:26b-a4b-it-qat` | `26B-QAT` |
| `gemma-31b-qat` | `GEMMA_QAT_31B_MODEL` | `gemma4:31b-it-qat` | `31B-QAT` |

- All three flow through the **identical, unchanged** `call_ollama()` path, the imported
  production prompt (`build_prompt` → `_BIAS_SINGLE_PROMPT_TEMPLATE`), the same parsing
  (`extract_label`), the same `parse_error`-counts-as-incorrect rule, the same metrics,
  and the same outputs (stdout table + `bias_predictions.json` / `bias_comparison.json`)
  as the other local models. **No new helper, no metric change, no schema change.**
- Labels are distinct from the existing mlx labels (`gemma-4b`, `gemma-26b`, `gemma-31b`)
  and consistent with `gemma-12b-qat`, so all outputs distinguish mlx vs QAT cleanly.
- The new labels work with the existing `--models`, `--limit`, `--timeout`, `--retries`,
  `--start-id`/`--stop-id` flags and the merge-by-`(model, article_id)` incremental save —
  they are just three more local models. The `GEMMA_31B_BACKEND` selection mechanism, the
  Flash retry path, and `call_aistudio()` are untouched.

### Constants the annotator must confirm against `ollama list`

1. `GEMMA_QAT_4B_MODEL`  — `gemma4:e4b-it-qat`
2. `GEMMA_QAT_26B_MODEL` — `gemma4:26b-a4b-it-qat`
3. `GEMMA_QAT_31B_MODEL` — `gemma4:31b-it-qat`

### Verification (static only — harness NOT executed)

1. **Parse / compile** — `ast.parse` and `python3 -m py_compile evaluation/evaluate_bias.py`
   both exit 0; the module was **not imported**, so `bootstrap_env()` / the SDK import never
   fired.
2. **Registry resolves** — AST extraction of `MODELS` (with the three new constants
   substituted) shows all three new rows as `{"backend": "ollama", "model": <QAT tag>,
   "params": <…>}`: `gemma-4b-qat` → `gemma4:e4b-it-qat` / `4B-QAT`; `gemma-26b-qat` →
   `gemma4:26b-a4b-it-qat` / `26B-QAT`; `gemma-31b-qat` → `gemma4:31b-it-qat` / `31B-QAT`.
   Registry size is now 9.
3. **`classify()` routing** — AST inspection confirms the `backend == "ollama"` branch
   dispatches to `call_ollama`; all three new rows are `backend: "ollama"`, so they take it.
4. **Distinct + existing unmodified** — all 9 keys unique; the three new keys are distinct
   from the mlx labels and from `gemma-12b-qat`; `gemma-4b`, `gemma-12b`, `gemma-26b`,
   `gemma-12b-qat`, and `gemini-3.5-flash` rows are byte-identical to their pre-change
   resolved values (diff-style equality check passed).
5. **Prompt parity** — the six production helpers are still imported verbatim from
   `agents/bias_agent.py` (unchanged); `build_prompt` uses `_BIAS_SINGLE_PROMPT_TEMPLATE`
   and `extract_label` uses `_VALID_BIAS_LABELS` + `_strip_code_fences`.

### Thesis note

These three QAT variants complete an **mlx-vs-QAT comparison across all four sizes**
(4B, 12B, 26B, 31B): each size now has both an mlx entry and a QAT entry served through the
identical local Ollama HTTP path with identical call parameters (temperature 0,
`format:"json"`, `think:false`), isolating the quantization regime as the only variable.

`progress_log.md` and earlier summaries were not modified.

---

## Section M — Clustering Harness: Multi-Key Quota Survival (2026-06-13)

`evaluation/evaluate_clustering.py` was made resilient to Gemini free-tier quota
limits so a full 60-article run can complete in one pass by **rotating across
multiple API keys**. Only that file was edited; this note is the sole change to
`summaries/phase_6/summary.md`. The change is **read-only on production** (no
production code, no `agents/llm_client.py`, no DB, no MCP, no `evaluation/dataset.json`
/ `clustering_ground_truth.json`) and was **static-verified only** — the harness was
**NOT executed**; the annotator runs all model/API tests.

### Key finding driving the design

Key rotation **already exists in production** and the harness already used it. Both
Gemini call sites delegate to `agents.llm_client`:

- embeddings → `llm_client.gemini_embed`
- entities  → `llm_client.gemini_generate_with_fallback(task_type="entities")`

Both internally use `agents.llm_client.GeminiKeyPool` — round-robin across
`GEMINI_API_KEY` .. `GEMINI_API_KEY_10`, **per-key 1-hour quarantine** on
429/`resource_exhausted`, **same-key 3× backoff** on transient 503/500, and
**fail-fast** on permanent (auth/invalid-key) errors, with API keys always masked
in logs (`AIza***xyz`). So a single run *already* rotates across every configured
key. Re-implementing rotation in the harness would have required either editing the
shared module (out of scope) or constructing `genai.Client`s directly (which would
bypass the exact production embedding/entity path the harness is designed to
exercise). Both were rejected; the harness leans on the existing pool.

The heavier quota consumer is **entity extraction** (`gemma-4-31b-it`), which the
prior run exhausted first.

### What changed (harness-local only)

1. **Quota-exhaustion awareness.** New `_is_pool_exhausted()` classifier detects the
   pool's *all-keys-down* error signatures returned by `llm_client`
   (`"all Gemini keys are quarantined"`, `"pool exhausted"`, `"no keys available"`,
   and a fallback `"chain exhausted ... (last error: quota ...)"`). A lone quota
   error on one key never reaches the harness — the pool rotates past it; only a
   fully-spent rotation surfaces. Transient (503/500) and permanent (auth/invalid)
   single-call errors are deliberately **not** treated as exhaustion: they return
   `None` and the run continues, mirroring the pool's own
   quota-vs-transient-vs-permanent distinction.
2. **Clean stop on the quota wall.** `_embed` / `_extract_entities` raise a new
   `_PoolExhausted` only on that pool-level signal. `preprocess()` catches it,
   stops at that article (no looping the remainder into guaranteed failures),
   logs how far it got (`N/60`), and returns `(prepared, stopped_early=True)`.
   `main_async()` then **skips writing `clustering_results.json`** (so a prior good
   result is never clobbered with an all-singleton degraded one) and exits with
   code **2** (distinct from config-error exit 1; non-zero = "incomplete due to
   quota", no exception raised — Rule 2.4).
3. **Incremental, atomic cache save.** The preprocess cache
   (`evaluation/results/.preprocess_cache.json`) is now saved **per completed
   article** via a temp-file + `os.replace()` atomic write, instead of once at the
   end. A quota stop or hard kill can no longer lose already-fetched
   embeddings/entities, and the write can never be left half-corrupt.
4. **Cache-aware resume.** A cached article consumes **zero key/quota** on rerun
   (unchanged behavior, now reinforced by the incremental save). A later run with
   refreshed quota resumes from the cache and re-spends quota only on the
   not-yet-completed articles.

### Env convention the annotator must set up

No new convention — the production one is reused. Populate `.env` with
`GEMINI_API_KEY` (slot 1) and `GEMINI_API_KEY_2 … GEMINI_API_KEY_10` (slots 2–10);
the pool loads up to 10 keys (its loader uses slots 2–10, so a `GEMINI_API_KEY_11`,
if present, is currently ignored by the production loader — not changed here). More
keys = more total daily quota before the wall. No keys were invented or printed.

### Unchanged (verified)

Cosine threshold (`_SIMILARITY_THRESHOLD` 0.82), entity-overlap rule
(`_ENTITY_OVERLAP_MIN` 2, `_entity_overlap`), the 72 h window, `_FIND_SIMILAR_LIMIT`,
greedy in-order assignment, and all scoring / pairwise-metric math are byte-identical
to their pre-change state. The change is purely about how Gemini calls acquire/rotate
keys and how the harness reacts to total exhaustion.

### Verification (static only — harness NOT executed)

1. **Parse / compile** — `python3 -m py_compile evaluation/evaluate_clustering.py`
   and `ast.parse(...)` both exit 0. The module was **not imported**, so
   `bootstrap_env()` / the Google SDK import / any API call never fired.
2. **Both call sites route through rotation** — `_embed → llm_client.gemini_embed`;
   `_extract_entities → llm_client.gemini_generate_with_fallback`. Both pool functions
   own the round-robin + quarantine; no direct `genai.Client` is built in the harness.
3. **Quota → next key + retry** — performed inside the pool: `get_available_key`
   skips quarantined/tried keys, `quarantine_key` parks a 429 key for 1 h, and the
   call retries with the next key (`gemini_embed` `continue` loop; the
   `gemini_generate_with_fallback` `while True` key loop). Transient → 3× 5 s same-key
   backoff; permanent → immediate return. The harness adds the *all-exhausted* stop.
4. **All-keys-exhausted path** — `_is_pool_exhausted` → `_PoolExhausted` →
   `preprocess` breaks, returns `stopped_early=True` → `main_async` logs progress,
   does **not** write `clustering_results.json`, and `sys.exit(2)`. The cache was
   already saved incrementally and is preserved.
5. **No new dependencies** — `os`, `tempfile` are stdlib.

`progress_log.md` and earlier summaries were not modified.

---

## Section N — Clustering Evaluation: Diagnostics, Recall Study, and Production Promotion (2026-06-14)

After the quota-survival work (Section M), a full 60/60 clustering evaluation was
completed (all articles served from `.preprocess_cache.json`, no API calls) and an
analytical study was carried out. The full report lives in the root-level
`clustering evaluation report.md`; this section is the durable summary. **One production
file was changed in this arc — `agents/clustering_agent.py` — under explicit, separate
approval; everything else is evaluation-only.**

**Preprocessing:** all 60 articles received an embedding and entities; the baseline
formed **9 clusters** and **38 singletons**.

### N.1 Pair-level diagnostics added to the harness

`evaluation/evaluate_clustering.py` was extended to record, for every ground-truth pair,
the exact `cosine`, `shared_entities`, each gate's pass/fail (`cosine_pass`,
`entity_overlap_pass`, `same_section`, `within_72h`), the merge decision, and the list of
`failed_gates`. These are written to a `pairwise_diagnostics` block in
`clustering_results.json` and printed as a stdout table (`print_diagnostics`). This made
every group outcome explainable down to the precise gate that blocked it. Helpers added:
`diagnose_groups()` and `print_diagnostics()`. No thresholds or metric math changed.

### N.2 Baseline result (production-faithful, standard gate)

Standard gate (`cosine ≥ 0.82` AND `shared_entities ≥ 2`), greedy in-order assignment:

- Groups correct **8/10**; hard groups (4,6,7) **2/3** (Group 6 split); pairwise
  **P=1.0000, R=0.7500, F1=0.8571** (TP=12, FP=0, FN=4).
- The 4 residual FN are (14,53), (24,10), (24,59), (10,59). All other groups — including
  the hard different-title groups 4, 7 and the identical-title group 10 ({55,45,34}, cosines
  0.93–0.97, 6–9 shared entities) — clustered correctly. **Precision is a perfect 1.0:
  zero false merges across the 23 ground-truth articles.**

### N.3 The two residual misses (root cause)

- **Group 6 — one borderline pair + one genuine outlier.** Article 59 is a true semantic
  outlier from its group-mates: (24,59) cosine **0.732** / 1 entity and (10,59) **0.784** /
  1 entity both fail *both* gates and sit below even the 0.80 adaptive floor, so 59 can
  never attach. The (24,10) pair is borderline-recoverable: cosine **0.807** (0.013 under
  the floor) despite **11 shared entities**. Under the standard gate no Group 6 pair merges,
  so the group fully splits; the adaptive gate recovers only (24,10) → `partial` (see N.4).
- **Group 2 — genuine semantic limit.** (14,53) fails **both** gates: cosine 0.748 and
  only **1** shared proper noun (Libya). Both are op-eds on an abstract theme ("militias
  and the state") with a total opposition→neutral stylistic rewrite. Irreducible; closing
  it (entity_min = 1, or cosine < 0.80 without entity support) would endanger precision.

### N.4 Recall study — two opt-in experimental merge policies

Two evaluation-only strategies were added behind flags, both defaulting OFF so the
harness still reproduces production exactly:

- `--adaptive-threshold` (**Variant B**): also merge a pair when
  `cosine ≥ _ADAPTIVE_COSINE_FLOOR (0.80)` **and** `shared_entities ≥ _ADAPTIVE_ENTITY_MIN
  (6)` — a strong entity signal compensates a slightly lower cosine. Implemented via a
  `_pair_merges()` predicate and `_cluster_greedy()`.
- `--merge-strategy components` (**Variant A**): order-independent connected components
  (union-find) over qualifying pairs, via `_cluster_components()`.

`cluster()` became a dispatcher; experiments write to separate files
(`clustering_results__<strategy>[_adaptive].json`) and stamp a `merge_policy` metadata
block. Live A/B over all 60 articles (cache-served):

| Configuration | Groups | Hard | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|
| Baseline (greedy, production-faithful) | 8/10 | 2/3 | 12 | 0 | 4 | 1.0000 | 0.7500 | 0.8571 |
| Variant A — components | 8/10 | 2/3 | 12 | 0 | 4 | 1.0000 | 0.7500 | 0.8571 |
| Variant B — adaptive (greedy) | 8/10 | 2/3 | **13** | 0 | 3 | **1.0000** | **0.8125** | **0.8966** |
| Variant A + B — components + adaptive | 8/10 | 2/3 | **13** | 0 | 3 | **1.0000** | **0.8125** | **0.8966** |

The adaptive variants recover exactly **one** pair — (24,10) — moving Group 6 from `split`
to `partial` ({24,10} together, article 59 still isolated); the remaining FN are (14,53),
(24,59), (10,59). Group-level and hard-group scores are unchanged (article 59 is too far
from its group to attach). Components alone changes nothing (Group 6 has no qualifying
standard edge to chain). **Precision held at 1.0 (zero false merges) in every variant.**

### N.5 Production promotion — adaptive gate in `agents/clustering_agent.py` (PRODUCTION CHANGE)

On the evidence above, **Variant B (adaptive) only** was promoted to production; Variant A
(components) was **not** (it changes global grouping semantics with a transitive-chaining
precision risk for zero additional benefit on this data). Changes to
`agents/clustering_agent.py`:

1. New constants `_ADAPTIVE_COSINE_FLOOR = 0.80`, `_ADAPTIVE_ENTITY_MIN = 6` (standard
   `_SIMILARITY_THRESHOLD = 0.82` / `_ENTITY_OVERLAP_MIN = 2` unchanged).
2. **`find_similar` SQL `threshold` lowered 0.82 → 0.80** so the DB returns borderline
   candidates the agent previously never saw (the cosine gate was SQL-enforced, so without
   this the adaptive branch would be dead code).
3. New pure helper **`_merge_qualifies(cosine, overlap)`**: merge if
   `(cos ≥ 0.82 AND ov ≥ 2)` **OR** `(cos ≥ 0.80 AND ov ≥ 6)`.
4. Candidate loop now reads each candidate's cosine and defers to `_merge_qualifies`.

**Deliberately unchanged:** greedy in-order claim-once assignment, the `scores` /
`relevance_score` model, the MCP boundary (`self.call_tool`), and the public
`run(section, article_ids)` signature (so `agents/graph.py` needs no change). The new
behaviour is a strict **superset** of prior merges (every old merge still happens; only
0.80–0.82 pairs with ≥ 6 entities are newly admitted; nothing below 0.80 ever merges).
Reversible by restoring the `find_similar` threshold and removing/ignoring the helper.

### N.6 Reproduced verification (no API; production code exercised)

- **Self-run A/B** of the harness confirmed N.4 exactly (baseline P=1.00 R=0.7500
  F1=0.8571, 8/10, hard 2/3; adaptive P=1.00 R=0.8125 F1=0.8966, 8/10, hard 2/3). The
  only delta is Group 6 moving `split` (three singletons) → `partial [24,10]+singleton 59`.
- **Production-function test:** the real `agents/clustering_agent.py` was imported (with a
  lightweight stub for the offline-unavailable `mcp` package; **no production code
  modified**) and `_merge_qualifies` was run over all 16 ground-truth pairs. Exactly one
  pair flipped — **(24,10)**: cosine 0.807, 11 entities — with **zero regressions** and
  the genuine non-matches (14,53), (24,59), (10,59) correctly left unmerged. Six boundary
  checks all passed.

### N.7 Verification status and caveats

`agents/clustering_agent.py` **parses** (`ast.parse`) and reports **no linter errors**; it
was **not** run against a live DB/MCP (unavailable in this environment), so the
behavioural evidence comes from the harness (identical `_merge_qualifies` logic) and the
direct function test. Caveats recorded honestly: (i) constants 0.80 / 6 are calibrated to
one 60-article set and should be re-validated on more data; (ii) `ClusteringAgent`
integration tests should run on a live-DB environment before production reliance; (iii)
the 0.80 floor returns marginally more `find_similar` candidates (filtered in Python — a
small cost, not a correctness issue).

### N.8 Files touched in this arc

- `evaluation/evaluate_clustering.py` — diagnostics, adaptive/components experiments +
  flags (evaluation-only).
- `agents/clustering_agent.py` — **production**: adaptive gate (N.5).
- `clustering evaluation report.md` — full report (Sections 1–11), the authoritative
  detailed record.
- `evaluation/results/clustering_results.json` (+ `__greedy_adaptive`,
  `__components_adaptive`) — result/diagnostic outputs.

## Section O — 2-D threshold-sweep diagnostic (cosine × entity-min), measurement-only (2026-06-17)

Added a **read-only, cache-only** threshold sweep to `evaluation/evaluate_clustering.py`,
behind a new `--sweep` flag, to empirically justify the production gate (cosine 0.82 /
entity-overlap 2) and to show exactly where lowering the entity minimum costs precision.

- **Grid:** cosine `[0.70, 0.78, 0.80, 0.82, 0.84, 0.88]` × entity-min `[1, 2, 3]` = **18
  cells**, plus **one production-rule reference row** (standard 0.82/2 **OR** adaptive 0.80/6).
- **Per cell:** the **standard gate in isolation** (`merge ⇔ cosine ≥ T AND overlap ≥ E`,
  adaptive branch disabled) via a new **parameterised local clusterer**
  `_sweep_cluster(prepared, order, cos_t, ent_min)` that takes both thresholds as arguments
  and feeds the existing `score_groups` / `pairwise_metrics`. **No production constant is
  reassigned** — `_SIMILARITY_THRESHOLD` / `_ENTITY_OVERLAP_MIN` are read for the reference
  row only.
- **Cache-only / zero API:** loads via new `preprocess_from_cache_only(articles)` which
  never calls `_embed` / `_extract_entities`; if any article is missing from the cache it
  reports the ids and stops (exit 2) rather than fetching. Confirmed cache covers 60/60.
- **Output:** prints a T×E grid (F1 + precision per cell, `*` flag on any precision < 1.0,
  most visible in the entity-min = 1 column) + a detailed per-cell table (groups, hard,
  TP/FP/FN, P/R/F1) + the reference row + best-F1-with-precision-1.0; writes
  `evaluation/results/clustering_sweep.json` (`ensure_ascii=False`). **Never writes
  `clustering_results.json`.**
- **No production change:** `agents/clustering_agent.py` untouched; the default run (no flag)
  is byte-identical to before (the `--sweep` branch runs and exits before the normal path).
- **Static-verified** (ast.parse + py_compile + lint clean). Command:
  `python3 evaluation/evaluate_clustering.py --sweep`.

### O.1 Sweep results — two-pass empirical threshold justification (annotator-run)

The annotator ran `--sweep` (grid evolved to three passes). `clustering_sweep.json`
written; zero API calls; 54 cells total (18 with window/greedy, 18 without window/greedy, 18
without window/components).

**Pass 1 — with 72h window (production-faithful):** Precision stayed 1.0 in all 18 cells
(structural: no two labelled groups share both section AND 72h window, so FP is mathematically
impossible here). Recall monotone in cosine floor. Only cells beating production F1 (0.8966)
set E=1, recovering single-entity / low-cosine pairs (unsafe in open world). Production
reference = 8/10, hard 2/3, P=1.0 R=0.8125 F1=0.8966.

**Pass 2 — without 72h window (stress-test):** Removes the structural protection and reveals
the true false-merge risk. Key results (standard gate isolated, no time filter):
- E=1 never achieves P=1.0 below cosine 0.84 (FP=2 even at production 0.82)
- E=2 also fails at cosine ≤ 0.82 without the window (FP=2 at `0.82/E=2`)
- **E=3 achieves P=1.0 at cosine ≥ 0.80** (first safe E below 0.84)
- False merges interact destructively with greedy assignment: at `0.78/E=2`, FP=7 and TP
  collapses to 8 — worse on every metric than the production baseline
- `0.82/E=2` (the production pair) requires the 72h window to hold P=1.0; the time window is
  the third and necessary pillar of the precision guarantee, not a redundant heuristic

**Pass 3 — without 72h window AND connected components (purest stress-test):** removes BOTH
the time window and the greedy ordering artefact (union-find, order-independent). Decisive
results:
- Greedy was **hiding** most false-merge potential: at `0.82/E=2` (no window), FP=2 under
  greedy (Pass 2) but **FP=16 under components** (Pass 3) — 8× increase. Greedy's claim-once
  freezes an article into the first cluster, suppressing contagion.
- Transitive contamination is catastrophic at low cosine: `0.70/E=1` → **FP=102** (P=0.14),
  because a single spurious cross-group edge merges entire same-section groups (the 15
  middle_east GT articles chain into one component: C(15,2)−9 = 96 FP, +6 from libya = 102).
- No entity-min rescues precision below cosine 0.88 under components (even E=3 leaves FP=4 at
  cosine 0.78–0.84); only `cosine≥0.88` reaches P=1.0 (R=0.50).
- This is the empirical case against connected components in production (Section 8 declined
  it for "transitive-chaining risk"): 8× the false pairs of greedy, degrading to 102.

**FP at production thresholds (0.82/2) across passes:** Pass 1 (window+greedy)=0 → Pass 2
(no window, greedy)=2 → Pass 3 (no window, components)=16. Each removed protection multiplies
damage; production keeps both (window + greedy) → FP=0.

**What the three passes prove:** The conditions (cosine ≥ 0.82, entity ≥ 2, 72h window) plus
greedy claim-once form a defence-in-depth system — each closes a gap the others leave open.
E=2 justified over E=3: equally safe in production but E=2 keeps a safety margin (one below
observed min true-merge overlap of 3). Greedy claim-once costs recall but buys precision
robustness (the correct trade for a precision-critical task). Full analysis in `clustering
evaluation report.md` Sections 9.8–9.10. No production change.

`progress_log.md` and earlier summaries were not modified.

---

## Section P — Bias Evaluation: Full Results, Per-Label Analysis, and Report (2026-06-18)

The bias model-comparison harness (Step 6.2) was run to completion and the results were
analysed. The authoritative detailed write-up lives in the root-level `bias evaluation .md`;
this section is the durable summary. **No production code was changed in this arc** — it is
measurement and documentation only. Raw per-model metrics (accuracy, macro-F1, per-label
precision/recall/F1, 5×5 confusion matrices, all with **zero parse errors**) are in
`bias evaluation/{Arabic,English,Mix,Improved}/bias_comparison.json`.

**Dataset:** 60 articles, 5 labels (`pro_government`, `opposition`, `neutral`, `pan_arab`,
`western_aligned`), **exactly 12 per label** (perfectly balanced, so macro-F1 ≈ accuracy and
is the honest headline). Local models served via identical Ollama HTTP params (temp 0,
`format:"json"`, `think:false`); `gemini-3.5-flash` is a hosted API reference point.

### P.1 Stage 1 — prompt-language selection (Arabic vs English vs Mix)

Three formulations of the same prompt compared. Mean over the five models:

| Prompt | Mean accuracy | Mean macro-F1 |
|---|---|---|
| Arabic | 0.723 | 0.713 |
| English | 0.727 | 0.722 |
| **Mix** | **0.750** | **0.746** |

**Mix** (English instructions + Arabic per-label definitions) wins on both metrics and is
best-or-tied in 4/5 models (only `gemma-4b` marginally preferred Arabic). The Mix benefit is
largest for mid/large local models (`12b`, `26b`). Mix selected as the base for refinement.

### P.2 Stage 2 — Improved prompt (final results)

| Metric | 4b | 12b | 26b | 31b | Flash |
|---|---|---|---|---|---|
| Correct (/60) | 39 | **50** | 44 | 49 | **50** |
| Accuracy | 0.650 | **0.833** | 0.733 | 0.817 | **0.833** |
| Macro-F1 | 0.642 | **0.833** | 0.736 | 0.808 | **0.835** |

- `gemma-12b` is the **biggest winner of refinement** (+0.067 acc / +0.074 macro-F1 over Mix)
  and reaches near-parity with hosted `gemini-3.5-flash` (its 0.002 macro-F1 lead is noise).
- **Bigger ≠ better:** `gemma-12b` beats both `gemma-26b` and `gemma-31b`; `gemma-31b` is the
  slowest model (~55–58 min/60) with no quality payoff.

### P.3 Per-label error structure (Improved prompt)

Two systematic patterns dominate every model:
- **`western_aligned` is the hardest label (recall problem):** lowest recall almost
  everywhere (0.50–0.75), leaking mostly to `neutral`/`opposition`. It is the single largest
  drag on the large models' macro-F1.
- **`neutral` is an over-prediction sink for weaker models:** high `neutral` recall, low
  `neutral` precision (`gemma-4b` neutral precision 0.52) — uncertain articles get dumped
  into `neutral`. Strongest labels: `pan_arab`, `opposition`, and high `pro_government`
  precision.

### P.4 QAT vs mlx quantization study (Improved prompt)

| Metric | 4b | 4b-QAT | 12b | 12b-QAT | 26b | 26b-QAT |
|---|---|---|---|---|---|---|
| Accuracy | 0.650 | 0.633 | **0.833** | 0.750 | 0.733 | 0.717 |
| Macro-F1 | 0.642 | 0.591 | **0.833** | 0.718 | 0.736 | 0.707 |

- QAT lowers quality in **every** case; worst on the best model `12b` (**−0.083 acc /
  −0.115 macro-F1**).
- **Key mechanism:** QAT does not degrade evenly — it **collapses `western_aligned` into
  `neutral`** (`12b-QAT` recovers only 2/12 western_aligned, sending 9 to neutral;
  `4b-QAT` recovers 1/12), which is why macro-F1 falls far harder than accuracy.
- Time savings are modest (slightly negative for 4b). On quality+time, QAT is a net loss; its
  real benefit (memory footprint) is not visible here and must be weighed separately.

### P.5 Recommendations

- **Best quality:** `gemini-3.5-flash` + Improved prompt (macro-F1 0.835).
- **Best local/offline:** `gemma-12b` (mlx) + Improved prompt (acc 0.833, macro-F1 0.833;
  ~28 min/60).
- **Avoid** `gemma-31b` (slow, no advantage) and the QAT variants for this task unless memory
  is the binding constraint (esp. `gemma-12b-qat`).
- **Next ceiling-raiser:** sharpen `western_aligned` vs `neutral` disambiguation in the prompt
  — that one confusion is the dominant remaining error for the strong models.

### P.6 Caveats

60-article set (12/label): differences ≤ 0.02 are noise (Flash 0.835 vs 12b 0.833). Perfectly
balanced set is generous vs a real stream where `neutral` dominates — the neutral-magnet
behaviour of small/QAT models would hurt more in production. Zero parse errors across all runs.
`gemini-3.5-flash` is a hosted reference, not a like-for-like member of the local size sweep.

### P.7 Files touched in this arc

- `bias evaluation .md` — full bias evaluation report (Sections 1–10), authoritative record.
- `summaries/phase_6/summary.md` — this Section P (durable summary).
- No production code, no DB, no `evaluation/dataset.json` changed.

`progress_log.md` and earlier summaries were not modified.
