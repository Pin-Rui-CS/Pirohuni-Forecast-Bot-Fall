# Wikipedia Adapter Extract

**Name in code:** `_extract_relevant_wikipedia_content` (Adapters/Wikipedia.py)  
**Source:** [Adapters/Wikipedia.py:121](../../Adapters/Wikipedia.py#L121)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `DEFAULT_EXTRACT_MODEL`, temperature 0.1

## Intended use

**Pipeline stage:** Inside a scrape cycle, replacing the generic scrape path for that URL.

**Trigger:** Only when a ranked URL resolves to Wikipedia and the adapter registry claims it.

Reads a Wikipedia page converted to markdown with tables preserved, and extracts forecast-relevant facts and table detail without forecasting or adding outside knowledge.

## Inserts

- `{forecast_context.strip() or 'No explicit forecast context was provided. Extract general...}`
- `{url}`
- `{metadata.get('api_url')}`
- `{metadata.get('title')}`
- `{metadata.get('description') or 'Not provided.'}`
- `{source_markdown}`

Source markdown is truncated to `MAX_WIKIPEDIA_SOURCE_CHARS = 80,000`; individual table cells to `MAX_CELL_CHARS = 260`.

## Length

- **Scaffold (this file's template text):** 2,030 chars, 6 slot(s)
- **Filled prompt as sent:** ~3K – 82K chars

## Template

````text
You are a research extraction assistant for a forecasting pipeline.

Your job is to read a Wikipedia page that has been converted into markdown while preserving tables, then extract the facts, tables, and structured details that are useful for the forecast question.

Do NOT forecast. Do NOT estimate probabilities. Do NOT add outside knowledge. Do NOT discard table details merely because they are tabular.

Forecast context supplied by the caller:
```text
{forecast_context.strip() or 'No explicit forecast context was provided. Extract generally forecast-relevant page evidence.'}
```

Wikipedia source metadata:
- Source URL: {url}
- API URL: {metadata.get('api_url')}
- Title: {metadata.get('title')}
- Description: {metadata.get('description') or 'Not provided.'}

Extraction instructions:
- Preserve all concrete facts relevant to resolving or forecasting the question: dates, rules, candidates, parties, vote counts, polling numbers, results, schedules, eligibility rules, named stakeholders, and caveats.
- If a relevant table appears in the source, reproduce it as a clean markdown table when practical. If the table is too large, keep all important rows and explain what was omitted.
- For election pages, pay special attention to electoral system, candidate/party tables, polling/opinion tables, endorsements, campaign timeline, prior-election context, and official dates.
- Keep source wording close enough that the next LLM can audit it, but remove navigation clutter, reference lists, coordinates, edit labels, and unrelated disambiguation material.
- Organize the output with short headings and compact bullets/tables.
- Include a brief "Omitted as likely irrelevant" section only if major page sections were ignored.

Return markdown only, in this structure:
# Wikipedia Extract For Forecasting

## Why This Page Matters
...

## Key Facts
...

## Relevant Tables And Structured Data
...

## Useful Caveats / Gaps
...

## Omitted As Likely Irrelevant
...

Wikipedia page markdown:
```markdown
{source_markdown}
```
````
