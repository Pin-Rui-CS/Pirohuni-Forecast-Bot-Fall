# Manifold Search Query Generation

**Name in code:** `_generate_search_queries` (research/manifold_research.py)  
**Source:** [research/manifold_research.py:77](../../research/manifold_research.py#L77)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `_MANIFOLD_SCORING_MODEL`, `"manifold/search-query-generation"`

## Intended use

**Pipeline stage:** Prediction-market research, call 1 of 2 for Manifold.

Generates 3–4 short queries for Manifold's search API.

**Related prompts:** Sibling of [kalshi](kalshi-query-generation-prompt.md) and [polymarket](polymarket-query-generation-prompt.md).

## Inserts

- `{question}`

Only the question title.

## Length

- **Scaffold (this file's template text):** 627 chars, 1 slot(s)
- **Filled prompt as sent:** ~0.8K chars

## Template

```text
Forecasting question: "{question}"

Generate 3-4 search queries to find this topic on Manifold Markets.
Rules:
  - Keep each query to 1-3 words — shorter queries work better on Manifold
  - Use the plain common name for the subject (e.g. 'Brent spot price' → 'crude oil', 'S&P 500 index' → 'S&P 500')
  - Use synonyms and alternate names (e.g. also try 'oil price' alongside 'crude oil')
  - Omit dates, ranges, question words, and filler
  - Think: what 1-3 words would appear in a Manifold Markets question title about this subject?

Reply with ONLY a JSON array of strings. Example: ["crude oil", "oil price", "Brent crude"]
```
