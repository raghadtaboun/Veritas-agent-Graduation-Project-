"""
agents/summary_agent.py — Summary Agent (Agent 5) [Canonical MCP rewrite]

Produces two textual outputs per event cluster:

  1. neutral_summary  — a single neutral Arabic paragraph based on shared
                        facts extracted from multi-source coverage.
  2. bias_assessment  — a comparative Arabic analysis describing how each
                        source frames the event differently.

Both outputs are written to ``events.summary`` / ``events.bias_assessment``
via the ``update_event_summary`` MCP tool (Step 4.5.3 — ``COALESCE(NULLIF(…))``
so an empty value preserves the prior column).

Architecture (Phase 4.5 canonical):
  - No ReAct loop, no ``think()``, no ``_fallback_decide``. A deterministic
    ``for`` loop does three sequential Gemini calls per event
    (facts → neutral_summary → bias_assessment).
  - All data / cache operations go through MCP tools: ``get_articles_for_event``
    for article metadata + existing bias rows, ``get_articles`` for article
    content, ``cache_get`` / ``cache_set`` for LLM caching (Rule 2.7),
    ``update_event_summary`` for the write. No direct psycopg2 or google.genai
    imports (Rule 2.3).
  - Gemini is invoked via ``self.call_gemini`` with distinct ``task_type`` values
    (``facts`` / ``summary`` / ``assessment``) — the fallback chain lives in
    ``agents/llm_client.py`` (blueprint.md §9).
  - **Empty-output guard (plan.md Step 4.5.5 point 6):** if BOTH
    neutral_summary and bias_assessment are empty after strip, the
    ``update_event_summary`` call is skipped entirely. The MCP tool
    re-checks this invariant on its end, but the short-circuit here
    avoids the extra round-trip.
  - ``bias_assessment`` column is now part of ``infra/schema.sql`` — the
    legacy ``ALTER TABLE IF NOT EXISTS`` bootstrap (Deviation D) has been
    dropped, matching what the Phase 0 schema already declares.

Public ``run(event_ids)`` signature and return-dict keys are unchanged so
``agents/graph.py`` requires no modification this session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base import MCPAgent

logger = logging.getLogger(__name__)

# ── Cache TTLs (seconds) — Rule 2.7 / blueprint.md §9 ────────────────────────
_TTL_FACTS:           int = 259_200    # 72 h
_TTL_NEUTRAL_SUMMARY: int = 86_400     # 24 h
_TTL_BIAS_ASSESSMENT: int = 259_200    # 72 h

# ── LLM budgets ──────────────────────────────────────────────────────────────
_FACTS_MAX_TOKENS:     int = 2048
_SUMMARY_MAX_TOKENS:   int = 2048
_ASSESSMENT_MAX_TOKENS: int = 2048
_GEN_TEMPERATURE:      float = 0.2

# ── Per-article character budgets (Rule 2.9) ─────────────────────────────────
_MAX_FACTS_ARTICLES:        int = 10
_TOTAL_FACTS_BUDGET:        int = 3000
_MAX_FACTS_PER_ARTICLE:     int = 800
_MAX_TITLE_CHARS:           int = 200
_MAX_FRAMING_CHARS:         int = 200
_MAX_ASSESSMENT_TITLE_CHARS: int = 100
_PROMPT_TRUNCATE:           int = 3000


# ── Prompt templates ─────────────────────────────────────────────────────────
# `_FACTS_PROMPT_TEMPLATE` is PRESERVED VERBATIM from the pre-migration
# `extract_facts` MCP tool (git commit c977c09 / docs/archive/pre_canonical_mcp/).
# Arabic-media domain knowledge lives in these prompts — they must not be
# paraphrased.

_FACTS_PROMPT_TEMPLATE = """\
You are a fact-extraction system for Arabic news analysis (the output should be in Arabic).

The following articles all cover the same news event from different sources.
Identify facts that appear in AT LEAST TWO of the articles (shared facts).
A fact is a verifiable claim about a person, place, event, or outcome.
Omit editorial opinions, framing, and speculation.

Return ONLY a JSON array of fact strings in Arabic.
No markdown, no code fences, no extra text.
Example: ["الحقيقة الأولى", "الحقيقة الثانية"]

{articles_text}"""

_NEUTRAL_SUMMARY_PROMPT = """\
You are a neutral Arabic news summarizer (the output should be in Arabic). Your ONLY task is to write one paragraph.

Shared facts from multiple sources covering the same news event:
{facts_text}

Write a SINGLE paragraph in Arabic that summarizes these facts objectively.

STRICT RULES:
- Do NOT use politically framed, partisan, or biased language of any kind.
- Base your summary ONLY on the listed facts — add no information beyond them.
- Write exactly ONE paragraph. No headers, no bullet points, no markdown.
Return only the paragraph text.\
"""

_BIAS_ASSESSMENT_PROMPT = """\
أنت محلل تحيّز إعلامي متخصص في تغطية الأخبار العربية. مهمتك تحليل كيف تُؤطّر مصادر مختلفة نفس الحدث الإخباري.

المقالات التالية تغطّي حدثاً واحداً من مصادر مختلفة:
{articles_text}

اكتب تحليلاً مقارناً بالعربية الفصحى (3-5 جمل) يوضّح اختلاف التأطير بين المصادر. التزم بالمعايير التالية:

## 1. التسمية الصريحة
- اذكر اسم كل مصدر صراحةً (مثل: "الجزيرة"، "العربية"، "بي بي سي عربي"، "RT عربية")
- لا تستخدم تعابير مبهمة مثل "بعض المصادر" أو "مصادر أخرى"

## 2. تحديد الميل الأيديولوجي
- حدّد طبيعة الميل (مؤيد لطرف، معارض لطرف، حياد ظاهر، انتقاء انتقائي)
- اربط الميل بالسردية الكبرى (مثل: قومية عربية، موالاة للنظام، توافق مع المنظور الغربي، معارضة)

## 3. تقديم أدلّة محدّدة
لكل مصدر، اذكر دليلاً واحداً على الأقل من النص:
- مفردات مُحمَّلة (loaded language) — استخدم اقتباساً قصيراً بين علامتي تنصيص '...'
- اختيار المصادر المُستشهَد بها (إسرائيلية، إيرانية، أمريكية، رسمية، معارضة)
- ما الذي يُبرز وما الذي يُهمَل
- الزاوية المُختارة للقصة (عسكرية، إنسانية، سياسية، اقتصادية)

## 4. المقارنة المباشرة
- استخدم روابط واضحة: "في المقابل"، "بينما"، "على النقيض من ذلك"
- لا تذكر مصدراً معزولاً بدون مقارنته بآخر

## 5. الحدود والتحفّظ الأكاديمي
- لا تدّعِ يقيناً يتجاوز ما يدعمه النص
- إذا كانت المقالات تتشابه في التغطية، قل ذلك صراحةً
- لا تأخذ موقفاً سياسياً — حلّل التأطير، لا تحكم على الحدث

## 6. الشكل
- 3-5 جمل عربية فصحى أكاديمية
- لا عناوين، لا قوائم، لا markdown
- لا تذكر "هذا تحليل..." أو "في الختام..." — ادخل مباشرة في التحليل

## مثال على المخرَج المطلوب
"تظهر الجزيرة ميلاً واضحاً لدعم السردية الفلسطينية، إذ تستخدم مصطلح 'العدوان' لوصف العمليات الإسرائيلية وتُبرز الخسائر المدنية في غزة كعنصر مركزي في القصة، مع اقتباسات مكثّفة من مصادر فلسطينية رسمية. في المقابل، تتبنّى العربية إطاراً أمنياً، إذ تُركّز على 'تهديد حماس' وتعتمد على بيانات الجيش الإسرائيلي والمحلّلين الأمنيين الإقليميين. أما بي بي سي عربي، فتحاول تقديم تغطية متوازنة شكلياً عبر إيراد الروايتين، لكنها تُفصّل في الموقف الأمريكي والأوروبي أكثر من الموقف العربي، ما يعكس انحيازاً ضمنياً نحو المنظور الغربي."

اكتب التحليل المقارن الآن. ابدأ مباشرة بالتحليل دون مقدمات:
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _strip_code_fences(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s.strip())
    return s.strip()


class SummaryAgent(MCPAgent):
    """
    Agent 5 — Summary Agent (canonical MCP, deterministic).

    ``run(event_ids)`` produces neutral_summary + bias_assessment for each
    event, writes them to the events table via ``update_event_summary``,
    and returns a results dict compatible with LangGraph ``NewsState``.
    """

    # ── Public API ────────────────────────────────────────────────────────

    async def run(self, event_ids: list[int]) -> dict[str, Any]:
        """
        Generate summaries for each event in *event_ids*.

        Returns
        -------
        dict with keys:
            summaries    : list[dict]  — one entry per processed event with
                           event_id, neutral_summary, bias_assessment.
            stored_count : int         — events rows updated (rowcount>0).
            errors       : list[str]   — non-fatal error strings.
        """
        if not event_ids:
            return {"summaries": [], "stored_count": 0, "errors": []}

        errors: list[str] = []
        summaries: list[dict[str, Any]] = []
        stored_count: int = 0

        for eid in event_ids:
            try:
                event_id = int(eid)
            except (TypeError, ValueError):
                errors.append(f"skipping non-integer event_id: {eid!r}")
                continue

            # ── Load article metadata + existing bias rows ───────────────
            ev_articles = await self.call_tool(
                "get_articles_for_event", {"event_id": event_id},
            )
            if "error" in ev_articles:
                errors.append(
                    f"get_articles_for_event error for event {event_id}: "
                    f"{ev_articles['error']}"
                )
                continue

            articles_meta = ev_articles.get("articles", []) or []
            if not articles_meta:
                errors.append(f"event {event_id} has no articles linked")
                continue

            # ── Load content for the same article IDs ─────────────────────
            article_ids = [int(a["id"]) for a in articles_meta if "id" in a]
            content_by_id = await self._fetch_content(article_ids)

            # ── Facts (only meaningful for multi-source events) ───────────
            facts: list[str] = []
            if len(articles_meta) >= 2:
                facts, facts_err = await self._extract_facts(
                    event_id=event_id,
                    articles_meta=articles_meta,
                    content_by_id=content_by_id,
                )
                if facts_err:
                    errors.append(facts_err)

            # ── Neutral summary ──────────────────────────────────────────
            neutral_summary, ns_err = await self._generate_neutral_summary(
                event_id=event_id,
                facts=facts,
            )
            if ns_err:
                errors.append(ns_err)

            # ── Bias assessment (requires ≥1 article with a bias row) ────
            bias_assessment, ba_err = await self._generate_bias_assessment(
                event_id=event_id,
                articles_meta=articles_meta,
            )
            if ba_err:
                errors.append(ba_err)

            summaries.append({
                "event_id":        event_id,
                "neutral_summary": neutral_summary,
                "bias_assessment": bias_assessment,
            })

            # ── Empty-output guard — plan.md Step 4.5.5 point 6 ──────────
            if not neutral_summary.strip() and not bias_assessment.strip():
                errors.append(
                    f"event {event_id}: both outputs empty — skipping "
                    "update_event_summary"
                )
                continue

            upd = await self.call_tool("update_event_summary", {
                "event_id":        event_id,
                "neutral_summary": neutral_summary,
                "bias_assessment": bias_assessment,
            })
            if "error" in upd:
                errors.append(
                    f"update_event_summary error for event {event_id}: "
                    f"{upd['error']}"
                )
                continue
            if upd.get("updated"):
                stored_count += 1

        logger.info(
            "[SummaryAgent] events=%d  summaries=%d  stored=%d  errors=%d",
            len(event_ids), len(summaries), stored_count, len(errors),
        )

        return {
            "summaries":    summaries,
            "stored_count": stored_count,
            "errors":       errors,
        }

    # ── Step 1 — facts (Gemini task_type="facts") ─────────────────────────

    async def _extract_facts(
        self,
        event_id: int,
        articles_meta: list[dict[str, Any]],
        content_by_id: dict[int, str],
    ) -> tuple[list[str], str | None]:
        """Extract shared facts across ≥ 2 articles covering *event_id*.

        Returns ``(facts, error_str_or_None)``. Empty facts + ``None`` means
        the event had insufficient material; empty facts + a string means
        the LLM/transport failed and the caller should append the string
        to its ``errors`` list.
        """
        cache_key = f"facts:{event_id}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            try:
                parsed = json.loads(cached["value"])
                if isinstance(parsed, list):
                    return [str(f) for f in parsed if f], None
            except (TypeError, ValueError):
                pass

        pruned = articles_meta[:_MAX_FACTS_ARTICLES]
        per_article = min(
            _MAX_FACTS_PER_ARTICLE,
            _TOTAL_FACTS_BUDGET // max(len(pruned), 1),
        )

        blocks: list[str] = []
        for i, meta in enumerate(pruned, 1):
            aid = int(meta.get("id", 0))
            src = meta.get("source") or "unknown"
            title = (meta.get("title") or "")[:_MAX_TITLE_CHARS]
            content = (content_by_id.get(aid, "") or "")[:per_article]
            blocks.append(
                f"--- Article {i} | Source: {src} ---\n"
                f"Title: {title}\n"
                f"{content}"
            )
        articles_text = "\n\n".join(blocks)

        prompt = _FACTS_PROMPT_TEMPLATE.format(articles_text=articles_text)
        prompt = prompt[:_PROMPT_TRUNCATE]

        gen = await self.call_gemini(
            prompt=prompt,
            task_type="facts",
            max_tokens=_FACTS_MAX_TOKENS,
            temperature=_GEN_TEMPERATURE,
        )
        if "error" in gen:
            return [], f"facts LLM error for event {event_id}: {gen['error']}"

        raw = _strip_code_fences(gen.get("text", ""))
        if not raw:
            return [], f"facts LLM returned empty text for event {event_id}"

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as e:
            return [], f"facts JSON parse failed for event {event_id}: {e}"

        if not isinstance(parsed, list):
            return [], f"facts response was not a JSON array for event {event_id}"

        facts = [str(f) for f in parsed if f]

        try:
            await self.call_tool("cache_set", {
                "key":   cache_key,
                "value": json.dumps(facts, ensure_ascii=False),
                "ttl":   _TTL_FACTS,
            })
        except (TypeError, ValueError):
            pass

        return facts, None

    # ── Step 2 — neutral summary (Gemini task_type="summary") ─────────────

    async def _generate_neutral_summary(
        self,
        event_id: int,
        facts: list[str],
    ) -> tuple[str, str | None]:
        """Build a neutral Arabic paragraph from *facts*.

        Returns ``(summary, error_str_or_None)``. Empty facts list returns
        ``("", None)`` — not an error, just nothing to summarise.
        """
        cache_key = f"neutral_summary:{event_id}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            return str(cached["value"]), None

        if not facts:
            return "", None

        facts_text = "\n".join(f"- {f}" for f in facts[:50])
        prompt = _NEUTRAL_SUMMARY_PROMPT.format(facts_text=facts_text)
        prompt = prompt[:_PROMPT_TRUNCATE]

        gen = await self.call_gemini(
            prompt=prompt,
            task_type="summary",
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=_GEN_TEMPERATURE,
        )
        if "error" in gen:
            return "", f"neutral_summary LLM error for event {event_id}: {gen['error']}"

        text = (gen.get("text") or "").strip()
        if not text:
            return "", None

        await self.call_tool("cache_set", {
            "key":   cache_key,
            "value": text,
            "ttl":   _TTL_NEUTRAL_SUMMARY,
        })
        return text, None

    # ── Step 3 — bias assessment (Gemini task_type="assessment") ──────────

    async def _generate_bias_assessment(
        self,
        event_id: int,
        articles_meta: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        """Comparative Arabic analysis of how sources frame the event.

        Returns ``(assessment, error_str_or_None)``. Returns ``("", None)``
        when the event has no article with a ``bias_scores`` row — that
        is expected for brand-new events whose bias classification failed
        or was filtered by the confidence gate.
        """
        cache_key = f"bias_assessment:{event_id}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            return str(cached["value"]), None

        bias_rows = [
            a for a in articles_meta
            if a.get("label") is not None
        ]
        if not bias_rows:
            return "", None

        blocks: list[str] = []
        for row in bias_rows:
            source  = row.get("source") or "unknown"
            label   = row.get("label") or "unknown"
            conf    = row.get("confidence")
            conf_f  = float(conf) if conf is not None else 0.0
            framing = (row.get("framing") or "")[:_MAX_FRAMING_CHARS]
            title   = (row.get("title") or "")[:_MAX_ASSESSMENT_TITLE_CHARS]
            blocks.append(
                f"Source: {source}\n"
                f"Bias label: {label} (confidence: {conf_f:.2f})\n"
                f"Editorial framing: {framing}\n"
                f"Title: {title}"
            )
        articles_text = "\n\n---\n\n".join(blocks)

        prompt = _BIAS_ASSESSMENT_PROMPT.format(articles_text=articles_text)
        prompt = prompt[:_PROMPT_TRUNCATE]

        gen = await self.call_gemini(
            prompt=prompt,
            task_type="assessment",
            max_tokens=_ASSESSMENT_MAX_TOKENS,
            temperature=_GEN_TEMPERATURE,
        )
        if "error" in gen:
            return "", f"bias_assessment LLM error for event {event_id}: {gen['error']}"

        text = (gen.get("text") or "").strip()
        if not text:
            return "", None

        await self.call_tool("cache_set", {
            "key":   cache_key,
            "value": text,
            "ttl":   _TTL_BIAS_ASSESSMENT,
        })
        return text, None

    # ── Data helper ───────────────────────────────────────────────────────

    async def _fetch_content(self, article_ids: list[int]) -> dict[int, str]:
        """Return ``{article_id: content}`` for the given IDs via ``get_articles``.

        Missing or errored rows are silently omitted — downstream prompts
        fall back to using the title alone for that article.
        """
        if not article_ids:
            return {}

        result = await self.call_tool("get_articles", {"ids": list(article_ids)})
        if "error" in result:
            logger.warning(
                "[SummaryAgent._fetch_content] get_articles error: %s",
                result["error"],
            )
            return {}

        out: dict[int, str] = {}
        for row in result.get("articles", []) or []:
            try:
                aid = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            out[aid] = row.get("content", "") or ""
        return out
