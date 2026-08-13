# Resolution Source Summary

**Name in code:** `_build_resolution_summary_prompt` (resolution_criteria_scraper.py)  
**Source:** [resolution_criteria_scraper.py:245](../../resolution_criteria_scraper.py#L245)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** temperature 0.1, `max_tokens=2000`

## Intended use

**Pipeline stage:** Resolution-source scraping — runs once per scraped resolution URL, in parallel with the main research chain.

Reads the scraped official resolution page and extracts the state of the resolution target: current values, dates, labels, and how the page presents them. Its output is the AUTHORITATIVE section of the compiler prompt — the one the compiler is told outranks every secondary source.

## Inserts

- `{question_text}`
- `{resolution_criteria}`
- `{key_terms_section}`
- `{url}`
- `{content[:_LLM_MAX_INPUT]}`

Page content is head-truncated at `_LLM_MAX_INPUT = 100,000` chars. `{key_terms_section}` is present only when the resolution criteria supplied specific terms to count.

## Length

- **Scaffold (this file's template text):** 2,363 chars, 5 slot(s)
- **Filled prompt as sent:** ~5K – 102K chars

## Template

```text
You are a research assistant helping a forecaster understand the official resolution source material for a forecasting question.

## Forecast Question
{question_text}

## Resolution Criteria
{resolution_criteria}

{key_terms_section}## Scraped Resolution Source Content ({url})
{content[:_LLM_MAX_INPUT]}

## Task
Write one structured summary of the scraped resolution source content. Use exactly these four sections:

**1. CURRENT STATE:** What does the source currently show? Include exact dates, labels, values, or status fields that matter for resolution.

**2. GAP TO RESOLUTION:** What exactly would need to appear or change on the source for this question to resolve? Has any part of the criteria already been met?

**3. HISTORICAL PATTERN:** If the scraped content contains relevant past entries, list the most relevant dates and describe the cadence. If not, state that the pattern is not present in the scraped content.

**4. KEY AMBIGUITY:** Flag any mismatch between the resolution criteria and what the scraped source actually displays, including labels, date ranges, formatting, or scoping issues.

## Important Rules
- Base your summary only on the scraped content provided above.
- Do not search for, identify, request, or recommend additional links.
- Do not use external knowledge to fill gaps in the scraped data.
- If information is missing, say 'not present in scraped content'.
- Quote exact strings where they are important to the resolution criteria.
- TABLES AND ENUMERATIONS: if the content contains a table or list that the resolution value counts over or reads from, reproduce the resolution-relevant rows (entity + status/value) rather than summarizing them away — for a question that counts rows in a category, that means EVERY row and its classification. Then state the row arithmetic explicitly (e.g. '27 rows present: 9 Clear, 12 Partial, 6 Unclear'). If the rows present do not add up to a total the page states, or the table appears cut off, say so explicitly ('N of M rows present; content appears truncated').
- Summarize by reading for relevance, not to hit a length. Include every detail that bears on the resolution criteria, however small — do NOT drop or compress resolution-relevant facts to make the summary shorter. The only things to leave out are navigation, boilerplate, and content with no bearing on the question.
```
