# Numeric Repair Instruction

**Name in code:** `_NUMERIC_REPAIR_INSTRUCTION` (forecasters/numeric.py)  
**Source:** [forecasters/numeric.py:2403](../../forecasters/numeric.py#L2403)  
**Tier:** Tier 1 ×2 + Tier 2 ×1 — the 3-run ensemble sends runs 1–2 to `FORECASTER_MODELS` (Tier 1: `anthropic/claude-opus-5`, `openai/gpt-5.6-sol`) and run 3 to `HETEROGENEOUS_RUN_MODEL` (Tier 2: `anthropic/claude-sonnet-5` / `gpt-5.6-terra`), which reads the **raw** research instead of the compiled brief
  
**Call site:** Same model as the failed run; exactly one repair retry.

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Appended to [numeric-prompt-template](../01-active/numeric-prompt-template.md).

## Intended use

**Pipeline stage:** Forecasting, per-run recovery.

**Trigger:** Fires when the response cannot be parsed into a valid component mixture or PMF.

Restates the components/PMF JSON contract and explicitly forbids falling back to percentile lists — the pre-overhaul format the model still drifts toward.

## Inserts

None — this prompt has no substitution slots.

None — fixed text.

## Length

- **Scaffold (this file's template text):** 273 chars, 0 slot(s)
- **Filled prompt as sent:** ~273 chars (plus the original prompt)

## Template

```text
End your reply with ONLY the final JSON object described above: a top-level "components" list of 1–3 items, each with "family", "params" (matching that family exactly), and "weight". For a discrete/count question, use the "pmf" form instead. Do not output percentile lists.
```
