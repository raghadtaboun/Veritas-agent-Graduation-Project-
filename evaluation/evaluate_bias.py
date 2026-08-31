"""
evaluation/evaluate_bias.py — Phase 6 Step 6.2: Bias Model Comparison Harness

Standalone research script. Runs the bias-classification task across NINE models
(four Gemma local mlx sizes, four Gemma QAT local variants spanning the same
sizes, and one Gemini Flash API model) over the 60-article human-labelled
dataset, compares each model's predictions to the human labels, and computes
per-label and macro F1.

    | Label in results  | Backend                                | Model identifier                                                          |
    | ----------------- | -------------------------------------- | ------------------------------------------------------------------------- |
    | gemma-4b          | Ollama (local)                         | gemma4:e4b-mlx                                                            |
    | gemma-12b         | Ollama (local)                         | gemma4:12b-mlx                                                            |
    | gemma-26b         | Ollama (local)                         | gemma4:26b                                                                |
    | gemma-31b         | Ollama (local, default) OR AI Studio   | GEMMA_31B_OLLAMA_MODEL / GEMMA_31B_AISTUDIO_MODEL (selected by GEMMA_31B_BACKEND) |
    | gemma-12b-qat     | Ollama (local)                         | GEMMA_QAT_12B_MODEL (default gemma4:12b-it-qat; annotator-editable)      |
    | gemma-4b-qat      | Ollama (local)                         | GEMMA_QAT_4B_MODEL (default gemma4:e4b-it-qat; annotator-editable)       |
    | gemma-26b-qat     | Ollama (local)                         | GEMMA_QAT_26B_MODEL (default gemma4:26b-a4b-it-qat; annotator-editable)  |
    | gemma-31b-qat     | Ollama (local)                         | GEMMA_QAT_31B_MODEL (default gemma4:31b-it-qat; annotator-editable)      |
    | gemini-3.5-flash  | Google AI Studio (API, resilient retry)| GEMINI_FLASH_MODEL (default gemini-3.5-flash; annotator must confirm)    |

Read-only on the production system. This script:
  * NEVER touches the MCP server, the production PostgreSQL DB, or Redis.
  * NEVER writes to evaluation/dataset.json (human labels are immutable).
  * Reuses the PRODUCTION bias prompt verbatim by importing
    ``_BIAS_SINGLE_PROMPT_TEMPLATE`` (the exact template
    ``BiasAgent.classify_single`` uses) from ``agents/bias_agent.py`` — see
    blueprint.md §11 / plan.md Step 6.2. The prompt is NOT re-implemented here.

Outputs (created under evaluation/results/):
  * bias_predictions.json — every (model, article_id, human_label,
    predicted_label, raw_model_output) row, for thesis auditability.
  * bias_comparison.json  — per-model metrics summary (macro-F1, accuracy,
    per-label F1, confusion matrix).

Output files are accumulated across runs so the annotator can run one model at a
time (the local 26B model is slow) without losing prior results:
  * bias_predictions.json — each article's row is persisted INCREMENTALLY as it
    completes, upserted by (model, article_id) via a crash-safe atomic write, so
    an interrupted run never loses completed work and re-running a sub-range of a
    model replaces only those rows (other rows are preserved).
  * bias_comparison.json — recomputed at end-of-run from ALL rows now present in
    bias_predictions.json for the evaluated model(s) (not just the current batch),
    merged by model key, so a batched run reflects the full accumulated set.

Rule compliance (agent.md):
  * Rule 2.4 — every model/network call is wrapped; a single failure is recorded
    (predicted_label = "parse_error" or an "error" note), never fatal.
  * Rule 2.6 / bootstrap — ``bootstrap_env()`` runs before the Google SDK import.

CLI:
    python evaluation/evaluate_bias.py                         # all models, 60 articles
    python evaluation/evaluate_bias.py --models gemma-4b       # one model
    python evaluation/evaluate_bias.py --models gemma-4b,gemma-12b --limit 5
    # Batch a slow model over an inclusive id range (resumable, no gaps/overlaps):
    python evaluation/evaluate_bias.py --models gemini-3.5-flash --start-id 11 --stop-id 30
    python evaluation/evaluate_bias.py --models gemini-3.5-flash --start-id 31 --stop-id 50
    python evaluation/evaluate_bias.py --models gemini-3.5-flash --start-id 51 --stop-id 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Project import path + environment bootstrap (Rule 2.6) ────────────────────
# bootstrap_env() MUST run before any google.genai import (agent.md bootstrap
# entry-point requirement). It also validates required keys and re-sets
# GOOGLE_API_KEY = GEMINI_API_KEY so the SDK uses the project's dedicated key.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.env_bootstrap import bootstrap_env  # noqa: E402

bootstrap_env()

import httpx  # noqa: E402
import google.genai as genai  # noqa: E402
import google.genai.types as genai_types  # noqa: E402

# Reuse the PRODUCTION bias prompt + parsing + budgets verbatim (no re-impl).
from agents.bias_agent import (  # noqa: E402
    _BIAS_SINGLE_PROMPT_TEMPLATE,
    _MAX_SINGLE_CHARS,
    _MAX_TITLE_CHARS,
    _VALID_BIAS_LABELS,
    _source_from_url,
    _strip_code_fences,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("evaluate_bias")


# ── Configuration ─────────────────────────────────────────────────────────────

# >>> ANNOTATOR: choose the backend for gemma-31b.
# >>>   "ollama"   — runs locally via the Ollama HTTP API; no Gemini quota consumed.
# >>>               Uses GEMMA_31B_OLLAMA_MODEL below. DEFAULT.
# >>>   "aistudio" — calls Google AI Studio; consumes Gemini free-tier quota.
# >>>               Uses GEMMA_31B_AISTUDIO_MODEL below.
GEMMA_31B_BACKEND: str = "ollama"   # or "aistudio"

# >>> ANNOTATOR: local Ollama tag for the 31b model. Edit to match the exact
# >>> name shown by `ollama list` (e.g. "gemma4:31b-q4", "gemma3:27b", etc.).
# >>> Confirmed present locally: gemma4:31b-mlx (verified with `ollama list`).
GEMMA_31B_OLLAMA_MODEL: str = "gemma4:31b-mlx"

# >>> ANNOTATOR: Google AI Studio model string. Used only when
# >>> GEMMA_31B_BACKEND == "aistudio". Default matches agents/llm_client.py.
GEMMA_31B_AISTUDIO_MODEL: str = "gemma-4-31b-it"

# >>> ANNOTATOR: local Ollama tag for the 12b QAT variant. Confirm the exact
# >>> name shown by `ollama list` before running (e.g. it may appear as
# >>> "gemma4:12b-it-qat" or with a digest suffix).
GEMMA_QAT_12B_MODEL: str = "gemma4:12b-it-qat"

# >>> ANNOTATOR: local Ollama tags for the 4b, 26b, and 31b QAT variants.
# >>> Confirm the exact names shown by `ollama list` before running. These
# >>> complete the mlx-vs-QAT comparison across all four sizes (4B, 12B, 26B, 31B).
GEMMA_QAT_4B_MODEL: str = "gemma4:e4b-it-qat"
GEMMA_QAT_26B_MODEL: str = "gemma4:26b-a4b-it-qat"
GEMMA_QAT_31B_MODEL: str = "gemma4:31b-it-qat"

# >>> ANNOTATOR: Gemini Flash model string for the AI Studio path.
# >>> "gemini-3.5-flash" was confirmed available on the annotator's account
# >>> (2026-06-11). If the API ever rejects it, check available models at
# >>> https://ai.google.dev/models or via `google.genai.list_models()` and
# >>> update this constant before running.
GEMINI_FLASH_MODEL: str = "gemini-3.5-flash"

# >>> ANNOTATOR: maximum retry attempts for the Flash transient/quota retry loop.
# >>> At default exponential backoff (3 × 2^attempt, capped at 60 s + up to 2 s
# >>> jitter), 30 attempts spans roughly 10–25 minutes of total wait before the
# >>> loop gives up and records parse_error. Raise this if the endpoint is
# >>> congested for longer; lower it for faster fail-over during testing.
GEMINI_FLASH_MAX_RETRIES: int = 30

# Canonical model registry. `key` is the label that appears in the results.
MODELS: dict[str, dict[str, str]] = {
    "gemma-4b":  {"backend": "ollama",   "model": "gemma4:e4b-mlx", "params": "4B"},
    "gemma-12b": {"backend": "ollama",   "model": "gemma4:12b-mlx", "params": "12B"},
    "gemma-26b": {"backend": "ollama",   "model": "gemma4:26b",     "params": "26B"},
    "gemma-31b": {
        "backend": GEMMA_31B_BACKEND,
        "model":   GEMMA_31B_OLLAMA_MODEL if GEMMA_31B_BACKEND == "ollama"
                   else GEMMA_31B_AISTUDIO_MODEL,
        "params":  "31B",
    },
    # ── Added Phase 6 extension (2026-06-11) ──────────────────────────────────
    # Local Ollama QAT variant — identical call path as the other local models.
    "gemma-12b-qat": {
        "backend": "ollama",
        "model":   GEMMA_QAT_12B_MODEL,
        "params":  "12B-QAT",
    },
    # ── Added Phase 6 extension (2026-06-13) ──────────────────────────────────
    # Local Ollama QAT variants of 4B/26B/31B — identical call path as the other
    # local models. Together with gemma-12b-qat these complete an mlx-vs-QAT
    # comparison across all four sizes (4B, 12B, 26B, 31B).
    "gemma-4b-qat": {
        "backend": "ollama",
        "model":   GEMMA_QAT_4B_MODEL,
        "params":  "4B-QAT",
    },
    "gemma-26b-qat": {
        "backend": "ollama",
        "model":   GEMMA_QAT_26B_MODEL,
        "params":  "26B-QAT",
    },
    "gemma-31b-qat": {
        "backend": "ollama",
        "model":   GEMMA_QAT_31B_MODEL,
        "params":  "31B-QAT",
    },
    # Gemini Flash via AI Studio — uses the resilient bounded retry loop
    # (call_aistudio_flash); backend tag "aistudio_flash" routes to it in classify().
    "gemini-3.5-flash": {
        "backend": "aistudio_flash",
        "model":   GEMINI_FLASH_MODEL,
        "params":  "Flash",
    },
}

# The five valid labels, in a fixed display order for the confusion matrix.
LABELS: list[str] = [
    "pro_government", "opposition", "neutral", "pan_arab", "western_aligned",
]
PARSE_ERROR: str = "parse_error"

OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")

# Per-call timeout (seconds) and transient-retry budget. The 26B local model is
# slow, so the default timeout is generous; override with --timeout.
DEFAULT_TIMEOUT: float = 240.0
DEFAULT_RETRIES: int = 2
_RETRY_BACKOFF: float = 3.0

_MAX_TOKENS: int = 4096
_TEMPERATURE: float = 0.0

# AI Studio courtesy throttle between calls (free-tier friendliness).
_AISTUDIO_INTERVAL: float = 0.3

_DATASET_PATH = _PROJECT_ROOT / "evaluation" / "dataset.json"
_RESULTS_DIR = _PROJECT_ROOT / "evaluation" / "results"
_PREDICTIONS_PATH = _RESULTS_DIR / "bias_predictions.json"
_COMPARISON_PATH = _RESULTS_DIR / "bias_comparison.json"


# ── Prompt construction (reuses the production template) ──────────────────────

def build_prompt(article: dict[str, Any]) -> str:
    """Build the single-article bias prompt exactly as production does.

    Mirrors ``BiasAgent._classify_single_from_payload`` /
    ``BiasAgent.classify_single``: same template, same char budgets, same
    source-derivation fallback. The dataset's ``source`` field is preferred;
    when absent we derive it from the URL via the production helper.
    """
    source = (article.get("source") or "").strip() or _source_from_url(article.get("url"))
    return _BIAS_SINGLE_PROMPT_TEMPLATE.format(
        source=source,
        title=(article.get("title") or "")[:_MAX_TITLE_CHARS],
        content=(article.get("content") or "")[:_MAX_SINGLE_CHARS],
    )


# ── Output parsing ────────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks that thinking-capable models may emit."""
    return _THINK_RE.sub("", text or "").strip()


def extract_label(raw_text: str) -> tuple[str | None, dict | None]:
    """Extract a valid bias label from a raw model response.

    Returns ``(label, parsed_dict)`` when a JSON object with a valid label is
    found; otherwise ``(None, parsed_or_None)``. A ``None`` label is recorded
    upstream as ``parse_error`` and counts as an incorrect prediction
    (per the Task 1 spec — unparseable output is not fatal).
    """
    cleaned = _strip_code_fences(_strip_think(raw_text))
    if not cleaned:
        return None, None

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError):
        m = _JSON_OBJ_RE.search(cleaned)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except (TypeError, ValueError):
                parsed = None

    if not isinstance(parsed, dict):
        return None, None

    label = str(parsed.get("label", "")).strip().lower()
    if label not in _VALID_BIAS_LABELS:
        return None, parsed
    return label, parsed


# ── Model backends ────────────────────────────────────────────────────────────

async def call_ollama(model: str, prompt: str, timeout: float, retries: int) -> dict[str, Any]:
    """Call a local Ollama model via its HTTP API. Never raises (Rule 2.4).

    Returns ``{"text": str}`` on success or ``{"error": str}`` on failure.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": False,  # suppress <think> blocks on thinking-capable Gemma builds
        "options": {"temperature": _TEMPERATURE, "num_predict": _MAX_TOKENS},
    }
    last_err = ""
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return {"text": data.get("response", "") or ""}
        except Exception as e:  # noqa: BLE001 — Rule 2.4: record, do not crash
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
    return {"error": last_err}


_aistudio_client: genai.Client | None = None


def _get_aistudio_client() -> genai.Client:
    global _aistudio_client
    if _aistudio_client is None:
        key = os.environ.get("GEMINI_API_KEY_2", "").strip()
        _aistudio_client = genai.Client(api_key=key)
    return _aistudio_client


async def call_aistudio(model: str, prompt: str, timeout: float, retries: int) -> dict[str, Any]:
    """Call a Google AI Studio Gemma model. Never raises (Rule 2.4).

    Returns ``{"text": str}`` on success or ``{"error": str}`` on failure.
    A per-call timeout is enforced via ``asyncio.wait_for``.
    """
    client = _get_aistudio_client()
    last_err = ""

    def _call() -> str:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=_MAX_TOKENS,
                temperature=_TEMPERATURE,
            ),
        )
        return resp.text or ""

    for attempt in range(retries + 1):
        try:
            await asyncio.sleep(_AISTUDIO_INTERVAL)
            text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
            return {"text": text}
        except Exception as e:  # noqa: BLE001 — Rule 2.4
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                await asyncio.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
    return {"error": last_err}


# ── Flash error classification ────────────────────────────────────────────────
# Mirrors the patterns in agents/llm_client._is_transient_server_error() and
# _is_quota_error(), inlined here to avoid importing the heavy production module.
#
# Retry policy (approved 2026-06-11):
#   • RETRYABLE  — transient server errors (503, 500, "model overloaded") AND
#                  quota/rate-limit errors (429, resource_exhausted). Both clear
#                  quickly under congestion. GEMINI_FLASH_MAX_RETRIES is the ceiling.
#   • PERMANENT  — auth, invalid key, invalid argument, bad request. Fail fast;
#                  do NOT loop — these will not resolve by retrying.
#   • Everything else (content policy, SDK bug, etc.) — also fail fast.

_FLASH_RETRY_FRAGMENTS: tuple[str, ...] = (
    # Transient server errors
    "503", "service unavailable", "servererror", "server_error",
    "500 internal", "internal error", "model overloaded", "overloaded",
    # Quota / rate-limit — treated as retryable (clears within the minute)
    "429", "quota", "rate limit", "rate_limit", "resource_exhausted",
    "resource exhausted", "too many requests",
)

_FLASH_PERMANENT_FRAGMENTS: tuple[str, ...] = (
    "api_key_invalid", "invalid api key", "api key not valid",
    "permission_denied", "unauthenticated", "invalid_argument",
    "400 bad", "401", "403 forbidden",
)


def _is_flash_retryable(exc: BaseException) -> bool:
    """True for transient server errors and quota/rate-limit errors (both retried)."""
    msg = str(exc).lower()
    return any(frag in msg for frag in _FLASH_RETRY_FRAGMENTS)


def _is_flash_permanent(exc: BaseException) -> bool:
    """True for auth, invalid-key, and bad-request errors — fail fast, never retry."""
    msg = str(exc).lower()
    return any(frag in msg for frag in _FLASH_PERMANENT_FRAGMENTS)


async def call_aistudio_flash(model: str, prompt: str, timeout: float) -> dict[str, Any]:
    """Call the Gemini Flash model via AI Studio with a resilient bounded retry loop.

    Unlike ``call_aistudio()``, this helper keeps retrying on **both** transient
    server errors (503, 500 INTERNAL, "model overloaded") **and** quota / rate-limit
    errors (429, resource_exhausted) — all of these clear quickly under congestion.
    Only true permanent errors (auth, invalid key, invalid argument) fail fast and
    are returned as ``{"error": ...}`` without further retries.

    Retry budget: ``GEMINI_FLASH_MAX_RETRIES`` attempts (default 30).
    Backoff per attempt: ``min(60, _RETRY_BACKOFF × 2^attempt + jitter[0, 2])`` s.
    At the default RETRY_BACKOFF of 3.0 s this spans roughly 10–25 minutes total
    before the ceiling is hit, at which point a final ``{"error": ...}`` is returned
    and the caller records ``parse_error`` — the run continues (Rule 2.4).

    The ``--retries`` CLI flag is intentionally ignored for this backend; the retry
    ceiling is controlled exclusively by ``GEMINI_FLASH_MAX_RETRIES``.
    The ``--timeout`` flag still applies to each individual attempt.
    Reuses ``_get_aistudio_client()`` — the same singleton as ``call_aistudio()``.
    """
    client = _get_aistudio_client()

    def _call() -> str:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=_MAX_TOKENS,
                temperature=_TEMPERATURE,
            ),
        )
        return resp.text or ""

    last_err = ""
    for attempt in range(GEMINI_FLASH_MAX_RETRIES):
        try:
            await asyncio.sleep(_AISTUDIO_INTERVAL)
            text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
            return {"text": text}
        except Exception as exc:  # noqa: BLE001 — Rule 2.4
            last_err = f"{type(exc).__name__}: {exc}"

            if _is_flash_permanent(exc):
                logger.error(
                    "Flash permanent error on attempt %d/%d — failing fast: %s",
                    attempt + 1, GEMINI_FLASH_MAX_RETRIES, last_err,
                )
                return {"error": last_err}

            if _is_flash_retryable(exc) or isinstance(exc, asyncio.TimeoutError):
                wait = min(
                    60.0,
                    _RETRY_BACKOFF * (2.0 ** attempt) + random.uniform(0.0, 2.0),
                )
                logger.warning(
                    "Flash transient/quota error (attempt %d/%d, next retry in %.1fs): %s",
                    attempt + 1, GEMINI_FLASH_MAX_RETRIES, wait, last_err,
                )
                await asyncio.sleep(wait)
                continue

            # Non-retryable, non-permanent (content policy, SDK bug, etc.) — fail fast.
            logger.error(
                "Flash non-retryable error on attempt %d/%d — failing fast: %s",
                attempt + 1, GEMINI_FLASH_MAX_RETRIES, last_err,
            )
            return {"error": last_err}

    logger.error(
        "Flash: exhausted %d retries. Last error: %s",
        GEMINI_FLASH_MAX_RETRIES, last_err,
    )
    return {"error": f"Flash exhausted {GEMINI_FLASH_MAX_RETRIES} retries. Last: {last_err}"}


async def classify(spec: dict[str, str], prompt: str, timeout: float, retries: int) -> dict[str, Any]:
    """Dispatch to the correct backend for *spec*."""
    if spec["backend"] == "ollama":
        return await call_ollama(spec["model"], prompt, timeout, retries)
    if spec["backend"] == "aistudio_flash":
        return await call_aistudio_flash(spec["model"], prompt, timeout)
    return await call_aistudio(spec["model"], prompt, timeout, retries)


# ── Metrics (manual — no sklearn/scipy dependency) ───────────────────────────

def compute_metrics(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Compute confusion matrix, per-label P/R/F1, macro-F1, accuracy.

    *pairs* is a list of ``(human_label, predicted_label)``. A predicted label
    not in :data:`LABELS` (e.g. ``parse_error``) is bucketed into the
    ``parse_error`` confusion-matrix column and always counts as incorrect.
    """
    columns = LABELS + [PARSE_ERROR]
    cm: dict[str, dict[str, int]] = {h: {c: 0 for c in columns} for h in LABELS}
    for human, pred in pairs:
        if human not in cm:
            continue
        col = pred if pred in LABELS else PARSE_ERROR
        cm[human][col] += 1

    per_label: dict[str, dict[str, float]] = {}
    macro_f1_sum = 0.0
    for lab in LABELS:
        tp = cm[lab][lab]
        fp = sum(cm[h][lab] for h in LABELS) - tp
        fn = sum(cm[lab][c] for c in columns) - tp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_label[lab] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": tp + fn,
        }
        macro_f1_sum += f1

    total = len(pairs)
    correct = sum(1 for h, p in pairs if h == p)
    parse_errors = sum(1 for _, p in pairs if p not in LABELS)
    macro_f1 = macro_f1_sum / len(LABELS) if LABELS else 0.0

    return {
        "accuracy": round(correct / total, 4) if total else 0.0,
        "macro_f1": round(macro_f1, 4),
        "total": total,
        "correct": correct,
        "parse_errors": parse_errors,
        "per_label": per_label,
        "confusion_matrix": cm,
        "confusion_matrix_columns": columns,
    }


# ── Dataset / output helpers ──────────────────────────────────────────────────

def load_dataset(
    path: Path,
    start_id: int | None,
    stop_id: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    articles = data.get("articles", [])
    # Only articles with a human label participate in F1 (plan.md Step 6.1:
    # exclude annotator confidence below 'high'). All 60 are 'high'.
    usable = [
        a for a in articles
        if a.get("human_label") in _VALID_BIAS_LABELS
        and str(a.get("human_confidence", "")).lower() == "high"
    ]
    # Apply the inclusive [start_id, stop_id] id-range filter FIRST, to the
    # ordered usable list, then apply --limit to cap the remaining count. So
    # --start-id 11 --stop-id 30 --limit 5 yields the first 5 articles in [11, 30].
    if start_id is not None:
        usable = [a for a in usable if a.get("id") is not None and a.get("id") >= start_id]
    if stop_id is not None:
        usable = [a for a in usable if a.get("id") is not None and a.get("id") <= stop_id]
    if limit is not None:
        usable = usable[:limit]
    return usable


def _load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not parse existing %s — starting fresh.", path.name)
    return default


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* crash-safely.

    Serialises to a temp file in the SAME directory (so os.replace is a true
    atomic rename on the same filesystem), then atomically replaces the target.
    An interruption mid-write therefore can never corrupt the existing file —
    the reader sees either the old complete file or the new complete file.
    ``ensure_ascii=False`` keeps Arabic readable; ``indent=2`` preserves format.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    try:
        with tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _upsert_prediction(row: dict[str, Any]) -> None:
    """Persist a single prediction *row* to bias_predictions.json immediately,
    upserting by ``(model, article_id)``.

    Loads the existing file (if any), replaces the row with the same
    ``(model, article_id)`` when present, else appends. ALL other rows — other
    models AND earlier articles of the same model — are preserved, so an
    interrupted run never loses completed work and re-running a sub-range of a
    model replaces only those rows (the old per-model wipe is gone). The write
    is crash-safe (temp file + atomic replace)."""
    existing = _load_json(_PREDICTIONS_PATH, [])
    if not isinstance(existing, list):
        existing = []
    key = (row.get("model"), row.get("article_id"))
    for i, r in enumerate(existing):
        if (r.get("model"), r.get("article_id")) == key:
            existing[i] = row
            break
    else:
        existing.append(row)
    _atomic_write_json(_PREDICTIONS_PATH, existing)


def merge_comparison(new_metrics: dict[str, Any]) -> dict[str, Any]:
    existing = _load_json(_COMPARISON_PATH, {})
    if not isinstance(existing, dict):
        existing = {}
    existing.update(new_metrics)
    return existing


# ── Run loop ──────────────────────────────────────────────────────────────────

async def evaluate_model(
    model_key: str,
    spec: dict[str, str],
    articles: list[dict[str, Any]],
    timeout: float,
    retries: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one model over all *articles*; return (prediction_rows, metrics)."""
    logger.info(
        "=== %s (%s, %s) over %d articles ===",
        model_key, spec["params"], spec["model"], len(articles),
    )
    rows: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = []

    for i, article in enumerate(articles, 1):
        aid = article.get("id")
        human = article.get("human_label")
        prompt = build_prompt(article)

        result = await classify(spec, prompt, timeout, retries)
        raw = result.get("text", "")
        note = result.get("error", "")

        if note:
            predicted = PARSE_ERROR
            confidence = None
            logger.warning("  [%d/%d] id=%s call error: %s", i, len(articles), aid, note)
        else:
            label, parsed = extract_label(raw)
            predicted = label or PARSE_ERROR
            confidence = None
            if parsed is not None:
                try:
                    confidence = float(parsed.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = None
            mark = "ok " if predicted == human else "MISS"
            logger.info(
                "  [%d/%d] id=%s %s human=%s pred=%s",
                i, len(articles), aid, mark, human, predicted,
            )

        row = {
            "model": model_key,
            "article_id": aid,
            "human_label": human,
            "predicted_label": predicted,
            "predicted_confidence": confidence,
            "raw_model_output": raw,
            "call_error": note or None,
        }
        rows.append(row)
        # Incremental crash-safe save: persist this row NOW, upserted by
        # (model, article_id), so an interruption keeps every completed article.
        _upsert_prediction(row)
        pairs.append((human or "", predicted))

    metrics = compute_metrics(pairs)
    metrics["model"] = model_key
    metrics["params"] = spec["params"]
    metrics["model_identifier"] = spec["model"]
    metrics["backend"] = spec["backend"]
    logger.info(
        "--- %s done: accuracy=%.4f macro_f1=%.4f parse_errors=%d ---",
        model_key, metrics["accuracy"], metrics["macro_f1"], metrics["parse_errors"],
    )
    return rows, metrics


def print_comparison_table(comparison: dict[str, Any]) -> None:
    """Print the final comparison table to stdout (plan.md Step 6.2 format)."""
    print("\n" + "=" * 52)
    print(f"{'Model':<12} {'Params':<8} {'Accuracy':<10} {'Macro-F1':<10}")
    print("-" * 52)
    for key in MODELS:  # stable display order
        m = comparison.get(key)
        if not m:
            continue
        print(f"{key:<12} {m.get('params',''):<8} "
              f"{m.get('accuracy',0):<10.2f} {m.get('macro_f1',0):<10.2f}")
    print("=" * 52)


async def main_async(args: argparse.Namespace) -> None:
    selected = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else list(MODELS)
    unknown = [m for m in selected if m not in MODELS]
    if unknown:
        logger.error("Unknown model(s): %s. Valid: %s", unknown, list(MODELS))
        sys.exit(2)

    # Graceful no-op for an inverted range: nothing-to-do is valid, not fatal.
    if args.start_id is not None and args.stop_id is not None and args.start_id > args.stop_id:
        logger.info(
            "start-id (%d) > stop-id (%d): empty range — no articles to evaluate. "
            "Nothing classified, nothing written.",
            args.start_id, args.stop_id,
        )
        return

    articles = load_dataset(_DATASET_PATH, args.start_id, args.stop_id, args.limit)
    if not articles:
        logger.error(
            "No usable articles loaded from %s for the given range/limit "
            "(start-id=%s, stop-id=%s, limit=%s).",
            _DATASET_PATH, args.start_id, args.stop_id, args.limit,
        )
        sys.exit(1)
    logger.info(
        "Loaded %d articles (start-id=%s, stop-id=%s, limit=%s). Models: %s",
        len(articles), args.start_id, args.stop_id, args.limit, selected,
    )

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for model_key in selected:
        # Each article's row is persisted incrementally inside evaluate_model
        # (upsert by (model, article_id), crash-safe atomic write), so there is
        # no separate end-of-run predictions write — the file is already current.
        await evaluate_model(
            model_key, MODELS[model_key], articles, args.timeout, args.retries,
        )

    # Recompute each evaluated model's metrics from ALL rows now present in
    # bias_predictions.json — NOT just this batch — so a batched run (e.g. Flash
    # 11–30, then 31–50, then 51–60) reflects the full accumulated set rather than
    # only the last batch. Same metric math, confusion-matrix logic, and
    # parse_error-counts-as-incorrect rule as before; only the source of the
    # (human_label, predicted_label) pairs changes (file rows vs in-memory batch).
    all_rows = _load_json(_PREDICTIONS_PATH, [])
    if not isinstance(all_rows, list):
        all_rows = []

    new_metrics: dict[str, Any] = {}
    for model_key in selected:
        spec = MODELS[model_key]
        pairs = [
            (r.get("human_label") or "", r.get("predicted_label") or PARSE_ERROR)
            for r in all_rows
            if r.get("model") == model_key
        ]
        metrics = compute_metrics(pairs)
        metrics["model"] = model_key
        metrics["params"] = spec["params"]
        metrics["model_identifier"] = spec["model"]
        metrics["backend"] = spec["backend"]
        new_metrics[model_key] = metrics

    comparison = merge_comparison(new_metrics)
    comparison["_metadata"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(_DATASET_PATH.relative_to(_PROJECT_ROOT)),
        "articles_evaluated": len(articles),
        "labels": LABELS,
        "models": {k: MODELS[k] for k in MODELS},
        "note": "macro_f1 = unweighted mean of per-label F1 over the 5 labels; "
                "parse_error predictions count as incorrect.",
    }
    _atomic_write_json(_COMPARISON_PATH, comparison)

    logger.info("Wrote %s (%d rows)", _PREDICTIONS_PATH.name, len(all_rows))
    logger.info("Wrote %s", _COMPARISON_PATH.name)
    print_comparison_table(comparison)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 6 bias model-comparison harness.")
    p.add_argument(
        "--models", type=str, default="",
        help="Comma-separated subset of: " + ", ".join(MODELS)
             + " (default: all). The annotator can run one model at a time.",
    )
    p.add_argument(
        "--start-id", type=int, default=None,
        help="Lower bound (inclusive): evaluate only articles with id >= N. "
             "Applied BEFORE --limit. Default: no lower bound.",
    )
    p.add_argument(
        "--stop-id", type=int, default=None,
        help="Upper bound (inclusive): evaluate only articles with id <= M. "
             "Applied BEFORE --limit. With --start-id forms the inclusive range "
             "[N, M] for resumable batches (e.g. 11-30, then 31-50). Default: "
             "no upper bound.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Evaluate only the first N articles AFTER the id-range filter "
             "(for quick verification runs).",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help=f"Per-call timeout in seconds (default {DEFAULT_TIMEOUT}).")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                   help=f"Transient-error retries per call (default {DEFAULT_RETRIES}).")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
