# Chain Round — Gap Analysis Section

**Name in code:** `GAP_ANALYSIS_SECTION` (feedback_loop/gap_analysis.py)  
**Source:** [feedback_loop/gap_analysis.py:33](../../feedback_loop/gap_analysis.py#L33)  
**Tier:** n/a — parallel architecture, not wired in

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Appended by `_build_chain_prompt` to whichever live forecaster prompt matches the question type (binary / MC / numeric).

> **Not in the pipeline.** The entire `feedback_loop/` package is referenced by nothing outside itself — `forecasting_bot.py` → `orchestrator.forecast_questions` never touches it. It was built to coexist with the current architecture, and its live dry-run is still pending.

## Intended use

**Pipeline stage:** Would be: forecast round 1 of the feedback loop.

Asks the forecaster, after its final answer, to name the missing information that would most change the forecast — restricted to things that plausibly ALREADY EXIST in published form, each with an `expected_swing` rating. Those gaps drive a second research round.

## Inserts

- `{GAP_MAX_QUERIES}`
- `{GAP_MARKER_OPEN}`
- `{"fact": "...", "why_it_moves_forecast": "...", "expected_swing": "low|moderate|decisive...}`
- `{GAP_MARKER_CLOSE}`

Appended after the base prompt; adds a Phase 5 to the phase sequence.

## Length

- **Scaffold (this file's template text):** 1,604 chars, 4 slot(s)
- **Filled prompt as sent:** ~1.6K chars (plus the full base prompt)

## Template

```text


---

## PHASE 5 — RESEARCH GAP ANALYSIS (write this AFTER your final answer)

After you have written your final answer in the format required above, add ONE
more block. Identify the missing information that would most change your
forecast IF it could be found — but only information that plausibly ALREADY
EXISTS in published, publicly accessible form (news reports, official releases,
datasets, filings, schedules).

Rules:
- Do NOT request numbers that have not been published yet (future data
  releases, outcomes that have not happened, unpublished statistics). Searching
  cannot find what does not exist; if the key missing fact is unpublished, do
  not list it.
- Each gap must state WHY finding it would move your estimate and how far:
  "expected_swing" is "low" (<5 percentage points), "moderate" (5-15), or
  "decisive" (>15). For numeric questions, interpret the swing as how much of
  your distribution's mass would shift.
- Queries must be focused Google queries targeting the specific missing fact
  (e.g. secondary sources quoting it) — not generic restatements of the
  question. At most {GAP_MAX_QUERIES} queries in total across all gaps.
- If no accessible information would meaningfully change your forecast, output
  an empty "gaps" list — that is a perfectly good answer, not a failure.

Output EXACTLY this structure between the markers (this block, not your final
answer line, is now the last thing you write):

{GAP_MARKER_OPEN}
{"gaps": [{"fact": "...", "why_it_moves_forecast": "...", "expected_swing": "low|moderate|decisive", "queries": ["...", "..."]}]}
{GAP_MARKER_CLOSE}
```
