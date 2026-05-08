_BIAS_COMPARATIVE_PROMPT_TEMPLATE = """\
You are a comparative political bias classifier for Arabic-language news media. \
Your task is to classify ONE target article by comparing it against sibling articles \
covering the same event from different sources.

The following {n} articles all cover the SAME news event:

{articles_text}

═══════════════════════════════════════════════════════════════
STEP 1 — COMPARATIVE READING (analyze before classifying)
═══════════════════════════════════════════════════════════════

Compare all {n} articles against each other and identify:

- EMPHASIS: What facts, actors, or angles does each article foreground in its \
headline and opening paragraphs?

- OMISSION: What facts present in OTHER sources are absent or buried in each article?

- VOCABULARY: What loaded terms does each use? Note specific Arabic phrasings, e.g.:
    'العدوان' vs 'العملية العسكرية'
    'المقاومة' vs 'الإرهابيون'
    'الشرعية' vs 'النظام الحاكم'
    'الإصلاحات' vs 'التغييرات'

- SOURCING: Whom does each article quote? Officials, opposition figures, foreign \
governments, NGOs, eyewitnesses, anonymous sources?

- NARRATIVE ARC: Who is implicitly cast as legitimate vs illegitimate, hero vs villain?

═══════════════════════════════════════════════════════════════
STEP 2 — CLASSIFY ARTICLE {target_index} (Source: {target_source})
═══════════════════════════════════════════════════════════════

Choose EXACTLY ONE of these five labels for the target article:

┌──────────────────┬──────────────────────────────────────────────────┐
│ pro_government   │ Supports or amplifies official Arab government   │
│                  │ authority. Indicators: quotes official spokes-   │
│                  │ people; uses regime-favored terminology; frames  │
│                  │ protests as 'sedition'; emphasizes stability;    │
│                  │ downplays opposition voices.                     │
├──────────────────┼──────────────────────────────────────────────────┤
│ opposition       │ Critiques or counters Arab government authority. │
│                  │ Indicators: quotes dissidents; emphasizes regime │
│                  │ failures, corruption, rights abuses; uses        │
│                  │ 'سلطوي', 'قمع'; foregrounds accountability       │
│                  │ demands.                                         │
├──────────────────┼──────────────────────────────────────────────────┤
│ neutral          │ Balanced without detectable political lean.      │
│                  │ Indicators: cites multiple perspectives propor-  │
│                  │ tionally; descriptive (not evaluative) language; │
│                  │ reports facts without partisan framing.          │
├──────────────────┼──────────────────────────────────────────────────┤
│ pan_arab         │ Reflects pan-Arab nationalist framing.           │
│                  │ Indicators: 'الأمة العربية', 'القضية المركزية';  │
│                  │ frames events as Arab vs foreign powers;         │
│                  │ valorizes resistance; suspicious of Western or   │
│                  │ Israeli motives.                                 │
├──────────────────┼──────────────────────────────────────────────────┤
│ western_aligned  │ Reflects Western institutional narratives.       │
│                  │ Indicators: cites Western governments, think     │
│                  │ tanks, NGOs prominently; uses human rights /     │
│                  │ democracy frames; treats Western policy as       │
│                  │ default-legitimate; aligns with US/EU positions. │
└──────────────────┴──────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════
STEP 3 — SCORE, CONFIDENCE, FRAMING
═══════════════════════════════════════════════════════════════

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
    Must NAME THE SOURCE explicitly (e.g., 'الجزيرة', 'العربية') and cite \
SPECIFIC EVIDENCE from the target article (a quoted phrase, a sourcing pattern, \
an omission) that distinguishes it from the OTHER sources in the cluster.

  GOOD framing examples:
    ✓ "تبرز الجزيرة 'العدوان الإسرائيلي' وتعتمد بشكل مكثّف على المصادر الفلسطينية، بينما تتجاهل الموقف الأمني الذي تركّز عليه العربية."
    ✓ "تستخدم العربية مصطلح 'العملية العسكرية' وتقتبس من المتحدث العسكري الإسرائيلي والمحلّلين الأمنيين، مما يعكس إطاراً أمنياً مختلفاً عن السردية الإنسانية للجزيرة."
    ✓ "تسعى بي بي سي عربي إلى توازن شكلي عبر إيراد الروايتين، لكنها توسّع في الموقف الأمريكي والأوروبي أكثر من السياق العربي."

  BAD framing (do not write like this):
    ✗ "المقالة منحازة"
    ✗ "تختلف عن المصادر الأخرى"
    ✗ "تقدّم وجهة نظر معيّنة"

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════

Return ONLY a JSON object. No markdown fences, no commentary, no preamble.

{{
  "score":      <float -1.0 to +1.0>,
  "label":      "<one of: pro_government | opposition | neutral | pan_arab | western_aligned>",
  "confidence": <float 0.0 to 1.0>,
  "framing":    "<one Arabic sentence naming the source and citing specific evidence>"
}}
"""


_BIAS_SINGLE_PROMPT_TEMPLATE = """\
You are a political bias classifier for Arabic-language news media. \
You are analyzing a SINGLE article without sibling articles for comparison. \
Bias detection therefore relies entirely on internal signals: vocabulary, \
sourcing, emphasis, and omission within the article itself.

--- Article | Source: {source} ---
Title: {title}

{content}

═══════════════════════════════════════════════════════════════
STEP 1 — INTERNAL BIAS SIGNALS (analyze before classifying)
═══════════════════════════════════════════════════════════════

Identify the following inside the article text:

- LOADED VOCABULARY: Are 'العدوان', 'الإرهابيون', 'المقاومة', 'الشرعية', 'النظام', \
'الإصلاحات', 'القمع' used in ways that imply judgment? Note the specific phrases.

- SOURCE SELECTION: Whom does the article quote? Officials? Opposition figures? \
Foreign governments? NGOs? Anonymous sources? Eyewitnesses?

- WHAT IS EMPHASIZED: Which facts open the article? Which appear in the headline \
versus buried late in the body?

- WHAT IS MISSING: Are obvious counter-perspectives absent? Are alternative \
explanations or contexts ignored?

- IMPLICIT NARRATIVE: Who is positioned as legitimate vs illegitimate, hero vs villain?

═══════════════════════════════════════════════════════════════
STEP 2 — CLASSIFY THE ARTICLE
═══════════════════════════════════════════════════════════════

Choose EXACTLY ONE of these five labels:

┌──────────────────┬──────────────────────────────────────────────────┐
│ pro_government   │ Supports or amplifies official Arab government   │
│                  │ authority. Indicators: quotes official spokes-   │
│                  │ people; uses regime-favored terminology; frames  │
│                  │ protests as 'sedition'; emphasizes stability;    │
│                  │ downplays opposition voices.                     │
├──────────────────┼──────────────────────────────────────────────────┤
│ opposition       │ Critiques or counters Arab government authority. │
│                  │ Indicators: quotes dissidents; emphasizes regime │
│                  │ failures, corruption, rights abuses; uses        │
│                  │ 'سلطوي', 'قمع'; foregrounds accountability       │
│                  │ demands.                                         │
├──────────────────┼──────────────────────────────────────────────────┤
│ neutral          │ Balanced without detectable political lean.      │
│                  │ Indicators: cites multiple perspectives propor-  │
│                  │ tionally; descriptive (not evaluative) language; │
│                  │ reports facts without partisan framing.          │
├──────────────────┼──────────────────────────────────────────────────┤
│ pan_arab         │ Reflects pan-Arab nationalist framing.           │
│                  │ Indicators: 'الأمة العربية', 'القضية المركزية';  │
│                  │ frames events as Arab vs foreign powers;         │
│                  │ valorizes resistance; suspicious of Western or   │
│                  │ Israeli motives.                                 │
├──────────────────┼──────────────────────────────────────────────────┤
│ western_aligned  │ Reflects Western institutional narratives.       │
│                  │ Indicators: cites Western governments, think     │
│                  │ tanks, NGOs prominently; uses human rights /     │
│                  │ democracy frames; treats Western policy as       │
│                  │ default-legitimate; aligns with US/EU positions. │
└──────────────────┴──────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════
STEP 3 — SCORE, CONFIDENCE, FRAMING
═══════════════════════════════════════════════════════════════

▸ score (continuous bias intensity, -1.0 to +1.0):
    +1.0  = strongly aligned with chosen label
    +0.5  = clear alignment with some balancing elements
     0.0  = label fits weakly, near-neutral
    -0.5  = article actively pushes AGAINST the label's typical framing
    -1.0  = strong counter-framing
    
    For label='neutral', score must be in [-0.2, +0.2].

▸ confidence (be especially cautious without sibling articles):
    0.9-1.0  Multiple strong indicators converge
    0.7-0.89 Clear lean with 1-2 solid indicators
    0.5-0.69 Detectable lean, indicators are subtle or partial
    0.2-0.49 Weak/ambiguous — prefer 'neutral' instead
    < 0.2    DO NOT USE. If genuinely uncertain, choose 'neutral' with confidence 0.5-0.7.

  IMPORTANT: Single-article classification is harder than comparative. \
Calibrate confidence DOWN by ~0.1 versus what you would assign in comparative \
mode, unless the evidence is overwhelming.

▸ framing (one Arabic sentence):
    Must NAME THE SOURCE explicitly (e.g., 'الشرق الأوسط', 'العربي الجديد') and \
cite SPECIFIC EVIDENCE from the article (a quoted phrase, a sourcing pattern, an \
omission) that supports your chosen label.

  GOOD framing examples:
    ✓ "تستخدم 'الشرق الأوسط' مصطلح 'الإصلاحات الجريئة' وتعتمد على تصريحات وزارية رسمية دون إيراد منتقدين، مما يعكس إطاراً مؤيّداً للحكومة."
    ✓ "تركّز 'العربي الجديد' على شهادات معتقلين سابقين وتستخدم مفردات مثل 'القمع الممنهج'، ما يكشف موقعها المعارض."
    ✓ "يقدّم 'رويترز عربي' الأرقام والتصريحات من جميع الأطراف بمساحة متوازنة دون مفردات تقييمية، ما يعكس التزاماً بالحياد المهني."

  BAD framing (do not write like this):
    ✗ "المقالة منحازة لأحد الأطراف"
    ✗ "تقدّم رواية محدّدة"
    ✗ "تظهر ميلاً سياسياً"

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════

Return ONLY a JSON object. No markdown fences, no commentary, no preamble.

{{
  "score":      <float -1.0 to +1.0>,
  "label":      "<one of: pro_government | opposition | neutral | pan_arab | western_aligned>",
  "confidence": <float 0.0 to 1.0>,
  "framing":    "<one Arabic sentence naming the source and citing specific evidence>"
}}
"""