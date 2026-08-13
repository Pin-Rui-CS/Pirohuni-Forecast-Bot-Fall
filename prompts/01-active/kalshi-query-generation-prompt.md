# Kalshi Search Query Generation

**Name in code:** `_generate_search_queries` (research/kalshi_research.py)  
**Source:** [research/kalshi_research.py:80](../../research/kalshi_research.py#L80)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `_KALSHI_SCORING_MODEL`, `"kalshi/search-query-generation"`

## Intended use

**Pipeline stage:** Prediction-market research, call 1 of 2 for Kalshi.

Generates 3–4 one-to-three-word queries for local title matching against Kalshi markets. Falls back to keyword extraction if the call fails.

**Related prompts:** One of three near-identical market query-generation prompts — see [manifold](manifold-query-generation-prompt.md) and [polymarket](polymarket-query-generation-prompt.md). Kalshi's differs only in the platform name, an extra 'unless the date is part of the event name' clause, and ASCII `->` where Polymarket uses `→`.

## Inserts

- `{question}`

Only the question title.

## Length

- **Scaffold (this file's template text):** 677 chars, 1 slot(s)
- **Filled prompt as sent:** ~0.8K chars

## Template

```text
Forecasting question: "{question}"

Generate 3-4 search queries to find this topic on Kalshi prediction markets.
Rules:
  - Keep each query to 1-3 words because they will be used for local title matching
  - Use the plain common name for the subject (e.g. 'Brent spot price' -> 'crude oil', 'S&P 500 index' -> 'S&P 500')
  - Use synonyms and alternate names (e.g. also try 'oil price' alongside 'crude oil')
  - Omit dates, ranges, question words, and filler unless the date is part of the event name
  - Think: what 1-3 words would appear in a Kalshi market title about this subject?

Reply with ONLY a JSON array of strings. Example: ["crude oil", "oil price", "Brent crude"]
```
