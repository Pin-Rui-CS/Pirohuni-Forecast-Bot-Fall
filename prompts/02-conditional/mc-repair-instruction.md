# Multiple Choice Repair Instruction

**Name in code:** `repair_instruction` in `get_multiple_choice_gpt_prediction`  
**Source:** [forecasters/multiple_choice.py:296](../../forecasters/multiple_choice.py#L296)  
**Tier:** Tier 1 ×2 + Tier 2 ×1 — the 3-run ensemble sends runs 1–2 to `FORECASTER_MODELS` (Tier 1: `anthropic/claude-opus-5`, `openai/gpt-5.6-sol`) and run 3 to `HETEROGENEOUS_RUN_MODEL` (Tier 2: `anthropic/claude-sonnet-5` / `gpt-5.6-terra`), which reads the **raw** research instead of the compiled brief
  
**Call site:** Same model as the failed run; exactly one repair retry.

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Appended to [multiple-choice-prompt-template](../01-active/multiple-choice-prompt-template.md).

## Intended use

**Pipeline stage:** Forecasting, per-run recovery.

**Trigger:** Fires when the option-probability validator rejects the response.

Restates the required N-line output format and the sum-to-100 constraint.

## Inserts

- `{len(options)}`
- `{options}`

Built with the live option list, so the exact option names appear in the text.

## Length

- **Scaffold (this file's template text):** 186 chars, 2 slot(s)
- **Filled prompt as sent:** ~200 – 400 chars (plus the original prompt)

## Template

```text
End your reply with exactly {len(options)} lines, one per option in this order {options}, each formatted as 'Option_Name: probability'. The probabilities must be numbers that sum to 100.
```
