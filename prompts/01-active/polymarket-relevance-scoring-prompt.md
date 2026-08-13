# Polymarket Relevance Scoring

**Name in code:** `_score_events` (research/polymarket_research.py)  
**Source:** [research/polymarket_research.py:256](../../research/polymarket_research.py#L256)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `_POLYMARKET_SCORING_MODEL`, temperature 0, `"polymarket/relevance-scoring"`

## Intended use

**Pipeline stage:** Prediction-market research, call 2 of 2 for Polymarket.

Scores candidate Polymarket events 0–10; `MIN_RELEVANCE = 5.0` and `MAX_RESULTS = 3` gate what survives into the research.

**Related prompts:** Sibling of [kalshi](kalshi-relevance-scoring-prompt.md) and [manifold](manifold-relevance-scoring-prompt.md).

## Inserts

- `{question}`
- `{numbered}`

`{numbered}` is the candidate event list.

## Length

- **Scaffold (this file's template text):** 579 chars, 2 slot(s)
- **Filled prompt as sent:** ~1K – 8K chars

## Template

```text
Research question: "{question}"

Rate each Polymarket prediction market below for relevance to the research question on a scale of 0–10:
  10 = directly measures the same event or outcome
   7 = closely related — strong predictive signal
   4 = tangentially related — weak signal
   0 = completely unrelated

Consider shared entities (people, countries, organisations), shared topic, and whether the market outcome would inform a prediction on the research question.

Markets:
{numbered}

Reply with ONLY a JSON array of numbers, one per market in order. Example: [8.5, 3.0, 6.0]
```
