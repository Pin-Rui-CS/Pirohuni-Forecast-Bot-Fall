# Manifold Relevance Scoring

**Name in code:** `_score_markets` (research/manifold_research.py)  
**Source:** [research/manifold_research.py:251](../../research/manifold_research.py#L251)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `_MANIFOLD_SCORING_MODEL`, temperature 0, `"manifold/relevance-scoring"`

## Intended use

**Pipeline stage:** Prediction-market research, call 2 of 2 for Manifold.

Scores candidate Manifold markets 0–10. Note the forecaster prompts separately instruct the model to discount Manifold as a play-money signal — that discount lives downstream, not here.

**Related prompts:** Sibling of [kalshi](kalshi-relevance-scoring-prompt.md) and [polymarket](polymarket-relevance-scoring-prompt.md).

## Inserts

- `{question}`
- `{numbered}`

`{numbered}` is the candidate market list.

## Length

- **Scaffold (this file's template text):** 585 chars, 2 slot(s)
- **Filled prompt as sent:** ~1K – 8K chars

## Template

```text
Research question: "{question}"

Rate each Manifold Markets prediction market below for relevance to the research question on a scale of 0–10:
  10 = directly measures the same event or outcome
   7 = closely related — strong predictive signal
   4 = tangentially related — weak signal
   0 = completely unrelated

Consider shared entities (people, countries, organisations), shared topic, and whether the market outcome would inform a prediction on the research question.

Markets:
{numbered}

Reply with ONLY a JSON array of numbers, one per market in order. Example: [8.5, 3.0, 6.0]
```
