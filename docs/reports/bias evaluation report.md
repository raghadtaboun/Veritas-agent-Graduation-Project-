# Bias Detection — Evaluation Report (Phase 6, Step 6.2)

## 1. Overview

This report documents the evaluation of the **Bias Agent** in the **Veritas Agent**
project. The task is single-article **bias classification** into one of five
mutually-exclusive labels. Every model classifies the same **60 human-labelled
articles** and its predictions are scored against the human gold labels.

The study ran in two stages:

1. **Stage 1 — Prompt-language selection.** Three linguistically different
   formulations of the *same* prompt were compared to choose the best one.
2. **Stage 2 — Prompt refinement + quantization study.** The winning formulation
   was deepened ("Improved"), re-evaluated, and the **QAT** (quantization-aware
   training) variants of the local models were tested against it.

**Models evaluated**

| Label | Backend | Identifier | Quantization |
| --- | --- | --- | --- |
| `gemma-4b` | Ollama (local) | `gemma4:e4b-mlx` | mlx |
| `gemma-12b` | Ollama (local) | `gemma4:12b-mlx` | mlx |
| `gemma-26b` | Ollama (local) | `gemma4:26b` | mlx |
| `gemma-31b` | Ollama (local) | `gemma4:31b-mlx` | mlx |
| `gemma-4b-qat` | Ollama (local) | `gemma4:e4b-it-qat` | QAT |
| `gemma-12b-qat` | Ollama (local) | `gemma4:12b-it-qat` | QAT |
| `gemma-26b-qat` | Ollama (local) | `gemma4:26b-a4b-it-qat` | QAT |
| `gemini-3.5-flash` | Google AI Studio (API) | `gemini-3.5-flash` | — (hosted) |

All local models are served through the identical Ollama HTTP path with identical
call parameters (`temperature 0`, `format:"json"`, `think:false`), so the
size/quantization comparison is like-for-like. `gemini-3.5-flash` is a hosted API
reference point.

**Metrics.** Correct count (/60), **accuracy**, **macro-F1** (unweighted mean of
the five per-label F1 scores), per-label precision/recall/F1, the 5×5 confusion
matrix, and wall-clock time. A malformed/unparseable model output is recorded as
`parse_error` and counts as **incorrect** — there were **zero parse errors** in
every run, so all numbers below reflect genuine classification quality, not
output-format failures.

## 2. Dataset

- **60 articles**, **5 labels**, **exactly 12 articles per label** (`support = 12`
  in every per-label block). The set is **perfectly balanced**.
- The five labels: `pro_government`, `opposition`, `neutral`, `pan_arab`,
  `western_aligned`.
- Because the classes are balanced, **macro-F1 tracks accuracy closely** in this
  study, and macro-F1 is the more honest headline metric (it weights each of the
  five labels equally and is not flattered by a model that simply over-predicts a
  common class).

## 3. Stage 1 — Prompt-language comparison

Three formulations of the same prompt were tested:

| Formulation | Description |
| --- | --- |
| **English** | Entire prompt (instructions + label definitions) in English. |
| **Arabic** | The same prompt fully translated into Arabic. |
| **Mix** | Instructions in **English**, each bias-label definition explained in **Arabic** — to give the model a precise grasp of each category on Arabic-language news. |

### 3.1 Accuracy

| Prompt | 4b | 12b | 26b | 31b | Flash |
| --- | --- | --- | --- | --- | --- |
| Arabic | 0.633 | 0.733 | 0.683 | 0.750 | 0.817 |
| English | 0.600 | 0.750 | 0.683 | 0.800 | 0.800 |
| **Mix** | 0.617 | **0.767** | **0.733** | 0.800 | **0.833** |

### 3.2 Macro-F1

| Prompt | 4b | 12b | 26b | 31b | Flash |
| --- | --- | --- | --- | --- | --- |
| Arabic | 0.632 | 0.718 | 0.664 | 0.738 | 0.813 |
| English | 0.585 | 0.744 | 0.682 | 0.799 | 0.800 |
| **Mix** | 0.608 | **0.759** | **0.730** | 0.797 | **0.834** |

### 3.3 Averages across the five models

| Prompt | Mean accuracy | Mean macro-F1 |
| --- | --- | --- |
| Arabic | 0.723 | 0.713 |
| English | 0.727 | 0.722 |
| **Mix** | **0.750** | **0.746** |

### 3.4 Reading Stage 1

- **Mix wins on average** on both metrics, and is the best-or-tied-best in **4 of
  5 models**. The only model that preferred a different formulation is the smallest
  one (`gemma-4b`), which did marginally better on the fully-Arabic prompt — a
  small, noise-level difference.
- The benefit of Mix is **largest for the mid/large local models** (`12b`, `26b`):
  giving the label *definitions* in Arabic while keeping *instructions* in English
  helps the model map nuanced Arabic-news framing to the right category. `26b`
  jumps +0.05 accuracy (0.683 → 0.733) moving from English/Arabic to Mix.
- The fully-**Arabic** prompt is consistently the **weakest** average — translating
  the operative instructions into Arabic appears to slightly degrade
  instruction-following, even though Arabic label *explanations* (the Mix idea)
  help.
- **Conclusion of Stage 1:** the **Mix** prompt is selected as the base for
  refinement.

## 4. Stage 2 — The Improved prompt

The Mix prompt was deepened with sharper, more detailed label guidance and
re-evaluated. The table below compares Mix → Improved.

### 4.1 Accuracy: Mix → Improved

| Model | Mix | Improved | Δ |
| --- | --- | --- | --- |
| 4b | 0.617 | 0.650 | +0.033 |
| 12b | 0.767 | **0.833** | **+0.067** |
| 26b | 0.733 | 0.733 | 0.000 |
| 31b | 0.800 | 0.817 | +0.017 |
| Flash | 0.833 | 0.833 | 0.000 |

### 4.2 Macro-F1: Mix → Improved

| Model | Mix | Improved | Δ |
| --- | --- | --- | --- |
| 4b | 0.608 | 0.642 | +0.034 |
| 12b | 0.759 | **0.833** | **+0.074** |
| 26b | 0.730 | 0.736 | +0.006 |
| 31b | 0.797 | 0.808 | +0.012 |
| Flash | 0.834 | 0.835 | +0.001 |

### 4.3 Final results on the Improved prompt

| Metric | 4b | 12b | 26b | 31b | Flash |
| --- | --- | --- | --- | --- | --- |
| Correct (/60) | 39 | **50** | 44 | 49 | **50** |
| Accuracy | 0.650 | **0.833** | 0.733 | 0.817 | **0.833** |
| Macro-F1 | 0.642 | **0.833** | 0.736 | 0.808 | **0.835** |

### 4.4 Reading Stage 2

- **`gemma-12b` is the biggest winner of refinement** (+0.067 accuracy, +0.074
  macro-F1). After refinement it matches the hosted `gemini-3.5-flash` almost
  exactly (50/60 correct, accuracy 0.833, macro-F1 0.833 vs 0.835).
- **`gemini-3.5-flash` is the best overall** but by the thinnest of margins; its
  edge over `12b` is **0.002 macro-F1** — inside the noise band of a 60-item set.
- **Prompt refinement has diminishing returns at the top.** The already-strong
  `flash` and `26b` barely moved; the gains concentrated on the model that had the
  most "headroom" to be guided (`12b`).
- **"Bigger is not better" inside the Gemma family.** `gemma-12b` beats both the
  larger `gemma-26b` (+0.10 accuracy) **and** the much larger `gemma-31b`
  (+0.016 accuracy, +0.025 macro-F1) on the Improved prompt. The 26B variant in
  particular is the weakest of the three large models here.

## 5. Per-label and confusion analysis (Improved prompt)

The aggregate numbers hide *where* models fail. Two systematic error patterns
dominate across nearly every model.

### 5.1 `western_aligned` is the hardest label (recall problem)

`western_aligned` has the **lowest recall** for almost every model — the models
recognise it when they commit to it (precision is usually high) but frequently
**miss** it, most often labelling it `neutral` or `opposition` instead.

| Model | `western_aligned` precision | recall | F1 | Main leak |
| --- | --- | --- | --- | --- |
| 4b | 0.750 | 0.500 | 0.600 | 4 → neutral |
| 12b | 0.889 | 0.667 | 0.762 | 2 → opposition, 2 → neutral |
| 26b | 1.000 | 0.500 | 0.667 | 5 → neutral |
| 31b | 1.000 | 0.500 | 0.667 | 3 → opposition, 2 → neutral |
| Flash | 1.000 | 0.750 | 0.857 | 3 → neutral |

Interpretation: western-aligned framing in Arabic news is often *implicit* (tone,
source selection, emphasis) rather than explicit, so models conservatively fall
back to `neutral`. This single label is the largest drag on macro-F1 for the
large models — e.g. `gemma-31b` is otherwise excellent (every other label F1 ≥
0.77) but its 0.667 `western_aligned` F1 pulls its macro-F1 down to 0.808.

### 5.2 `neutral` is an over-prediction sink for the smaller models

The mirror image of 5.1: small/weaker models achieve **very high `neutral`
recall but low `neutral` precision** — they dump uncertain articles into
`neutral`.

| Model | `neutral` precision | recall |
| --- | --- | --- |
| 4b | 0.524 | 0.917 |
| 12b | 0.706 | 1.000 |
| 26b | 0.500 | 0.667 |
| 31b | 0.786 | 0.917 |
| Flash | 0.714 | 0.833 |

`gemma-4b` catches 11/12 true neutrals but its `neutral` precision is only 0.52 —
roughly **half** of what it calls "neutral" is actually a different bias it failed
to detect. This "neutral magnet" behaviour is the small model's dominant failure
mode and is also exactly the behaviour QAT amplifies (Section 6).

### 5.3 What the models do well

- **`pan_arab`** is the strongest label across the board (F1 0.76–0.92), and
  `gemma-12b` reaches 0.917/0.917 precision/recall on it.
- **`opposition`** is reliably detected by the larger models (`gemma-26b`
  precision 1.0; `gemma-12b` F1 0.80).
- **`pro_government`** precision is consistently high (`gemma-12b` 1.0,
  `gemma-31b` 0.85): when a model says "pro-government" it is almost always right.

## 6. Quantization study — QAT vs mlx (Improved prompt)

The QAT variants of the three available local sizes were run on the Improved
prompt and compared with the mlx originals.

### 6.1 Headline comparison

| Metric | 4b | 4b-QAT | 12b | 12b-QAT | 26b | 26b-QAT |
| --- | --- | --- | --- | --- | --- | --- |
| Correct (/60) | 39 | 38 | **50** | 45 | 44 | 43 |
| Accuracy | 0.650 | 0.633 | **0.833** | 0.750 | 0.733 | 0.717 |
| Macro-F1 | 0.642 | 0.591 | **0.833** | 0.718 | 0.736 | 0.707 |

### 6.2 Quality delta (mlx → QAT)

| Model | Δ accuracy | Δ macro-F1 |
| --- | --- | --- |
| 4b | −0.017 | −0.051 |
| 12b | **−0.083** | **−0.115** |
| 26b | −0.017 | −0.029 |

### 6.3 The key finding: QAT collapses `western_aligned` into `neutral`

QAT does not degrade all classes evenly — it **specifically destroys the
`western_aligned` class**, which is exactly why macro-F1 falls much harder than
accuracy:

| Model | `western_aligned` recall | `western_aligned` F1 | `neutral` precision |
| --- | --- | --- | --- |
| 12b (mlx) | 0.667 | 0.762 | 0.706 |
| **12b-QAT** | **0.167** | **0.286** | 0.522 |
| 4b (mlx) | 0.500 | 0.600 | 0.524 |
| **4b-QAT** | **0.083** | **0.133** | 0.522 |
| 26b (mlx) | 0.500 | 0.667 | 0.500 |
| **26b-QAT** | **0.333** | **0.500** | 0.524 |

`gemma-12b-qat` recovers only **2 of 12** `western_aligned` articles (it sends
**9 of them to `neutral`**); `gemma-4b-qat` recovers only **1 of 12**. The QAT
models become extreme "neutral magnets": their `neutral` precision drops to ~0.52
because they funnel the bias they can no longer detect into `neutral`. This is the
mechanism behind the −0.115 macro-F1 drop on the otherwise-best `12b`.

### 6.4 Timing and the real QAT trade-off

| Model | Time (mlx) | Time (QAT) | Δ |
| --- | --- | --- | --- |
| 4b | ~00:10:12 | ~00:10:34 | ~22 s slower |
| 12b | ~00:28:08 | ~00:25:02 | ~3 min faster |
| 26b | ~00:11:40 | ~00:11:02 | ~38 s faster |

The wall-clock savings from QAT are **modest** (and slightly *negative* for 4b).
**On the two axes measured here — quality and time — QAT is not worth it**, and it
is most harmful precisely on the best model (`12b`). The genuine benefit of QAT —
**lower memory footprint / smaller model size** — is *not* visible in these tables
and must be weighed separately if the deployment target is memory-constrained
hardware. On quality alone, the mlx variants are clearly preferred.

## 7. Timing (mlx models, Improved/Mix prompts)

| Model | Approx. time (60 articles) |
| --- | --- |
| 4b | ~8–10 min (fastest) |
| 26b | ~11–12 min |
| 12b | ~21–28 min |
| 31b | ~55–58 min (slowest) |
| Flash | ~10 min (≈10 s/article, API) |

A notable anomaly: **`gemma-26b` is faster than `gemma-12b`** despite being the
larger model. This is unexpected and likely reflects a difference in the served
quantization/runtime configuration of the `gemma4:26b` tag rather than a true
compute ordering; it is worth verifying before drawing capacity-planning
conclusions. `gemma-31b` is by far the slowest (roughly 5–6× the 12B) without a
quality payoff to justify it.

## 8. Key findings

1. **Prompt language matters: Mix > English > Arabic.** Keeping instructions in
   English while explaining each label in Arabic is the best formulation,
   especially for mid/large local models.
2. **Refinement helped the mid-tier model the most.** `gemma-12b` gained
   +0.067 accuracy / +0.074 macro-F1 from the Improved prompt and rose to parity
   with the hosted Flash model.
3. **Best overall = `gemini-3.5-flash`** (macro-F1 0.835), but its lead over
   `gemma-12b` (0.833) is within noise.
4. **Best local/offline = `gemma-12b` (mlx) + Improved prompt** — 50/60,
   accuracy 0.833, macro-F1 0.833 — at the cost of ~28 min per 60 articles.
5. **Bigger is not better:** `gemma-12b` beats both `gemma-26b` and `gemma-31b`,
   and `gemma-31b` is the slowest model with no quality advantage.
6. **Two universal error modes:** `western_aligned` is under-recalled (leaks to
   `neutral`/`opposition`) and `neutral` is over-predicted by weaker models.
7. **QAT is a net loss on quality**, and it specifically collapses
   `western_aligned` into `neutral` (most severely on `12b`: −0.115 macro-F1).
   Only consider QAT when the memory footprint is the binding constraint.

## 9. Recommendations

- **For best quality:** use `gemini-3.5-flash` with the Improved prompt
  (macro-F1 0.835).
- **For the best local / offline option:** use `gemma-12b` (mlx) with the
  Improved prompt (accuracy 0.833, macro-F1 0.833), accepting the longer
  (~28 min) runtime.
- **Avoid** `gemma-31b` (very slow, no advantage over `12b`) and **avoid the QAT
  variants** for this task unless memory is the deciding constraint — particularly
  `gemma-12b-qat`, which loses the most quality.
- **To raise the ceiling further, target `western_aligned` directly:** add
  sharper, example-driven definitions/disambiguation for `western_aligned` vs
  `neutral` in the prompt, since that single confusion is the dominant remaining
  error for the strong models.

## 10. Methodological caveats

- The evaluation set is small (**60 articles, 12 per label**). Differences of
  **≤ 0.02** in accuracy/macro-F1 are within the noise band and should not be
  over-interpreted (e.g. Flash 0.835 vs 12b 0.833).
- The set is **perfectly class-balanced**, which is ideal for macro-F1
  interpretation but is more generous than a real-world stream where `neutral`
  would dominate; the "neutral magnet" behaviour of small/QAT models would be
  more damaging in production than these balanced numbers suggest.
- All runs had **zero parse errors**, so the scores reflect genuine
  classification ability, not output-formatting robustness.
- `gemini-3.5-flash` is hosted (different serving/quantization stack) and is a
  reference point, not a like-for-like member of the local size sweep.

---

*Sources: `bias evaluation/{Arabic,English,Mix,Improved}/bias_comparison.json`
(per-model accuracy, macro-F1, per-label precision/recall/F1, and 5×5 confusion
matrices over the 60-article human-labelled dataset).*
