"""
agents/bias_agent.py — Bias Agent (Agent 3) [Canonical MCP rewrite]

Classifies political bias for every article in a pipeline batch. Multi-
article event clusters use *comparative* classification (the LLM reads
every source covering the same event before classifying any single one);
singletons fall back to a one-article prompt.

Architecture (Phase 4.5 canonical):
  - No ReAct loop, no ``think()``, no ``_fallback_decide``. A deterministic
    ``for`` loop walks the task queue — one LLM call per target article.
  - All data access goes through MCP tools: ``get_articles`` to fetch the
    article payloads the LLM must see, ``insert_bias_score`` to persist
    the classification, ``cache_get`` / ``cache_set`` for LLM caching
    (Rule 2.7). No direct psycopg2, redis, or httpx imports (Rule 2.3).
  - Gemini is invoked via ``self.call_gemini(task_type="bias")`` which
    delegates to ``agents/llm_client.py`` (Rule 2.3). The centralized
    client owns the per-task fallback chain (blueprint.md §9).
  - **Confidence gate (plan.md Step 4.5.5 point 5):** results where
    ``label == "neutral"`` and ``confidence < 0.2`` are treated as the
    API-degradation signature (the pre-migration ``_BIAS_FALLBACK``
    returned ``neutral / 0.0 / 0.0`` on any error). Those rows are
    neither persisted nor included in ``bias_results``; the reason is
    appended to ``errors`` for auditability. Without this filter, a
    Gemini quota outage would pollute the ``bias_scores`` distribution
    with synthetic neutrals and degrade Phase 6 evaluation F1.
  - ``classify_single(text)`` is a thin single-article wrapper used by
    ``evaluation/evaluate.py`` (blueprint.md §11).

Public ``run(article_ids, event_clusters)`` signature is unchanged so
``agents/graph.py`` needs no modification this session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base import MCPAgent

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_VALID_BIAS_LABELS: frozenset[str] = frozenset({
    "pro_government", "opposition", "neutral", "pan_arab", "western_aligned",
})

# Confidence-gate thresholds (plan.md Step 4.5.5 point 5)
_DEGRADATION_NEUTRAL_LABEL: str = "neutral"
_DEGRADATION_MIN_CONFIDENCE: float = 0.2

_TTL_BIAS: int = 259_200   # 72 h — Rule 2.7 / blueprint.md §9
_BIAS_MAX_TOKENS: int = 4096
_BIAS_TEMPERATURE: float = 0.0

# Per-article character budgets (Rule 2.9)
_MAX_COMPARATIVE_CHARS: int = 800
_TOTAL_COMPARATIVE_BUDGET: int = 3000
_MAX_SINGLE_CHARS: int = 1500
_MAX_TITLE_CHARS: int = 200

# Synthetic article id used by classify_single() — never a real DB row.
_EVAL_SINGLE_ARTICLE_ID: int = 1


# ── Prompt templates ─────────────────────────────────────────────────────────
# PRESERVED VERBATIM from the pre-migration `classify_bias` MCP tool
# (git commit c977c09). Arabic-media framing vocabulary is encoded here
# and MUST NOT be paraphrased.

_BIAS_COMPARATIVE_PROMPT_TEMPLATE = """\
You are a comparative political bias classifier specialized
in Arabic-language news media.

The following {n} articles all cover the SAME news event
from different sources. Read all of them before classifying.

{articles_text}

═══ STEP 1 — COMPARATIVE READING ═══

Compare all {n} articles and identify:

- EMPHASIS: What does each article foreground in its headline and lead?
- OMISSION: What facts in OTHER sources are absent or buried in each?
- VOCABULARY: What loaded Arabic terms does each use? Common contrasts:
    'العدوان' vs 'العملية العسكرية'
    'المقاومة' vs 'الإرهابيون'
    'الشرعية' vs 'النظام الحاكم'
    'الإصلاحات' vs 'التغييرات'
- SOURCING: Whom does each quote (officials, opposition, foreign govs, NGOs, eyewitnesses)?
- NARRATIVE: Who is implicitly cast as legitimate vs illegitimate?

═══ STEP 2 — CLASSIFY ARTICLE {target_index} (Source: {target_source}) ═══

Choose EXACTLY ONE label. Indicators per label:

- pro_government — Supports or amplifies official government authority.
  Indicators: quotes official spokespeople; regime-favored terminology;
  frames protests as 'sedition'; emphasizes stability; downplays opposition.

- opposition — Critiques or counters government authority.
  Indicators: quotes dissidents; emphasizes regime failures, corruption,
  rights abuses; uses 'سلطوي', 'قمع'; foregrounds accountability demands.

- neutral — Balanced without detectable political lean.
  Indicators: cites multiple perspectives proportionally; descriptive
  (not evaluative) language; reports facts without partisan framing.

- pan_arab — Reflects pan-Arab nationalist framing.
  Indicators: 'الأمة العربية', 'القضية المركزية'; frames events as Arab
  vs foreign powers; valorizes resistance; suspicious of Western/Israeli motives.

- western_aligned — Reflects Western institutional narratives.
  Indicators: cites Western governments, think tanks, NGOs prominently;
  uses human rights / democracy frames; treats Western policy as
  default-legitimate; aligns with US/EU positions.

═══ STEP 3 — SCORE, CONFIDENCE, FRAMING ═══

▸ score (continuous bias intensity, -1.0 to +1.0):
    +1.0  = strongly aligned with chosen label
    +0.5  = clear alignment with some balancing elements
     0.0  = label fits weakly, near-neutral
    -0.5  = article actively pushes AGAINST the label's typical framing
    -1.0  = strong counter-framing
    
    For label='neutral', score must be in [-0.2, +0.2].

▸ confidence (your certainty about the label, 0.0 to 1.0):
    0.9-1.0  Multiple strong indicators converge (vocabulary + sources + framing)
    0.7-0.89 Clear lean with 1-2 solid indicators
    0.5-0.69 Detectable lean, indicators are subtle or partial
    0.2-0.49 Weak/ambiguous lean — prefer 'neutral' instead
    < 0.2    DO NOT USE. If genuinely uncertain, choose 'neutral' with confidence ≥ 0.5.

▸ framing (one Arabic sentence):
    Must NAME THE SOURCE explicitly (e.g., 'الجزيرة', 'العربية') and cite
    SPECIFIC EVIDENCE from the target article (a quoted phrase, a sourcing
    pattern, an omission) that distinguishes it from the OTHER sources.

  GOOD framing examples:
    ✓ "تبرز الجزيرة 'العدوان الإسرائيلي' وتعتمد بشكل مكثّف على المصادر الفلسطينية، بينما تتجاهل الموقف الأمني الذي تركّز عليه العربية."
    ✓ "تستخدم العربية مصطلح 'العملية العسكرية' وتقتبس من المتحدث العسكري الإسرائيلي والمحلّلين الأمنيين، مما يعكس إطاراً أمنياً مختلفاً عن السردية الإنسانية للجزيرة."

  BAD framing (do not write like this):
    ✗ "المقالة منحازة"
    ✗ "تختلف عن المصادر الأخرى"

═══ OUTPUT FORMAT ═══

Return ONLY a JSON object — no markdown, no commentary, no preamble. 
You MUST complete your internal analysis inside the "analysis" field before micro-deciding the final label.

{{
  "analysis": {{
    "loaded_vocabulary": "<Brief note on found Arabic judgment words or 'none'>",
    "sourcing_pattern": "<Who is quoted vs who is omitted>",
    "emphasis_points": "<What was foregrounded in headline/lead>"
  }},
  "label": "<one of: pro_government | opposition | neutral | pan_arab | western_aligned>",
  "score": <float -1.0 to +1.0. For 'neutral', score must be 0.0>,
  "confidence": <float 0.0 to 1.0. High confidence is allowed for clear factual neutral reporting>,
  "framing": "<one Arabic sentence naming the source and citing specific evidence>"
}}
"""

_BIAS_SINGLE_PROMPT_TEMPLATE = """\
You are a political bias classifier specialized
in Arabic-language news media.

You are analyzing a SINGLE article without sibling articles for comparison.
Bias detection therefore relies entirely on internal signals: vocabulary,
sourcing, emphasis, and omission within the article itself.

--- Article 1 | Source: {source} ---
Title: {title}
{content}

═══ READING PRINCIPLES (apply BEFORE classifying) ═══

1. EDITORIAL VOICE ONLY — separate the outlet's own narration from quoted actors.
   The label reflects how the OUTLET frames events in its own words (narration,
   word choice, emphasis, omission), NOT the opinions of people it quotes.
   ▸ القاعدة: التقرير المحايد قد يقتبس تصريحات حادّة من مسؤول أو معارض
     (مثل اتهام حكومةٍ لأخرى بـ'التدخّل'). هذه الكلمات تخصّ المُتحدِّث لا الصحيفة،
     ويبقى المقال محايداً إذا عرض الأطراف بتوازن. لا تَنسب موقف المُقتبَس إلى المقال.

2. ORIGIN ≠ LABEL — the outlet's nationality and the topic's geography do NOT
   set the label. Classify the STANCE, not the source's passport.
   ▸ القاعدة: مصدرٌ غربيّ ينتقد بشدّة حكومته أو زعيماً غربياً يعبّر عن موقف
     نقديّ/معارض، لا عن انحياز غربيّ. وذِكرُ ساسة غربيين أو إسرائيليين أو إيرانيين
     لا يحدّد التصنيف بذاته؛ العبرة بتبنّي الإطار من عدمه.

═══ STEP 1 — INTERNAL BIAS SIGNALS ═══

Identify the following inside the article text:

- LOADED VOCABULARY (editorial voice only): judgment words in the reporter's
  OWN narration — 'العدوان', 'الإرهابيون', 'المقاومة', 'الشرعية', 'النظام',
  'الإصلاحات', 'القمع' — EXCLUDING words that appear inside quotations.
- ATTRIBUTED vs EDITORIAL: which charged language sits inside quotes/attribution
  ('قال', 'وفق', 'بحسب', 'أعلن') versus the outlet's own voice?
- SOURCE SELECTION: whom does the article quote or center (officials, opposition,
  foreign govs, NGOs, anonymous sources, eyewitnesses)? Is the coverage one-sided?
- WHAT IS EMPHASIZED: which facts open the article / sit in the headline versus
  buried late in the body?
- WHAT IS MISSING: are obvious counter-perspectives or alternative explanations
  absent?
- POWER LOCUS: who holds authority in this context, and whom is the article
  aligned for or against? (Disambiguates opposition vs pro_government direction.)

═══ STEP 2 — LABEL DEFINITIONS ═══

Choose EXACTLY ONE label.

- pro_government — يقف صوتُ التحرير في موقع السلطة الرسمية القائمة ويعزّز شرعيتها.
   المؤشرات: يجعل المصادر الرسمية المرجعَ الوحيد/الأبرز؛ يتبنّى مصطلحات النظام؛
    يصوّر المعارضة أو الاحتجاج كتهديد ('فتنة'، 'عناصر'، 'مؤامرة')؛ يبرز الاستقرار والإنجاز.
   ليس هذا التصنيف إذا: كان مجرّد نقلٍ لتصريح رسميّ داخل تغطية متوازنة تتضمّن الطرف الآخر (← neutral).
   الفيصل: هل يتبنّى صوتُ التحرير شرعية السلطة، أم يكتفي بنقل موقفها؟

- opposition — يقف ضدّ سلطةٍ حكومية داخلية محدّدة.
   المؤشرات: يركّز على إخفاقات النظام والفساد والانتهاكات؛ يعتمد أصوات المعارضة والضحايا؛
    مفردات تحرير مثل 'قمع'، 'سلطويّ'، 'استبداد'؛ يطالب بالمساءلة.
   ليس هذا التصنيف إذا: كان الخصمُ قوّةً خارجية (غربية/أجنبية) لا حكومةً داخلية (← pan_arab)؛
    أو كان النقدُ منسوباً لمتحدّثين فقط دون تبنٍّ تحريريّ (← قد يكون neutral).
   الفيصل: مَن هدفُ النقد — حكومةٌ داخلية (opposition) أم قوّة خارجية (pan_arab)؟

- neutral — توازن مُثبَت بدليلٍ إيجابي، لا مجرّد غياب انحياز وكذلك اذا تحدث عن حدث مسلم به فلا يمكن تصنيفه في المقابل.
   يتحقّق فقط بالشروط الثلاثة معاً: (1) تمثيل متناسب لوجهات النظر،
    و(2) معاملة متكافئة للأطراف دون تشريع طرفٍ وتجريم آخر،
    و(3) أيّ شحنة لغوية منسوبة لمتحدّثين لا صادرة عن صوت التحرير.
   ليس هذا التصنيف إذا: كانت النبرة هادئة/تحليلية فحسب؛ أو كان اختيار المصادر أحاديّاً
    أو الطرف المقابل غائباً (حتى بلا مفردات مشحونة)؛ أو شُرِّع طرفٌ وجُرِّم آخر.
   الفيصل: هل ثمّة دليلٌ إيجابيّ على التوازن، أم مجرّد غياب كلماتٍ حادّة؟

- pan_arab — يتبنّى موقعَ المصلحة العربية الجامعة في مواجهة قوى خارجية.
   المؤشرات: 'الأمة العربية'، 'القضية المركزية'؛ يؤطّر الحدث كصراعٍ عربيّ ضدّ تدخّل
    غربيّ/إسرائيليّ؛ يثمّن المقاومة؛ يتوجّس من الدوافع الأجنبية.
   ليس هذا التصنيف إذا: كان النقدُ موجّهاً لحكومةٍ عربية داخلية بعينها دون البُعد العربيّ الجامع (← opposition).
   الفيصل: هل الخصمُ خارجيّ والمصلحةُ المُدافَع عنها عربيةٌ جامعة؟ (قد يظهر حتى لو كان سطح الموضوع أزمةً عربية داخلية).

- western_aligned — يتبنّى الإطارَ والمعايير المؤسساتية الغربية كمرجعيةٍ شرعية.
   المؤشرات: يجعل الحكومات/المراكز/المنظمات الغربية المرجعَ البارز؛ يوظّف أطر حقوق الإنسان
    والديمقراطية كمسلّمات؛ يعامل الموقف الأمريكي/الأوروبي كأمرٍ بديهيّ الشرعية؛
    يركّز أصوات الطرف الغربي/الإسرائيلي ويُهمّش مقابلها.
   ليس هذا التصنيف إذا: كان المصدرُ غربيّاً فقط؛ أو كان ينتقد ساسةً غربيين أو سياسةً غربية
    (← opposition/نقديّ أو neutral بحسب التأطير).
   الفيصل: هل يتبنّى صوتُ التحرير الإطارَ الغربيّ كمرجعية، أم يكتفي بتغطيته/نقده؟

═══ DECISION RULES (tie-breakers) ═══

Apply in order; stop at the first rule that resolves the case:

R1. If charged language appears ONLY inside quotes and sides are presented
    proportionally → neutral. (Never inherit a quoted speaker's stance.)
R2. If LOADED VOCABULARY is 'none' BUT sourcing is one-sided or counter-views
    are omitted → choose the leaning label, NOT neutral.
R3. If the tone is calm/analytical BUT actors are legitimized/delegitimized
    asymmetrically → choose the leaning label, NOT neutral.
R4. If the article criticizes EXTERNAL/Western interference in defense of an
    Arab or regional interest → pan_arab. If it criticizes a specific DOMESTIC
    government → opposition.
R5. A Western-sourced piece that criticizes Western actors is critical/opposition
    or neutral by its framing — never western_aligned by origin alone.

═══ STEP 3 — SCORE, CONFIDENCE, FRAMING ═══

▸ score (continuous bias intensity toward the chosen label, -1.0 to +1.0):
    +1.0  strongly aligned with the chosen label
    +0.5  clear alignment with some balancing elements
     0.0  label fits weakly, near-neutral
    -0.5  article actively pushes AGAINST the label's typical framing
    -1.0  strong counter-framing
    For label='neutral', score MUST be 0.0.

▸ confidence (be especially cautious without sibling articles):
    0.9-1.0  multiple strong indicators converge
    0.7-0.89 clear lean with 1-2 solid indicators
    0.5-0.69 detectable lean; indicators subtle or partial
    0.2-0.49 weak/ambiguous
    < 0.2    DO NOT USE.
    Calibrate confidence DOWN by ~0.1 versus comparative mode, unless evidence is
    overwhelming. NOTE: a well-supported neutral (balanced sourcing, charged words
    only in quotes) deserves HIGH confidence — do not under-rate clear neutrality.

▸ framing (one Arabic sentence):
    Must NAME THE SOURCE explicitly (e.g., 'الشرق الأوسط', 'العربي الجديد') and
    cite SPECIFIC EVIDENCE from the article (a quoted phrase, a sourcing pattern,
    an omission) that supports the chosen label.

  GOOD framing examples:
    ✓ "تستخدم 'الشرق الأوسط' عبارة 'الإصلاحات المنتظرة' وتعتمد تصريحات وزارية رسمية دون إيراد منتقدين، ما يعكس إطاراً مؤيّداً للحكومة."
    ✓ "تركّز 'العربي الجديد' على شهادات معتقلين سابقين وتستخدم مفردات مثل 'القمع الممنهج'، ما يكشف موقعها المعارض."

  BAD framing (do not write like this):
    ✗ "المقالة منحازة لأحد الأطراف"
    ✗ "تظهر ميلاً سياسياً"

═══ OUTPUT FORMAT ═══

Return ONLY a JSON object — no markdown, no commentary, no preamble.
You MUST complete the "analysis" fields BEFORE deciding the final label.

{{
  "analysis": {{
    "loaded_vocabulary": "<Brief note on found Arabic judgment words or 'none'>",
    "sourcing_pattern": "<Who is quoted vs who is omitted>",
    "emphasis_points": "<What was foregrounded in headline/lead>"
  }},
  "label": "<one of: pro_government | opposition | neutral | pan_arab | western_aligned>",
  "score": <float -1.0 to +1.0. For 'neutral', score MUST be 0.0>,
  "confidence": <float 0.0 to 1.0. High confidence is allowed for clearly balanced neutral reporting>,
  "framing": "<one Arabic sentence naming the source and citing specific evidence>"
}}
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _strip_code_fences(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s.strip())
    return s.strip()


def _source_from_url(url: str | None) -> str:
    """Derive a short source label from an article URL (hostname)."""
    if not url:
        return "unknown"
    try:
        netloc = urlparse(url).netloc
        return netloc or "unknown"
    except Exception:
        return "unknown"


class BiasAgent(MCPAgent):
    """
    Agent 3 — Bias Agent (canonical MCP, deterministic).

    ``run(article_ids, event_clusters)`` classifies every article in
    *article_ids*. Multi-article events (size ≥ 2) are classified
    comparatively; singletons fall back to a one-article prompt. Each
    classification passes through the confidence gate before being
    persisted via ``insert_bias_score``.
    """

    # ── Public API ────────────────────────────────────────────────────────

    async def run(
        self,
        article_ids: list[int],
        event_clusters: dict[int, list[int]],
    ) -> dict[str, Any]:
        """
        Classify bias for all *article_ids* using *event_clusters* from
        ClusteringAgent.

        Parameters
        ----------
        article_ids :
            All article IDs to process for this pipeline batch.
        event_clusters :
            ``event_id → [article_id, ...]`` for clusters with ≥ 2 members.
            Clusters of size 1 are treated as singletons here — matching
            the partitioning convention from the legacy implementation.

        Returns
        -------
        dict with keys:
            bias_results : list[dict]  — one entry per kept classification
            stored_count : int         — rows newly inserted into bias_scores
            errors       : list[str]   — non-fatal error strings
        """
        if not article_ids:
            return {"bias_results": [], "stored_count": 0, "errors": []}

        errors: list[str] = []

        # ── Build membership: multi-member clusters vs singletons ─────────────
        article_event: dict[int, int] = {}
        clustered: set[int] = set()
        for eid, aids in event_clusters.items():
            if len(aids) < 2:
                continue
            for aid in aids:
                article_event[int(aid)] = int(eid)
                clustered.add(int(aid))

        singletons: list[int] = [int(a) for a in article_ids if int(a) not in clustered]

        # ── Load MCP payloads (id, title, content, source) ────────────────────
        cluster_payloads: dict[int, list[dict[str, Any]]] = {}
        for eid, aids in event_clusters.items():
            if len(aids) < 2:
                continue
            pl = await self._fetch_bias_payloads(list(aids))
            if pl:
                cluster_payloads[int(eid)] = pl
            else:
                errors.append(
                    f"could not load article rows for event {eid} "
                    f"(article_ids={list(aids)})"
                )

        singleton_payloads: dict[int, dict[str, Any]] = {}
        for aid in singletons:
            pl = await self._fetch_bias_payloads([aid])
            if pl:
                singleton_payloads[aid] = pl[0]
            else:
                errors.append(f"could not load article row for singleton {aid}")

        # ── Build ordered task queue ─────────────────────────────────────────
        task_queue: list[tuple[int, int | None]] = []
        for eid in sorted(event_clusters.keys()):
            aids = event_clusters[eid]
            if len(aids) < 2:
                continue
            for aid in sorted(aids):
                task_queue.append((int(aid), int(eid)))
        for aid in sorted(singletons):
            task_queue.append((aid, None))

        if not task_queue:
            return {"bias_results": [], "stored_count": 0, "errors": errors}

        # ── Deterministic classification loop ─────────────────────────────────
        bias_results: list[dict[str, Any]] = []
        stored_count: int = 0

        for target_id, eid in task_queue:
            if eid is not None:
                payloads = cluster_payloads.get(eid)
                if not payloads:
                    continue
                cls = await self._classify_comparative(payloads, target_id)
            else:
                payload = singleton_payloads.get(target_id)
                if not payload:
                    continue
                cls = await self._classify_single_from_payload(payload)

            if "error" in cls:
                errors.append(
                    f"bias classify error for article {target_id}: {cls['error']}"
                )
                continue

            # Confidence gate — Step 4.5.5 point 5 (API-degradation guard)
            label = str(cls.get("label", _DEGRADATION_NEUTRAL_LABEL))
            confidence = float(cls.get("confidence", 0.0))
            if (
                label == _DEGRADATION_NEUTRAL_LABEL
                and confidence < _DEGRADATION_MIN_CONFIDENCE
            ):
                errors.append(
                    f"dropped low-confidence neutral for article {target_id} "
                    f"(confidence={confidence:.2f} < {_DEGRADATION_MIN_CONFIDENCE})"
                )
                continue

            row = {
                "article_id": int(target_id),
                "score":      float(cls.get("score", 0.0)),
                "label":      label,
                "confidence": confidence,
                "framing":    str(cls.get("framing", "")),
            }
            bias_results.append(row)

            ins = await self.call_tool("insert_bias_score", row)
            if "error" in ins:
                errors.append(
                    f"insert_bias_score error for article {target_id}: "
                    f"{ins['error']}"
                )
                continue
            if ins.get("inserted"):
                stored_count += 1
            # inserted=False == article already has a bias_scores row
            # (ON CONFLICT DO NOTHING, idempotent re-run — not an error).

        logger.info(
            "[BiasAgent] stored=%d  results=%d  errors=%d",
            stored_count, len(bias_results), len(errors),
        )

        return {
            "bias_results": bias_results,
            "stored_count": stored_count,
            "errors":       errors,
        }

    async def classify_single(self, text: str) -> dict[str, Any]:
        """
        Single-article bias classification for Phase 6 evaluation
        (``evaluation/evaluate.py``).

        Constructs a single-article prompt using a synthetic payload (never
        writes to the DB), routes the call through ``self.call_gemini``
        with the full Gemini fallback chain, parses and clamps the response,
        and returns the normalised dict. Never raises (Rule 2.4).

        Returns on success: ``{"score", "label", "confidence", "framing"}``.
        Returns on failure: ``{"error": str}``.
        """
        safe = (text or "")[:_MAX_SINGLE_CHARS]
        payload = {
            "id":      _EVAL_SINGLE_ARTICLE_ID,
            "title":   "مقال التقييم",
            "content": safe,
            "source":  "evaluation",
        }
        return await self._classify_single_from_payload(payload)

    # ── LLM helpers ───────────────────────────────────────────────────────

    async def _classify_comparative(
        self,
        articles: list[dict[str, Any]],
        target_article_id: int,
    ) -> dict[str, Any]:
        """
        Comparative classification across ≥ 2 articles covering one event.

        Returns ``{score, label, confidence, framing}`` on success, or
        ``{"error": str}`` on any transport / parse error. Never raises.
        """
        if not articles or target_article_id not in {a["id"] for a in articles}:
            return {"error": "target_article_id not found in payloads"}

        if len(articles) == 1:
            return await self._classify_single_from_payload(articles[0])

        chars_per_article = min(
            _MAX_COMPARATIVE_CHARS,
            _TOTAL_COMPARATIVE_BUDGET // max(len(articles), 1),
        )

        blocks: list[str] = []
        for i, a in enumerate(articles, 1):
            blocks.append(
                f"--- Article {i} | Source: {a['source']} ---\n"
                f"Title: {(a.get('title') or '')[:_MAX_TITLE_CHARS]}\n"
                f"{(a.get('content') or '')[:chars_per_article]}"
            )
        articles_text = "\n\n".join(blocks)

        target_index = next(
            i for i, a in enumerate(articles, 1)
            if a["id"] == target_article_id
        )
        target_source = next(
            a["source"] for a in articles
            if a["id"] == target_article_id
        )

        prompt = _BIAS_COMPARATIVE_PROMPT_TEMPLATE.format(
            n=len(articles),
            articles_text=articles_text,
            target_index=target_index,
            target_source=target_source,
        )
        return await self._run_bias_prompt(prompt)

    async def _classify_single_from_payload(
        self,
        article: dict[str, Any],
    ) -> dict[str, Any]:
        """Single-article classification — used for singletons and classify_single."""
        prompt = _BIAS_SINGLE_PROMPT_TEMPLATE.format(
            source=article.get("source", "unknown"),
            title=(article.get("title") or "")[:_MAX_TITLE_CHARS],
            content=(article.get("content") or "")[:_MAX_SINGLE_CHARS],
        )
        return await self._run_bias_prompt(prompt)

    async def _run_bias_prompt(self, prompt: str) -> dict[str, Any]:
        """
        Cache-aware Gemini call for a bias prompt.

        Returns the parsed & clamped classification dict, or ``{"error": ...}``.
        Cache TTL = 72 h (Rule 2.7 / blueprint.md §9), key = ``bias:<md5(prompt)>``.
        """
        cache_key = f"bias:{_md5(prompt)}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            try:
                parsed = json.loads(cached["value"])
                if isinstance(parsed, dict) and "label" in parsed:
                    return parsed
            except (TypeError, ValueError):
                pass

        gen = await self.call_gemini(
            prompt=prompt,
            task_type="bias",
            max_tokens=_BIAS_MAX_TOKENS,
            temperature=_BIAS_TEMPERATURE,
        )
        if "error" in gen:
            return {"error": gen["error"]}

        raw = _strip_code_fences(gen.get("text", ""))
        if not raw:
            return {"error": "bias LLM returned empty text"}

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as e:
            return {"error": f"bias JSON parse failed: {e}"}

        if not isinstance(parsed, dict):
            return {"error": "bias response was not a JSON object"}

        clamped = self._clamp_classification(parsed)

        try:
            await self.call_tool("cache_set", {
                "key":   cache_key,
                "value": json.dumps(clamped, ensure_ascii=False),
                "ttl":   _TTL_BIAS,
            })
        except (TypeError, ValueError):
            pass

        return clamped

    @staticmethod
    def _clamp_classification(raw: dict[str, Any]) -> dict[str, Any]:
        """Coerce an LLM response into the canonical bias-row shape."""
        label = str(raw.get("label", "neutral")).strip().lower()
        if label not in _VALID_BIAS_LABELS:
            label = "neutral"

        try:
            score = float(raw.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(-1.0, min(1.0, score))

        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        framing = str(raw.get("framing", "")).strip()

        return {
            "score":      score,
            "label":      label,
            "confidence": confidence,
            "framing":    framing,
        }

    # ── Data helper ───────────────────────────────────────────────────────

    async def _fetch_bias_payloads(
        self,
        article_ids: list[int],
    ) -> list[dict[str, Any]]:
        """
        Return classify_bias-shaped dicts for the given article IDs.

        Uses the ``get_articles`` MCP tool (not psycopg2). Input order is
        preserved. Never raises (Rule 2.4).
        """
        if not article_ids:
            return []

        result = await self.call_tool("get_articles", {"ids": list(article_ids)})
        if "error" in result:
            logger.warning("[BiasAgent._fetch_bias_payloads] %s", result["error"])
            return []

        rows = result.get("articles", []) or []
        by_id: dict[int, dict[str, Any]] = {}
        for r in rows:
            try:
                aid = int(r["id"])
            except (KeyError, TypeError, ValueError):
                continue
            by_id[aid] = {
                "id":      aid,
                "title":   r.get("title", "") or "",
                "content": r.get("content", "") or "",
                "source":  _source_from_url(r.get("url", "")),
            }
        return [by_id[i] for i in article_ids if i in by_id]
