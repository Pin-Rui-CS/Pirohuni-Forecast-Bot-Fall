# Artifact Check (research audit)

**Name in code:** `_ARTIFACT_CHECK_PROMPT` (research/pipeline.py)  
**Source:** [research/pipeline.py:857](../../research/pipeline.py#L857)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `call_llm(..., _label="artifact-check")`, temperature 0.1

## Intended use

**Pipeline stage:** Research step 6 — after the scrape cycles, before the compiler.

Grades the gathered research against the evidence plan's required artifact and returns JSON: `status` (complete / partial / missing), what was found, what is missing, a `closest_available` adjacent metric, up to `_MAX_RETRY_QUERIES = 4` retry queries, and a `forecast_swing` rating. The status string is passed through to the forecaster prompts, where 'partial' or 'missing' triggers their WIDEN rule — so a wrong verdict here moves the final number.

## Inserts

- `{today}`
- `{title}`
- `{evidence_plan_excerpt}`
- `{research_excerpt}`
- `{prior_check_section}`
- `{max_retry_queries}`

`{research_excerpt}` is fitted to `_MAX_ARTIFACT_CHECK_INPUT_CHARS = 60,000` by `fit_artifact_check_sections` with a `_MIN_ARTIFACT_CHECK_SECTION_CHARS = 4,000` floor per section. `{evidence_plan_excerpt}` is sent whole — an earlier 4,000-char head-cut was removed because it dropped the plan's own retry-query guidance. `{prior_check_section}` is empty on the first pass; see [artifact-check-prior-section](../02-conditional/artifact-check-prior-section.md).

## Length

- **Scaffold (this file's template text):** 4,607 chars, 6 slot(s)
- **Filled prompt as sent:** ~10K – 68K chars

## Template

```text
You audit research gathered for a forecasting question.

Today's date is {today}.

Forecasting question:
{title}

The evidence plan named a required evidence artifact:
{evidence_plan_excerpt}

Research gathered so far (per provider, truncated):
{research_excerpt}
{prior_check_section}
Decide whether the required artifact was actually found in the research.

Return only valid JSON:
{{
  "status": "complete" | "partial" | "missing",
  "what_was_found": "one or two sentences quoting the key values found, or stating none were",
  "what_is_missing": "one or two sentences naming the exact rows/values still missing, or empty string",
  "closest_available": "if the EXACT resolution metric is absent but a same-family adjacent metric WAS actually retrieved (a related series, the same series on a different basis, or a different-but-comparable measure), quote that value WITH its date/period and source, and state its factual relationship to the target (e.g. 'June 2025 I-94 visitor arrivals = 5,278,944; target is I-92 Foreign Originating, which historically runs a stable fraction of this'); empty string if no adjacent value was retrieved. Quote ONLY what was retrieved: do not compute ratios, percentages, base rates, or averages from it, do not characterize it as high/low, and do not state a forecast implication — rate-construction and interpretation are the forecaster's job",
  "forecast_swing": "low" | "moderate" | "decisive",
  "retry_queries": ["up to {max_retry_queries} focused Google queries that target ONLY the missing artifact, e.g. secondary sources quoting it; empty list if status is complete or no query could plausibly find it"]
}}

Rules:
- Every field records what the research CONTAINS, not an analysis of it. In ANY field, do not perform arithmetic, construct a base rate or reference-class frequency, estimate a probability, or editorialize about what a value implies. Quote values with their dates and sources; the forecaster computes rates and draws conclusions.
- TEMPORAL IMPOSSIBILITY: today is {today}. A report or observation whose claimed event or
  publication date is AFTER today cannot exist — its date is wrong (almost always a prior-year
  event mislabeled with the current year, e.g. a year-less "Aug 7" snippet from an old post).
  Such a value must NOT be presented as a candidate for the resolution window: classify it as
  misdated historical data, say so explicitly, and exclude it from "closest_available".
- NEVER assign a year that no source states. A date without a year ("Aug 7", "posted 16h ago")
  must not be assumed to fall in the current year or in the question's resolution window; record
  it as "(year not stated in source)".
- CORRECTIONS OUTRANK EARLIER INFERENCES: if any scraped extract explicitly corrects, redates,
  or retracts a claim made elsewhere in the research (e.g. "Important note: this event is dated
  8 August 2025, not 2026"), the correction wins. Report the corrected fact and do not restate
  the superseded claim anywhere in your output.
- "complete" only if the artifact's actual values/rows appear in the research text.
- Mentions that the artifact exists, without its values, count as "partial" at best.
- If the resolution value is a count or aggregate over an enumerable set (rows,
  member states, entries), "complete" additionally requires the row-level
  breakdown behind the headline number (which members are in which category).
  A headline count whose composition was not retrieved (e.g. a truncated table)
  is "partial", "what_is_missing" must name the missing rows, and at least one
  retry query must target that row-level breakdown.
- A retrieved value that is the right family but the WRONG exact metric does NOT make status "complete", but it MUST be recorded in "closest_available" — never silently discard a value that was actually fetched just because it is not the exact metric. It is decision-relevant and must survive into the brief.
- forecast_swing estimates how far a reasonable forecast would plausibly move if the
  missing information were resolved one way versus the other: "low" (<5 percentage
  points), "moderate" (5-15), "decisive" (>15). Use "low" when status is "complete".
- Retry queries must be materially different from generic restatements of the question.
- If the missing value simply does not exist yet (a future data release, an outcome
  that has not happened, an unpublished statistic), return an empty retry_queries
  list — searching cannot find numbers that have not been published. Suggest retry
  queries only when the artifact plausibly already exists somewhere online.
```
