# Binary Repair Instruction

**Name in code:** `_BINARY_REPAIR_INSTRUCTION` (forecasters/binary.py)  
**Source:** [forecasters/binary.py:244](../../forecasters/binary.py#L244)  
**Tier:** Tier 1 ×2 + Tier 2 ×1 — the 3-run ensemble sends runs 1–2 to `FORECASTER_MODELS` (Tier 1: `anthropic/claude-opus-5`, `openai/gpt-5.6-sol`) and run 3 to `HETEROGENEOUS_RUN_MODEL` (Tier 2: `anthropic/claude-sonnet-5` / `gpt-5.6-terra`), which reads the **raw** research instead of the compiled brief
  
**Call site:** Same model as the failed run; exactly one repair retry.

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Appended to [binary-prompt-template](../01-active/binary-prompt-template.md) by `gather_forecast_runs`, after the failing response and the validator's error message.

## Intended use

**Pipeline stage:** Forecasting, per-run recovery.

**Trigger:** Fires when `_validate_binary_response` cannot find a usable probability line.

Restates the required output format so the run can be salvaged instead of dropped.

## Inserts

None — this prompt has no substitution slots.

None — fixed text.

## Length

- **Scaffold (this file's template text):** 128 chars, 0 slot(s)
- **Filled prompt as sent:** ~128 chars (plus the full original prompt it is appended to)

## Template

```text
Finish with your final answer on its own line, exactly in the form "Probability: ZZ%", where ZZ is an integer between 0 and 100.
```
