# Resolution Source Compile

**Name in code:** `_build_compile_prompt` (resolution_criteria_scraper.py)  
**Source:** [resolution_criteria_scraper.py:564](../../resolution_criteria_scraper.py#L564)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** temperature 0.1, `max_tokens=2000`

## Intended use

**Pipeline stage:** Resolution-source scraping — one call, after all per-URL summaries are done.

Merges the per-page resolution summaries into one four-section report (current state, update behaviour, and so on) that becomes the Resolution Mechanics material in the brief.

## Inserts

- `{question_text}`
- `{resolution_criteria}`
- `{summaries_text}`

`{summaries_text}` is the concatenated per-URL summaries, capped at `_LLM_MAX_INPUT`.

## Length

- **Scaffold (this file's template text):** 1,035 chars, 3 slot(s)
- **Filled prompt as sent:** ~3K – 100K chars

## Template

```text
You are a research assistant helping a forecaster. You have been given summaries from multiple web pages relevant to a forecast question. Compile them into a single coherent report.

## Forecast Question
{question_text}

## Resolution Criteria
{resolution_criteria}

## Individual Page Summaries

{summaries_text}

## Task
Synthesize all of the above into a single structured report using exactly these four sections:

**1. CURRENT STATE:** What do the sources collectively show? Combine the most recent relevant entries across all sources with their dates and labels.

**2. GAP TO RESOLUTION:** What exactly would need to appear/change for this question to resolve Yes? Has any part of the criteria already been met?

**3. HISTORICAL PATTERN:** Combine the historical patterns across all sources. Note cadence, gaps, and how long it has been since the last qualifying entry.

**4. KEY AMBIGUITY:** Note any conflicts between sources, or gaps in coverage.

Base your report only on the summaries provided. Do not speculate beyond them.
```
