# Artifact Check — Prior Check Section

**Name in code:** `_PRIOR_CHECK_SECTION` (research/pipeline.py)  
**Source:** [research/pipeline.py:917](../../research/pipeline.py#L917)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** Same call as the parent — no separate LLM request.

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Formatted into `{prior_check_section}` of [artifact-check-prompt](../01-active/artifact-check-prompt.md).

## Intended use

**Pipeline stage:** Research step 6, second pass.

**Trigger:** Only on a re-check after a focused retry, when a prior artifact-check result exists.

Shows the audit its own previous verdict so the re-check can tell genuine new retrieval from a repeat of the same gap.

## Inserts

- `{prior_check_json}`

`{prior_check_json}` is the previous verdict as indented JSON.

## Length

- **Scaffold (this file's template text):** 589 chars, 1 slot(s)
- **Filled prompt as sent:** ~0.6K – 2K chars

## Notes

Relevant to the Commonwealth 44512 pattern, where a retry actually RETRIEVED the missing artifact but a stale 'missing' verdict survived downstream.

## Template

```text

An EARLIER automated check of the pre-retry research produced the verdict below, and a
focused retry then scraped additional sources specifically to verify it. Your job now is to
RECONCILE, not restate: check each claim in the earlier verdict against what the retry
actually retrieved. If a retry extract corrects, redates, or contradicts an earlier claim,
the retry's scraped content OUTRANKS the earlier inference — report the corrected fact and
drop the superseded claim. Do not carry any earlier claim forward unexamined.

Earlier (pre-retry) verdict to reconcile:
{prior_check_json}

```
