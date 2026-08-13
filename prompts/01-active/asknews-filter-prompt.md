# AskNews Filter

**Name in code:** `_build_prompt` (research/asknews_filter.py)  
**Source:** [research/asknews_filter.py:149](../../research/asknews_filter.py#L149)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `call_llm(..., _label="asknews-filter")`, `DEFAULT_ASKNEWS_FILTER_MODEL`

## Intended use

**Pipeline stage:** Research step 1.5 — runs on the AskNews output before anything else reads it.

A filter, explicitly not a summariser: removes articles that do not bear on the question and returns the rest UNCHANGED. Exists because AskNews is the largest single input and was crowding out later stages. Carries a both-directions rule so it cannot quietly prune evidence that cuts against the majority narrative.

## Inserts

- `{title}`
- `{resolution_criteria or 'Not provided.'}`
- `{background or 'Not provided.'}`
- `{fine_print or 'Not provided.'}`
- `{asknews_research}`

Skipped entirely when AskNews returned nothing usable.

## Length

- **Scaffold (this file's template text):** 2,392 chars, 5 slot(s)
- **Filled prompt as sent:** ~3K – 60K chars (the AskNews block dominates and is not capped here)

## Template

```text
You are filtering a block of news articles gathered for a forecasting question,
before the rest of the research pipeline reads it.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or 'Not provided.'}

Background:
{background or 'Not provided.'}

Fine print:
{fine_print or 'Not provided.'}

Your job is to REMOVE what does not bear on this question and RETURN THE REST
UNCHANGED. You are a filter, not a summariser.

What to keep:
- Any article carrying a concrete fact that bears on the question: figures,
  counts, dates, rules, thresholds, named actors, scheduled events, or a stated
  trend. Keep it even if it cuts against the apparent majority reading of the
  block — both directions must survive.
- Anything touching the resolution source itself: how it is published, how
  often it updates, and what it has reported.
- If you are unsure whether an article bears on the question, KEEP it. The cost
  of keeping a marginal article is a few hundred characters; the cost of
  dropping a load-bearing one is a forecast built without it.

What to remove:
- Whole articles that carry no fact bearing on the question: unrelated topics,
  pure commentary, and process coverage with no dated content.
- A repeat of an article you have already kept: keep the fuller copy and delete
  the duplicate outright.
- Inside an article you keep, sentences that are pure boilerplate, syndication
  notices, or subscription/navigation text.

Rules you must not break:
- NEVER rewrite, paraphrase, summarise, translate, reorder or "clean up" the
  text of an article you keep. Reproduce its wording character for character.
  You may only delete whole articles and whole sentences.
- Reproduce each kept article in its original format, with its heading line and
  all of its trailing metadata lines intact and unaltered:
  **Title**, then the body, then "Original language:", "Publish date:", and
  "Source:[name](url)". Every kept article MUST keep its Publish date and
  Source lines — they are how the rest of the pipeline dates and cites it.
- Keep the articles in the order they appear below.
- Do not add commentary, headings, counts, or notes of your own. Do not say
  what you removed. Return only the surviving article blocks.
- Preserve the first line of the block ("Here are the relevant news articles:")
  exactly as it appears.

Articles to filter:
{asknews_research}
```
