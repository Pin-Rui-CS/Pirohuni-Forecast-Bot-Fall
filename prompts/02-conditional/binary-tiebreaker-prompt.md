# Binary Tiebreaker / Synthesis

**Name in code:** `tiebreaker_prompt` in `get_binary_gpt_prediction`  
**Source:** [forecasters/binary.py:334](../../forecasters/binary.py#L334)  
**Tier:** Tier 1 — `anthropic/claude-opus-5` (OpenRouter) / `gpt-5.6-sol` (OpenAI)
  
**Call site:** `FORECASTER_TIEBREAKER_MODEL` (Tier 1, fixed), `cache_static_prefix=True`

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Wraps the full [binary-prompt-template](../01-active/binary-prompt-template.md) — the original prompt is inserted whole as `{prompt}`, then this text and every run's reasoning are appended.

## Intended use

**Pipeline stage:** Forecasting, post-ensemble.

**Trigger:** Fires when the ensemble's probability spread reaches `SPREAD_THRESHOLD = 30` percentage points. Binary questions only — there is no MC or numeric equivalent.

A single strong model re-reads all three runs' reasoning and casts one final probability, told to discard runs that misread the question. Replaces the aggregate rather than adjusting it.

## Inserts

- `{prompt}`
- `{min(probabilities):.0f}`
- `{max(probabilities):.0f}`
- `{prob_spread:.0f}`
- `{rationale_blocks}`

`{rationale_blocks}` carries each run's model name, its probability, and its full reasoning — so this is the largest single forecasting call when it fires.

## Length

- **Scaffold (this file's template text):** 584 chars, 5 slot(s)
- **Filled prompt as sent:** ~25K – 100K chars (original prompt + three full rationales)

## Notes

The only ensemble-level synthesis step in the bot. Numeric questions, where run disagreement smears the aggregated CDF (`quantile_average_cdfs` does no regime matching), have no equivalent.

## Template

```text
{prompt}

---

IMPORTANT: Multiple independent forecasting runs produced highly divergent results. Their probability estimates ranged from {min(probabilities):.0f}% to {max(probabilities):.0f}% (spread: {prob_spread:.0f} percentage points). Please review all the reasoning from each run below and cast a single final probability, carefully weighing the strongest arguments and discarding any runs that appear to have misread the question or made obvious errors.

{rationale_blocks}

Based on all of the above reasoning, give your final synthesized answer as: "Probability: ZZ%", 0-100
```
