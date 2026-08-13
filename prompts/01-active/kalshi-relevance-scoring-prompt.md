# Kalshi Relevance Scoring

**Name in code:** `_score_markets` (research/kalshi_research.py)  
**Source:** [research/kalshi_research.py:343](../../research/kalshi_research.py#L343)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `_KALSHI_SCORING_MODEL`, temperature 0, `"kalshi/relevance-scoring"`

## Intended use

**Pipeline stage:** Prediction-market research, call 2 of 2 for Kalshi.

Scores each candidate market 0–10 for relevance to the question; markets at or above the threshold are kept and rendered into the research. Returns a bare JSON array of numbers.

**Related prompts:** One of three near-identical market scoring prompts — see [manifold](manifold-relevance-scoring-prompt.md) and [polymarket](polymarket-relevance-scoring-prompt.md).

## Inserts

- `{question}`
- `{numbered}`

`{numbered}` is the candidate market list, one per line.

## Length

- **Scaffold (this file's template text):** 575 chars, 2 slot(s)
- **Filled prompt as sent:** ~1K – 8K chars depending on candidate count

## Template

```text
Research question: "{question}"

Rate each Kalshi prediction market below for relevance to the research question on a scale of 0-10:
  10 = directly measures the same event or outcome
   7 = closely related - strong predictive signal
   4 = tangentially related - weak signal
   0 = completely unrelated

Consider shared entities (people, countries, organisations), shared topic, and whether the market outcome would inform a prediction on the research question.

Markets:
{numbered}

Reply with ONLY a JSON array of numbers, one per market in order. Example: [8.5, 3.0, 6.0]
```
