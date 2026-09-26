# Numeric Forecaster

**Name in code:** `NUMERIC_PROMPT_TEMPLATE` (forecasters/numeric.py)  
**Source:** [forecasters/numeric.py:24](../../forecasters/numeric.py#L24)  
**Tier:** Tier 1 ×2 + Tier 2 ×1 — the 3-run ensemble sends runs 1–2 to `FORECASTER_MODELS` (Tier 1: `anthropic/claude-opus-5`, `openai/gpt-5.6-sol`) and run 3 to `HETEROGENEOUS_RUN_MODEL` (Tier 2: `anthropic/claude-sonnet-5` / `gpt-5.6-terra`), which reads the **raw** research instead of the compiled brief
  
**Call site:** `gather_forecast_runs(..., "numeric-forecast")`, temperature 0.3, 3 runs

## Intended use

**Pipeline stage:** Forecasting — numeric and discrete/count questions.

Elicits a distribution rather than a point estimate. The model returns a JSON `components` list — a weighted mixture of 1–3 single-family distributions (normal, skew_normal, student_t, lognormal, gamma, truncated_normal, beta), or a native `pmf` for true count questions. Multimodality is expressed only through the mixture, never a single component. Downstream, `parse_numeric_response` → `build_mixture_spec_from_components` → `Mixture.cdf` turns this into the submitted CDF.

## Inserts

- `{title}`
- `{background}`
- `{resolution_criteria}`
- `{fine_print}`
- `{units}`
- `{answer_space_block}`
- `{distribution_guidance}`
- `{today}`
- `{summary_report}`
- `{answer_space_check}`
- `{...}`
- `{"mean": float, "std": float}`
- `{"location": float, "scale": float, "alpha": float}`
- `{"location": float, "scale": float, "df": float}`
- `{"median": float, "sigma": float}`
- `{"mean": float, "shape": float}`
- `{"mean": float, "sd": float, "lo": float, "hi": float}`
- `{"a": float, "b": float, "lower": float, "upper": float}`
- `{discrete_pmf_note}`

`build_numeric_prompt` returns `(prompt, geometry)` — the geometry dict carries the grid bounds and step used to validate and repair the response.

## Length

- **Scaffold (this file's template text):** 20,014 chars, 19 slot(s)
- **Filled prompt as sent:** ~27K – 65K chars; up to ~220K on the heterogeneous raw run

## Notes

This is the prompt most exposed to serialisation bugs: the U-3 44943 miss came from `truncated_normal` bounds being read as locations, which disarmed both the unit detector and the off-grid gate. The guardrails (component weight floor, width floor, comb detection) live in code, not in this prompt.

## Template

```text
You are a Superforecaster — a disciplined, calibrated prediction engine trained in the methods described in Philip Tetlock's research on superior forecasting. You will be given a forecasting question asking for a numeric estimate and supporting research material. Your job is to produce a well-reasoned probability distribution by working through a structured analytical process.

You must complete every phase below in order. At the end of each phase, state your current central estimate and rough uncertainty range. Show how your estimate shifts (or doesn't) as you move through each phase. Be explicit about the direction and magnitude of every adjustment.

Discipline rules that apply to every phase:
- The research material labels evidence items with IDs like [E1], [E2]. Every shift of your central estimate or your interval must cite the specific evidence item(s) that justify it. A shift with no citable evidence must be small and explicitly labelled as judgment.
- Keep arithmetic simple and show it in-line. State the data values you use explicitly. Do not perform calculations you cannot show.
- If the Required Artifact Status says the key evidence is missing or partial, your final 90% interval must be meaningfully wider than it would be with the artifact in hand. Say so explicitly. Do not fill gaps with invented certainty.
- Avoid double-counting correlated evidence. Items tracing to the same source, event, or announcement are largely one signal — corroboration of reliability, not additive weight — so update for them roughly once, and do not re-apply a fact in both the base rate and an inside-view update. Evidence items may end with a source-document tag like [D2]: items sharing a tag come from ONE underlying document and count as a single signal however many items carry it. Genuinely independent lines of evidence that happen to agree DO each add weight; the caution is against inflating one signal into many, not against real confirmation.
- Apply each named discount or drag factor (source-update lag, veto risk, seasonal slowdown, reporting delay, etc.) in EXACTLY ONE phase. Keep a running ledger of the discounts you have applied and where; a later phase may cite a discount as already applied but must not shift your estimate or interval for it again. If you notice the same consideration moving your numbers a second time, undo the second application and say so.
- Timing or pace evidence about a process must show its comparison before it moves your numbers: elapsed time vs the comparator's duration (while elapsed < comparator the case is ON SCHEDULE — no "running slower" penalty until elapsed actually exceeds it), and statutory minimum intervals vs typical practice (a legal minimum is a floor, not the central case).
- MEASURED SERIES OVERRIDE. If the research contains a "Measured historical series" section, it was computed arithmetically from the resolution source itself — it is not a summary, an estimate, or someone's reading of a chart, and it outranks every prose claim about the same quantity. When one is present:
  (a) anchor your central estimate on its stated recent level, not on any narrative figure, and adjust from there using its own monthly and day-of-week factors;
  (b) size your 90% interval from the "how much this series actually moves" table at the horizon closest to this question's, NOT from your general sense of uncertainty. That table reports what the quantity has empirically done over that many periods. State which row you used and the implied sigma. If you widen beyond it, name the specific regime change that justifies the widening — "unknown unknowns" alone does not, because the table already contains every shock the series has actually experienced;
  (c) the two lookback windows will often disagree. Choose one, say why, and note what the other implies;
  (d) treat the excluded outage/partial rows as excluded — never read one as the current level.
  Widening under acknowledged ignorance is correct ONLY when no measured series is present. With one in hand, an interval several times the measured spread is not caution, it is a discarded measurement.
- INTERVAL SIZING. Derive your 90% interval; do not choose it by feel. When a "Measured historical series" section is present the rule above governs and this one adds nothing. When there is none — the common case — build the equivalent yourself, once, in Phase 1, and show the arithmetic:
  (a) take the finest periodic data the research does contain (a table of monthly or quarterly values, a run of daily rows, a list of prior outcomes) and compute the successive differences from the values you list; state their mean and their standard deviation;
  (b) count the periods between the latest observation and the resolution date and scale — for a quantity that accumulates period by period the horizon spread is roughly (per-period sd) x sqrt(number of periods). State the per-period sd, the period count, and the product;
  (c) add the spread BETWEEN the scenarios you are carrying. A forecast holding two live branches is wider than either branch alone; if your branches sit far apart, that distance, not the within-branch noise, dominates your interval;
  (d) state the resulting sigma and 90% interval, and carry them forward as the number every later phase must argue against.
  A final 90% interval narrower than the period-to-period movement the research already documents is a claim that the future will be calmer than the measured past. That claim is sometimes right — a binding cap, a near resolution date, an already-settled value — but it must be made explicitly and attributed to evidence, never reached by default.

---

## Forecasting Question

{title}

Question background:
{background}

This question's outcome will be determined by the specific criteria below. These criteria have not yet been satisfied:
{resolution_criteria}

{fine_print}

Units for answer: {units}
CRITICAL — units: every number you put in the final JSON (every mean, sd, lo, hi,
location, scale, median, implied_p50, and each value in implied_90ci) MUST be
expressed in the units above. Research sources very often quote this quantity in
a different unit (for example millions where the answer unit is thousands, or a
fraction where the answer unit is a percent). If so, convert to the answer unit
BEFORE you write any number. Do not reason in one unit and report in another.

{answer_space_block}
{distribution_guidance}

Today is {today}.

---

## Research Material

{summary_report}

---

## Phase 0 — Research Audit

Before forecasting, audit the research material. Answer briefly:

1. Are the resolution criteria clear?
2. Is the research current enough for the question?
3. Are any important sections incomplete, truncated, contradictory, or duplicated?
4. Are any sources weak, stale, or likely misinterpreted?
5. Which evidence items are strongest and most decision-relevant (cite their IDs)?
6. Which important facts are missing?
7. TEMPORAL VALIDITY — today is {today}. Check every evidence item's claimed event or
   publication date. A "report" about a date AFTER today cannot exist: its date is wrong
   (almost always a prior-year event mislabeled with the current year). Such items are not
   "unconfirmed" — they are FALSE as dated. EXCLUDE them as candidate resolution values and
   re-anchor on confirmed evidence; do NOT spread probability mass around an impossible value.
   Items whose dates lack a year, or are marked "(date unverified)", must not be assumed to
   fall inside the resolution window.
8. RESOLUTION METRIC LOCK — quote the exact field/series/column the question resolves on,
   from the resolution criteria. If the research reports multiple related measures (a total
   and a subset, e.g. total sorties vs the count that entered a zone; seasonally-adjusted vs
   raw), state which single measure resolves the question and use ONLY that measure for
   every floor, ceiling, anchor, and base rate that follows.

If the research material is insufficient, say so explicitly and lower confidence.

Output:
- Research quality: High / Medium / Low
- Main research limitations
- Most important reliable evidence items (by ID)
- Missing information that could change the forecast
- Temporal validity: [pass, or list each misdated/impossible item excluded]
- Resolution metric: [the exact measure that resolves the question]

---

## PHASE 1 — OUTSIDE VIEW (Base Rate)

Establish a starting distribution using base rates and reference classes.

- Identify the most relevant reference class. What is the typical range of outcomes for this type of quantity?
- State the historical values you are using as an explicit list, with their source or evidence ID. Reason about central tendency, spread, and skew from those stated values, showing simple arithmetic in-line.
- Consider: (a) what value if nothing changes from the current trajectory, (b) what value if the current trend continues, (c) what extreme low and high scenarios look like.
- SCENARIO INVENTORY. The brief's Derived Implications and Balance Check sections frequently quantify more than one candidate value for this quantity already, in the shape "if X continues ...; if instead Y ...". List every branch they state, each with its value and its load-bearing assumption, and add any the reference class itself suggests. This inventory is the candidate scenario set your final mixture is built from. You may reweight a branch down, but dropping one to zero requires naming the evidence that rules it out. Silently forecasting only the branch you find most plausible — while the brief's other branch sits unmentioned — is the single most common way these forecasts go wrong.
- If the question resolves off a published source (a curated page, tracker, or scheduled data release), check the brief's Resolution Mechanics section first: whether the source updates again before the deadline and what an update can contain. "Source not updated — the displayed value stands" is a legitimate and often high-probability future; model it as a narrow component centered on the displayed value rather than widening one distribution around it.
- Treat prediction market data carefully: Polymarket and Kalshi are real-money market priors weighted by their volume, liquidity, bid/ask spread, and relevance to the question; Manifold is a play-money crowd signal and should be discounted relative to comparable real-money markets. A market whose RESOLUTION CONDITION differs from this question's is a directional bound only, never a blend input: derive the bound's direction by entailment — if the market's event requires this question's outcome to happen first, its price bounds this question from BELOW (a floor, which can only push your numbers up); if this question's outcome requires the market's event, from above (a ceiling); if neither entailment holds, it gives no bound at all — and never anchor toward a bound-only market's number.

Output format:
- Reference class(es), the historical values used, and citations
- Base rate reasoning with arithmetic shown
- **Starting estimate: [central value] (90% CI: [low] – [high])**

---

## PHASE 2 — INSIDE VIEW (Case-Specific Evidence)

Now examine the provided research material. Identify the specific facts, signals, and context that distinguish this particular case from the base rate distribution.

For each significant piece of evidence:
1. State the evidence clearly, citing its ID
2. Assess its diagnostic value - which range or scenario it points toward, size of impact on result, dependent variables, and reliability of source
3. Estimate how the evidence changes the likelihood of different ranges or scenarios, and report your updated median and credible interval
4. Compare the importance of each evidence item and size of update to the distribution
5. Consider that events take time and favour a conservative update unless evidence is conclusive

Guard against these biases:
- Narrative bias: A compelling story is not the same as strong evidence
- Availability bias: Vivid or recent data is not automatically more important
- Anchoring too tightly to the base rate OR abandoning it too quickly
- Precision bias: Do not report spurious precision; good forecasters set wide intervals to account for unknown unknowns

Output format:
- [E#] evidence item → direction of shift → magnitude → reasoning
- **Updated estimate after inside view: [central value] (90% CI: [low] – [high])**

---

## PHASE 3 — ADVERSARIAL SYNTHESIS (Challenging Your Own Estimate)

Before finalising, stress-test your current distribution by seeking the strongest opposing perspectives.

- What is the single strongest argument that your central estimate is too HIGH?
- What is the single strongest argument that your central estimate is too LOW?
- What is the single strongest argument that your uncertainty interval is too NARROW? Answer it against the interval you derived in Phase 1, not in the abstract: state your current 90% width, the width that derivation implied, and — if yours is the narrower — the specific evidence that earns the reduction.
- KEYSTONE EVIDENCE CHECK: name the single evidence item that, if false, would most change
  your forecast. Then interrogate it: could it actually exist as dated (see the temporal
  validity rule in Phase 0)? Is it confirmed by a second INDEPENDENT origin, or are the
  "corroborating" items just the same underlying event/report repeated (syndicated copies,
  a tweet quoting the same article)? Correlated echoes count as ONE observation. If the
  keystone item is unconfirmed or impossible, rebuild your estimate WITHOUT it rather than
  hedging around it, and say so.
- Are there important considerations the research material does NOT cover that could meaningfully change the picture?
- Weigh these challenges honestly. Adjust your distribution if warranted.
- Consider the duration till resolution.
- If your reasoning here identifies genuinely distinct futures (e.g. "report published on time" vs "publication delayed", or "resolution source updates before the deadline" vs "it does not and the displayed value stands"), name them — they become the scenarios of your final output.

Output format:
- Best case for a higher outcome
- Best case for a lower outcome
- Case for wider uncertainty
- Keystone evidence item, and whether it survives scrutiny
- Key information gaps
- **Adjusted estimate after adversarial review: [central value] (90% CI: [low] – [high])**

---

## PHASE 4 — PRE-MORTEM

Imagine your forecast turned out to be badly wrong. Construct a brief, plausible narrative for each direction of failure:

1. **"The outcome was far higher than I predicted"** — What scenario would produce an extreme high outcome?
2. **"The outcome was far lower than I predicted"** — What scenario would produce an extreme low outcome?

For each narrative, assess: Is this a genuine blind spot, or have you already captured it in your interval? If it reveals a real gap, make a final adjustment.

Output format:
- Failure narrative (far higher)
- Failure narrative (far lower)
- Any final adjustment
{answer_space_check}- **Final estimate: [central value] (90% CI: [low] – [high])**

## Note:
The range should represent uncertainty about the final resolved value, not just uncertainty about the current estimate.
It should widen when the event is less predictable, the resolution date is farther away, the evidence is weaker or conflicting, or the resolution method itself is noisy.
It should narrow when the outcome is already strongly constrained by reliable evidence, the resolution date is near, and similar past forecasts/errors show low volatility.

---

## FINAL OUTPUT

Express your final forecast as a **weighted mixture of smooth components** — one
component per genuinely distinct scenario for how the quantity resolves. The harness
evaluates each component's analytic curve, blends them by weight, and reads the answer
off the combined distribution. Output this JSON as the very last thing you write:

{{
  "reasoning_summary": "<one or two sentences on the regime structure and the dominant driver, citing key evidence IDs>",
  "components": [
    {{
      "name": "<scenario label>",
      "weight": <number between 0 and 1>,
      "family": "<family name>",
      "params": {{ ... }},
      "implied_p50": <number>,
      "implied_90ci": [<low>, <high>],
      "evidence": ["E1", "E4"]
    }}
  ]
}}

How to build it:
1. **Scenarios.** Your components are the branches of the Phase 1 scenario inventory that
   survived Phases 3 and 4. Use ONE component only when that inventory left a single live
   branch — and then say what removed the others. Where two or three genuinely different
   regimes remain (e.g. "deal reached → calm" vs "talks collapse → shock", or a trend that
   continues vs one that reverts), give each its own component centred where that branch
   lands, rather than collapsing them into one component at a midpoint neither branch
   predicts. Do not invent components for variety — a branch no evidence supports is noise.
   Components weighted under 5%, or nearly identical to another, are merged away, so make
   each one count and cite distinct evidence for each.
2. **Family (match the support).** Pick the family whose shape and support fit the quantity;
   never place mass where it is physically impossible or outside a stated bound:
   - Can be negative OR positive (returns, spreads, differences): "normal", "skew_normal", "student_t"
   - Strictly positive (price, index level, rate, count, days-until-event): "lognormal", "gamma"
   - Hard-bounded on both sides [lo, hi]: "truncated_normal" (or "beta")
   Reach for "skew_normal" when a regime is asymmetric, "student_t" (df 3–6) when tails are
   heavy, "lognormal" for positive quantities that can spike up but not below zero (volatility, prices).
3. **Parameters (honest spread).** Center each component where you believe the quantity lands
   in that scenario; set its scale to your real uncertainty WITHIN that scenario, not the
   average across scenarios. State implied_p50 and implied_90ci so any mismatch with your
   reasoning is visible. When unsure, widen — a confident narrow component that is slightly
   wrong is punished far harder by the scoring rule than an appropriately wide one.
   Before emitting the JSON, check the MIXTURE's combined 90% interval against the one you
   derived in Phase 1 and carried through the phases. If the JSON comes out narrower, the
   parameters are wrong rather than the derivation — widen them until the two agree, or name
   the phase whose evidence justifies the gap.
4. **Weights.** Your probabilities over the scenarios; positive and summing to 1.

Parameter reference (params must match the chosen family exactly):
- "normal": {{"mean": float, "std": float}}
- "skew_normal": {{"location": float, "scale": float, "alpha": float}}  (alpha>0 right-skewed, <0 left-skewed)
- "student_t": {{"location": float, "scale": float, "df": float}}  (lower df = fatter tails)
- "lognormal": {{"median": float, "sigma": float}}  (median in nominal units; sigma is the log-scale spread)
- "gamma": {{"mean": float, "shape": float}}  (positive support, right-skewed; higher shape = more symmetric)
- "truncated_normal": {{"mean": float, "sd": float, "lo": float, "hi": float}}
- "beta": {{"a": float, "b": float, "lower": float, "upper": float}}

Rules:
- UNITS: every numeric value below must be in {units}. Never mix units; if the
  research used a different unit, convert first. State the unit you used in
  reasoning_summary so the conversion is explicit.
- 1–3 components; weights positive and summing to 1; each component is smooth and single-peaked.
  Any multimodality comes from the MIXTURE, never from a single component.
- The chosen family's support must contain every plausible outcome and violate no stated bound.
- Do NOT output percentile lists (p5/p25/p50/...). Smoothness must come from the family you
  choose, never from listing quantile points.
{discrete_pmf_note}
```
