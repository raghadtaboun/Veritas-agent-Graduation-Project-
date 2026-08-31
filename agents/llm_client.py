"""
agents/llm_client.py — Centralised client-side LLM dispatch (Phase 4.5 Step 4.5.2;
multi-key pool added Phase 5 Pre-Work 2026-05-01).

This module is the *single* place in the canonical-MCP architecture where
LLM provider SDKs are imported and called. Every LLM call originating from
an agent flows through one of three async functions defined here:

    gemini_generate_with_fallback(prompt, task_type, max_tokens, temperature)
    gemini_embed(text)
    groq_generate(prompt, model, max_tokens, temperature)

Agents do not import these functions directly. They call the thin
``call_gemini`` / ``call_gemini_embedding`` / ``call_groq`` methods on the
``MCPAgent`` base class, which delegate here. This keeps the per-agent
surface area minimal and concentrates fallback / throttling / key-isolation
logic in one auditable file.

Design constraints enforced (per ``blueprint.md`` Section 9 and
``plan.md`` Step 4.5.2):

* **Multi-key round-robin pool.** Eight Gemini API keys loaded from
  ``GEMINI_API_KEY`` through ``GEMINI_API_KEY_8``. Every request uses the
  next key in rotation; the pointer advances only on success.
* **Per-key quarantine.** A 429 / quota error quarantines the offending key
  for 1 hour. The same request retries with the next available key for the
  same model before falling back to the next model tier.
* **Per-task model strategy.** Every task tier (bias, summary, assessment,
  entities, facts, general) currently resolves to ``gemma-4-31b-it``. See
  ``GEMINI_FALLBACK_CHAIN``.
* **Transient server error retry.** ``gemma-4-31b-it`` (and previously
  ``gemini-2.5-flash``) returns 503 Service Unavailable and 500 INTERNAL
  under load. The pool retries 3 times with a 5 s sleep on the *same key*
  before treating the failure as a model-level issue and advancing the
  chain. Neither 503 nor 500 INTERNAL quarantines the key.
* **Key masking.** API keys are always logged as ``AIza***xyz``
  (first 4 + last 3 chars). Full keys are never logged under any circumstance.
* **Concurrency limits.** ``asyncio.Semaphore(4)`` for Gemini,
  ``asyncio.Semaphore(2)`` for Groq.
* **Minimum-interval throttle.** 250 ms between consecutive Gemini calls;
  2.1 s between consecutive Groq calls (30 RPM free-tier ceiling). With
  8-key rotation the effective per-key interval is ~2 s — well within the
  15 RPM limit for gemma-4-31b-it.
* **3,000-character truncation** of every prompt / embedding input before
  the API call (Rule 2.9).
* **Never raises.** All public functions catch every exception and return
  a structured dict with an ``"error"`` key on failure (Rule 2.4).

This module assumes ``config.env_bootstrap.bootstrap_env()`` has already
run in the calling process. Importing it before the bootstrap will pick
up the wrong API key and is a programming error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import google.genai as genai
import google.genai.types as genai_types
from groq import AsyncGroq

logger = logging.getLogger(__name__)

# ── Truncation ───────────────────────────────────────────────────────────────
_MAX_INPUT_CHARS: int = 100_000  # Rule 2.9


# ── Per-model rate-limit constants ───────────────────────────────────────────
# Used for documentation and quota-error log clarity. The pool does not
# enforce these limits; they serve as reference values so operators can
# understand which tier was exhausted without consulting external docs.
GEMINI_MODEL_LIMITS: dict[str, dict[str, int]] = {
    "gemini-2.0-flash":     {"rpm": 15,  "rpd": 1500, "tpm": 1_000_000},
    "gemini-2.5-flash":     {"rpm": 5,   "rpd": 20,   "tpm": 250_000},
    "gemini-1.5-flash":     {"rpm": 15,  "rpd": 1500, "tpm": 1_000_000},  # 404 on this account
    "text-embedding-004":   {"rpm": 100, "rpd": 1000, "tpm": 30_000},
    "gemini-embedding-001": {"rpm": 100, "rpd": 1000, "tpm": 30_000},
    "gemma-4-31b-it": {"rpm": 15, "rpd": 1000, "tpm": 1_000_000},
}


# ── Per-task fallback chains ──────────────────────────────────────────────────
# Every task type currently resolves to a single model, gemma-4-31b-it.
# Resilience comes from the multi-key round-robin pool (a 429 / quota error
# rotates to the next key for the same model), not from a multi-model cascade.
# Earlier Gemini tiers (gemini-2.0-flash / gemini-2.5-flash) were superseded;
# gemini-1.5-flash was removed earlier as it returns 404 NOT_FOUND on this
# account (Phase 4.5 Step 4.5.5b / summary.md Section E issue #1).
GEMINI_FALLBACK_CHAIN: dict[str, list[str]] = {
    "bias":       ["gemma-4-31b-it"],
    "summary":    ["gemma-4-31b-it"],
    "assessment": ["gemma-4-31b-it"],
    "entities":   ["gemma-4-31b-it"],
    "facts":      ["gemma-4-31b-it"],
    "general":    ["gemma-4-31b-it"],
}


# ── Embedding model ───────────────────────────────────────────────────────────
# text-embedding-004 is the plan.md default; the project's .env overrides to
# gemini-embedding-001 because text-embedding-004 is not available on this key.
_EMBEDDING_MODEL: str = os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")
_EMBEDDING_DIM: int = 768


# ── Groq configuration ────────────────────────────────────────────────────────
_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# ── Concurrency + throttle primitives (lazy so .env wins on first use) ───────
_GEMINI_SEMAPHORE: asyncio.Semaphore | None = None
_GROQ_SEMAPHORE: asyncio.Semaphore | None = None
# 250 ms minimum interval is adequate with 8-key rotation; the natural
# spread across keys keeps each key well within its RPM ceiling.
_MIN_GEMINI_INTERVAL: float = 0.25
_GROQ_INTERVAL_SECS: float = 2.1  # 30 RPM free-tier ceiling
_gemini_throttle_lock: asyncio.Lock | None = None
_groq_throttle_lock: asyncio.Lock | None = None
_last_gemini_call: float = 0.0
_last_groq_call: float = 0.0


def _get_gemini_semaphore() -> asyncio.Semaphore:
    global _GEMINI_SEMAPHORE
    if _GEMINI_SEMAPHORE is None:
        _GEMINI_SEMAPHORE = asyncio.Semaphore(4)
    return _GEMINI_SEMAPHORE


def _get_groq_semaphore() -> asyncio.Semaphore:
    global _GROQ_SEMAPHORE
    if _GROQ_SEMAPHORE is None:
        _GROQ_SEMAPHORE = asyncio.Semaphore(2)
    return _GROQ_SEMAPHORE


def _get_gemini_throttle_lock() -> asyncio.Lock:
    global _gemini_throttle_lock
    if _gemini_throttle_lock is None:
        _gemini_throttle_lock = asyncio.Lock()
    return _gemini_throttle_lock


def _get_groq_throttle_lock() -> asyncio.Lock:
    global _groq_throttle_lock
    if _groq_throttle_lock is None:
        _groq_throttle_lock = asyncio.Lock()
    return _groq_throttle_lock


async def _wait_gemini_interval() -> None:
    """Sleep until at least ``_MIN_GEMINI_INTERVAL`` since the last call."""
    global _last_gemini_call
    async with _get_gemini_throttle_lock():
        elapsed = time.monotonic() - _last_gemini_call
        if elapsed < _MIN_GEMINI_INTERVAL:
            await asyncio.sleep(_MIN_GEMINI_INTERVAL - elapsed)
        _last_gemini_call = time.monotonic()


async def _wait_groq_interval() -> None:
    """Sleep until at least ``_GROQ_INTERVAL_SECS`` since the last call."""
    global _last_groq_call
    async with _get_groq_throttle_lock():
        elapsed = time.monotonic() - _last_groq_call
        if elapsed < _GROQ_INTERVAL_SECS:
            await asyncio.sleep(_GROQ_INTERVAL_SECS - elapsed)
        _last_groq_call = time.monotonic()


# ── Security helpers ──────────────────────────────────────────────────────────
def _mask_key(key: str) -> str:
    """Return ``AIza***xyz`` — first 4 chars + '***' + last 3 chars.

    Never log a raw API key under any circumstance (Rule 2.6).
    """
    if not key or len(key) < 7:
        return "***"
    return f"{key[:4]}***{key[-3:]}"


# ── Error classification ──────────────────────────────────────────────────────
def _is_quota_error(exc: BaseException) -> bool:
    """True when *exc* looks like a 429 / quota / rate-limit error."""
    msg = str(exc).lower()
    return (
        "429" in msg
        or "quota" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "resource_exhausted" in msg
        or "resource exhausted" in msg
        or "too many requests" in msg
    )


def _is_transient_server_error(exc: BaseException) -> bool:
    """True when *exc* looks like a transient 503 or 500 INTERNAL ServerError.

    503 Service Unavailable and 500 INTERNAL are both server-side transients
    from Google. Same retry policy applies: 3 attempts × 5 s sleep on the
    same key, no quarantine. Checked *before* ``_is_quota_error`` in the
    exception handler to ensure mutual exclusivity — transient errors must
    never trigger key quarantine.
    """
    msg = str(exc).lower()
    return (
        "503" in msg
        or "service unavailable" in msg
        or "servererror" in msg
        or "server_error" in msg
        or "500 internal" in msg
        or "internal error" in msg
    )


def _truncate(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= _MAX_INPUT_CHARS:
        return text
    return text[:_MAX_INPUT_CHARS]


# ── Key pool ──────────────────────────────────────────────────────────────────
class GeminiKeyPool:
    """Round-robin pool of Gemini API keys with per-key 1-hour quarantine.

    Thread model: asyncio-only (no threading.Lock needed). All mutable state
    is protected by ``self._lock`` (asyncio.Lock). ``status()`` is an
    unprotected best-effort snapshot for monitoring and verification scripts.
    """

    _QUARANTINE_SECS: int = 3600  # 1 hour

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("GeminiKeyPool requires at least one API key")
        self._keys: list[str] = keys
        self._clients: list[genai.Client] = [genai.Client(api_key=k) for k in keys]
        self._quarantined: dict[str, float] = {}  # key → quarantine_until (monotonic)
        self._index: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    def _release_expired_unlocked(self) -> None:
        """Release quarantine entries whose timer has expired. Must be called under lock."""
        now = time.monotonic()
        expired = [k for k, until in self._quarantined.items() if now >= until]
        for k in expired:
            del self._quarantined[k]
            logger.info(
                "[gemini_pool] key %s quarantine expired — back in rotation",
                _mask_key(k),
            )

    async def get_available_key(
        self, skip_keys: set[str] | None = None
    ) -> tuple[str, genai.Client] | None:
        """Return ``(key, client)`` at or after the current rotation index.

        Skips quarantined keys and any key in *skip_keys*. Does **not** advance
        the rotation pointer — call ``record_success()`` after a successful API
        call. Returns ``None`` if no key is available (all quarantined or all
        in *skip_keys*).
        """
        async with self._lock:
            self._release_expired_unlocked()
            n = len(self._keys)
            for i in range(n):
                idx = (self._index + i) % n
                key = self._keys[idx]
                if key in self._quarantined:
                    continue
                if skip_keys and key in skip_keys:
                    continue
                return key, self._clients[idx]
            return None

    async def record_success(self, key: str) -> None:
        """Advance the rotation pointer one position past *key*."""
        async with self._lock:
            try:
                idx = self._keys.index(key)
                self._index = (idx + 1) % len(self._keys)
            except ValueError:
                pass

    async def quarantine_key(self, key: str) -> int:
        """Quarantine *key* for ``_QUARANTINE_SECS`` (1 hour).

        Quarantine is per-key, not per-(key, model). A key that hits 2.5 RPD
        is treated as exhausted for all models — they share the daily project
        quota in this account configuration. If empirical data shows independent
        per-model quotas, elevate to per-(key, model) quarantine in a future PR.

        Returns the count of remaining available keys after quarantine.
        """
        async with self._lock:
            self._release_expired_unlocked()
            self._quarantined[key] = time.monotonic() + self._QUARANTINE_SECS
            available = sum(1 for k in self._keys if k not in self._quarantined)
            return available

    def status(self) -> dict[str, Any]:
        """Best-effort synchronous status snapshot for monitoring and verification."""
        now = time.monotonic()
        quarantined_keys = [k for k, until in self._quarantined.items() if now < until]
        return {
            "total_keys": len(self._keys),
            "quarantined": len(quarantined_keys),
            "available": len(self._keys) - len(quarantined_keys),
            "rotation_index": self._index,
            "quarantined_masked": [_mask_key(k) for k in quarantined_keys],
        }


def _load_gemini_keys() -> list[str]:
    """Load Gemini API keys from environment in slot order.

    Checks ``GEMINI_API_KEY`` (slot 1) then ``GEMINI_API_KEY_2`` …
    ``GEMINI_API_KEY_10`` (slots 2–10). For each slot, also checks the
    ``Gemini_API_KEY_{N}`` variant (literal lowercase-'emini' prefix) as
    written in some ``.env`` files. Deduplicates identical values.
    """
    keys: list[str] = []
    seen: set[str] = set()

    for name in ("GEMINI_API_KEY", "Gemini_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val and val not in seen:
            keys.append(val)
            seen.add(val)
            break

    for n in range(2, 11):
        for name in (f"GEMINI_API_KEY_{n}", f"Gemini_API_KEY_{n}"):
            val = os.environ.get(name, "").strip()
            if val and val not in seen:
                keys.append(val)
                seen.add(val)
                break

    return keys


_gemini_pool: GeminiKeyPool | None = None


def _get_gemini_pool() -> GeminiKeyPool:
    """Return the singleton ``GeminiKeyPool``, initializing it on first call.

    Synchronous (no ``await``) so it can be used in verification scripts and
    module-level initializations — matches the existing pattern of
    ``_get_gemini_semaphore()`` and ``_get_gemini_throttle_lock()``.

    Raises ``RuntimeError`` if no Gemini API keys are found in the environment.
    Assumes ``bootstrap_env()`` has already run in the calling process.
    """
    global _gemini_pool
    if _gemini_pool is None:
        keys = _load_gemini_keys()
        if not keys:
            raise RuntimeError(
                "agents.llm_client: no Gemini API keys found in environment "
                "— did bootstrap_env() run before this module's first call?"
            )
        _gemini_pool = GeminiKeyPool(keys)
        logger.info(
            "[gemini_pool] initialized with %d key(s), 0 quarantined", len(keys)
        )
    return _gemini_pool


# ── Groq client (lazy singleton) ─────────────────────────────────────────────
_groq_client: AsyncGroq | None = None


def _get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "agents.llm_client: GROQ_API_KEY missing from environment "
                "— did bootstrap_env() run before this module's first call?"
            )
        _groq_client = AsyncGroq(api_key=api_key)
    return _groq_client


# ── Public API ────────────────────────────────────────────────────────────────


async def gemini_generate_with_fallback(
    prompt: str,
    task_type: str = "general",
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Run a Gemini text-generation call with tiered model fallback and key rotation.

    Pool behavior:

    * **Round-robin.** The rotation pointer advances on each successful call so
      load is distributed evenly across the 8-key pool.
    * **Quota failover.** A 429 / resource_exhausted error quarantines the key
      for 1 hour and retries with the next available key for the *same model*
      before advancing to the next model tier in the chain.
    * **Transient retry.** A 503 Service Unavailable or 500 INTERNAL error
      retries up to 3 times with a 5 s sleep on the *same key* (it is not the
      key's fault). If all 3 retries fail, the model tier is exhausted and the
      chain advances. Neither 503 nor 500 INTERNAL quarantines the key.
    * **Non-quota, non-transient errors** return immediately — they indicate a
      prompt or SDK problem, not a capacity problem.
    * **All-keys-quarantined** returns a structured error dict (Rule 2.4 —
      never raises).

    Returns
    -------
    dict
        Success: ``{"text": str, "model_used": str, "key_used": str,
                    "attempts": int}``.
        Failure: ``{"error": str, "attempted": list[str]}``.

    Never raises (Rule 2.4).
    """
    truncated = _truncate(prompt)
    chain = GEMINI_FALLBACK_CHAIN.get(task_type) or GEMINI_FALLBACK_CHAIN["general"]

    try:
        pool = _get_gemini_pool()
    except Exception as exc:
        return {"error": f"gemini pool init failed: {exc}", "attempted": []}

    semaphore = _get_gemini_semaphore()
    attempted: list[str] = []
    last_error: str = ""

    for model in chain:
        tried_keys: set[str] = set()
        skip_to_next_model = False

        while True:
            key_result = await pool.get_available_key(skip_keys=tried_keys)

            if key_result is None:
                if not tried_keys:
                    # Pool is fully quarantined before the first key attempt.
                    return {
                        "error": (
                            "all Gemini keys are quarantined — pool exhausted; "
                            "try again after the 1-hour quarantine window"
                        ),
                        "attempted": attempted,
                    }
                break  # All available keys tried for this model; advance chain.

            key, client = key_result
            tried_keys.add(key)
            masked = _mask_key(key)
            attempted.append(f"{model}/{masked}")
            quota_hit = False

            # ── Transient error retry loop (same key, up to 3 attempts) ─────
            for attempt_num in range(1, 4):
                await _wait_gemini_interval()

                def _call(m: str = model, c: genai.Client = client) -> str:
                    response = c.models.generate_content(
                        model=m,
                        contents=truncated,
                        config=genai_types.GenerateContentConfig(
                            max_output_tokens=max_tokens,
                            temperature=temperature,
                        ),
                    )
                    return response.text or ""

                try:
                    async with semaphore:
                        text = await asyncio.to_thread(_call)

                    logger.info(
                        "[gemini_generate] model=%s key=%s task=%s",
                        model, masked, task_type,
                    )
                    await pool.record_success(key)
                    return {
                        "text": text,
                        "model_used": model,
                        "key_used": masked,
                        "attempts": len(attempted),
                    }

                except Exception as exc:
                    if _is_transient_server_error(exc):
                        if attempt_num < 3:
                            logger.warning(
                                "transient server error on %s (attempt %d/3, key=%s)"
                                " — retrying in 5s",
                                model, attempt_num, masked,
                            )
                            await asyncio.sleep(5)
                            continue  # retry same key, same model
                        else:
                            logger.warning(
                                "transient server error on %s exhausted 3 retries"
                                " (key=%s) — advancing to next model",
                                model, masked,
                            )
                            last_error = (
                                f"transient server error after 3 retries on {model} (key={masked})"
                            )
                            skip_to_next_model = True
                            break  # break attempt loop

                    elif _is_quota_error(exc):
                        limits = GEMINI_MODEL_LIMITS.get(model, {})
                        remaining = await pool.quarantine_key(key)
                        logger.warning(
                            "key %s hit quota on %s "
                            "(limit: %s RPM / %s RPD) — quarantining; "
                            "%d key(s) remaining",
                            masked, model,
                            limits.get("rpm", "?"), limits.get("rpd", "?"),
                            remaining,
                        )
                        last_error = f"quota on {model} (key={masked})"
                        quota_hit = True
                        break  # break attempt loop; continue key rotation

                    else:
                        last_error = f"{type(exc).__name__}: {exc}"
                        logger.error(
                            "[gemini_generate_with_fallback] %s non-quota error"
                            " (task=%s, key=%s): %s",
                            model, task_type, masked, exc,
                        )
                        return {"error": last_error, "attempted": attempted}

            # ── Post-attempt-loop routing ─────────────────────────────────────
            if skip_to_next_model:
                break  # break while-True; advance model chain below

            if quota_hit:
                continue  # continue while-True; try next key for same model

            # Fallthrough guard — should not occur in normal flow.
            break

        if skip_to_next_model:
            continue  # continue for-model loop; try next model tier

        # All available keys exhausted for this model (quota); try next model.

    return {
        "error": (
            f"gemini fallback chain exhausted for task={task_type}"
            f" (last error: {last_error})"
        ),
        "attempted": attempted,
    }


async def gemini_embed(text: str) -> dict[str, Any]:
    """Generate a single 768-dim Gemini embedding with pool key rotation.

    Uses round-robin key selection from the pool. Quota errors quarantine the
    key and retry with the next available key. Other errors return immediately
    with an error dict.

    Returns
    -------
    dict
        Success: ``{"embedding": list[float], "dimension": 768,
                    "model_used": str, "key_used": str}``.
        Failure: ``{"error": str}``.

    Never raises (Rule 2.4).
    """
    truncated = _truncate(text)
    if not truncated:
        return {"error": "gemini_embed: empty input text after truncation"}

    try:
        pool = _get_gemini_pool()
    except Exception as exc:
        return {"error": f"gemini pool init failed: {exc}"}

    semaphore = _get_gemini_semaphore()
    tried_keys: set[str] = set()

    while True:
        key_result = await pool.get_available_key(skip_keys=tried_keys)
        if key_result is None:
            return {
                "error": (
                    "gemini_embed: no keys available "
                    "(all quarantined or all tried without success)"
                )
            }

        key, client = key_result
        tried_keys.add(key)
        masked = _mask_key(key)
        await _wait_gemini_interval()

        def _call(c: genai.Client = client) -> list[float]:
            result = c.models.embed_content(
                model=_EMBEDDING_MODEL,
                contents=truncated,
                config=genai_types.EmbedContentConfig(
                    output_dimensionality=_EMBEDDING_DIM,
                ),
            )
            if not result.embeddings:
                raise ValueError("gemini returned no embeddings")
            values = result.embeddings[0].values
            if not values:
                raise ValueError("gemini returned an empty embedding vector")
            return list(values)

        try:
            async with semaphore:
                values = await asyncio.to_thread(_call)

            await pool.record_success(key)
            logger.info(
                "[gemini_embed] model=%s key=%s dim=%d",
                _EMBEDDING_MODEL, masked, len(values),
            )
            return {
                "embedding": values,
                "dimension": len(values),
                "model_used": _EMBEDDING_MODEL,
                "key_used": masked,
            }

        except Exception as exc:
            logger.error(
                "[gemini_embed] %s (key=%s): %s",
                type(exc).__name__, masked, exc,
            )
            if _is_quota_error(exc):
                limits = GEMINI_MODEL_LIMITS.get(_EMBEDDING_MODEL, {})
                remaining = await pool.quarantine_key(key)
                logger.warning(
                    "key %s hit quota on %s "
                    "(limit: %s RPM / %s RPD) — quarantining; "
                    "%d key(s) remaining",
                    masked, _EMBEDDING_MODEL,
                    limits.get("rpm", "?"), limits.get("rpd", "?"),
                    remaining,
                )
                continue  # try next key
            return {"error": f"{type(exc).__name__}: {exc}"}


async def groq_generate(
    prompt: str,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Single Groq text-generation call with concurrency + interval throttling.

    Returns
    -------
    dict
        Success: ``{"text": str, "model_used": str}``.
        Failure: ``{"error": str, "model_used": str}``.

    Never raises.
    """
    truncated = _truncate(prompt)
    target_model = model or _GROQ_MODEL

    try:
        client = _get_groq_client()
    except Exception as exc:
        return {"error": f"groq client init failed: {exc}", "model_used": target_model}

    semaphore = _get_groq_semaphore()
    await _wait_groq_interval()

    try:
        async with semaphore:
            response = await client.chat.completions.create(
                model=target_model,
                messages=[{"role": "user", "content": truncated}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        choices = response.choices or []
        if not choices:
            return {"error": "groq returned no choices", "model_used": target_model}
        text = choices[0].message.content or ""
        return {"text": text, "model_used": target_model}
    except Exception as exc:
        logger.error("[groq_generate] %s: %s", type(exc).__name__, exc)
        return {"error": f"{type(exc).__name__}: {exc}", "model_used": target_model}


__all__ = [
    "GEMINI_FALLBACK_CHAIN",
    "GEMINI_MODEL_LIMITS",
    "GeminiKeyPool",
    "gemini_generate_with_fallback",
    "gemini_embed",
    "groq_generate",
    "_get_gemini_pool",
    "_mask_key",
]
