# Wayback Snapshot History

**Name in code:** `_summarize_snapshot_history` (resolution_criteria_scraper.py)  
**Source:** [resolution_criteria_scraper.py:396](../../resolution_criteria_scraper.py#L396)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** temperature 0.1, `max_tokens=1200`

## Intended use

**Pipeline stage:** Resolution-source scraping, fallback path.

**Trigger:** Only on the Wayback fallback rung, when dated captures of the resolution source were retrieved. One call regardless of snapshot count.

Reconstructs the resolution source's history from dated captures: a value time series, the observed update cadence, and what changed between captures. Gives the forecaster a real same-source flow rate instead of an improvised one — this is what makes the 'source not updated' branch in Phase 0.5 quantifiable.

## Inserts

- `{question_text}`
- `{resolution_criteria}`
- `{url}`
- `{snapshot_blocks[:_LLM_MAX_INPUT]}`

`{snapshot_blocks}` is head-truncated at `_LLM_MAX_INPUT = 100,000`. The prompt explicitly forbids presenting any capture as the current value.

## Length

- **Scaffold (this file's template text):** 1,445 chars, 4 slot(s)
- **Filled prompt as sent:** ~2K – 102K chars

## Template

```text
You are a research assistant reconstructing the HISTORY of the official resolution source for a forecasting question, from dated Wayback Machine captures of the page.

## Forecast Question
{question_text}

## Resolution Criteria
{resolution_criteria}

## Dated captures of {url} (oldest first)
{snapshot_blocks[:_LLM_MAX_INPUT]}

## Task
Write a compact history with exactly these three sections:

**1. VALUE TIME SERIES:** For each capture date, the value(s) the resolution criteria care about, one line per capture: `YYYY-MM-DD (capture): <value(s)>`. Quote exact figures/labels. If a capture does not show the value, write 'not visible in capture'.

**2. UPDATE CADENCE:** Any 'last updated'/'as of' stamps visible in the captures, and what the differences between captures imply about how often the page actually changes. State the observed gaps in weeks/months.

**3. OBSERVED CHANGES:** What changed between consecutive captures (entries added/removed, statuses reclassified, totals moved). Compute the simple rate of change per month where the series allows it, showing the arithmetic.

## Rules
- Use ONLY the captures above. No external knowledge, no speculation about what later values 'should' be.
- These captures are HISTORICAL. Do not present any of them as the current value; the live page was scraped separately.
- If the captures are unreadable or irrelevant to the resolution criteria, say exactly that in one line per section.
```
