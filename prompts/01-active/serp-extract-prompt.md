# Per-Cycle Scrape Extract

**Name in code:** `_build_extract_prompt` (research/serp_research.py)  
**Source:** [research/serp_research.py:1560](../../research/serp_research.py#L1560)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `DEFAULT_EXTRACT_MODEL`

## Intended use

**Pipeline stage:** Research step 5 — runs once per scrape cycle (up to `DEFAULT_MAX_SCRAPE_CYCLES = 3`), each time over that cycle's newly scraped pages.

Turns raw scraped page text into a running evidence report, and decides what the next scrape cycle should chase. Heavily hedged against fabrication: it must record only what a page literally states. This is the single largest-input research call in the pipeline.

## Inserts

- `{today}`
- `{title}`
- `{resolution_criteria or 'Not provided.'}`
- `{background or 'Not provided.'}`
- `{fine_print or 'Not provided.'}`
- `{group_lines or '- None'}`
- `{report_task}`
- `{part_one_shape}`
- `{"group": "Exact category name", "reason": "What is still missing or unusable."}`
- `{"lacking_groups": []}`
- `{previous_report or 'No previous report yet.'}`
- `{scrape_text or 'No scrape packets available.'}`

Scrape content is capped at `_MAX_EXTRACT_INPUT_CHARS = 180,000` total, `_MAX_SCRAPE_CHARS = 40,000` per page. After cycle 1 only the categories the previous cycle reported lacking are listed in `{group_lines}`.

## Length

- **Scaffold (this file's template text):** 3,116 chars, 12 slot(s)
- **Filled prompt as sent:** ~20K – 185K chars — the biggest research prompt in the bot

## Template

````text
You are a research assistant compiling scraped web evidence for a forecasting question.
Do not make a prediction, estimate probabilities, or recommend an answer.

Today's date is {today}.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or 'Not provided.'}

Background:
{background or 'Not provided.'}

Fine print:
{fine_print or 'Not provided.'}

Research categories you may use when requesting more scraping:
{group_lines or '- None'}

Task:
- {report_task}
- State the actual extracted contents: facts, numbers, dates, names, rules, and quoted/near-quoted source claims.

Ground every statement in the scraped content — never fabricate or add information:
- Record ONLY what the scraped page content actually states. Do not infer, assume, complete, or "fill in" any fact, value, date, or attribution that is not literally present in that page's content.
- The "URL purpose" lines are pre-scrape guesses, not evidence. Never lift a fact, number, or date from them.
- Specifically for dates: never attach a year (or any date) that does not appear in the source itself. To resolve the year of an undated figure, use ONLY the page's "Source publish date" line (shown with each scrape packet) or an explicit in-text date — a figure published in July 2025 describes July 2025, regardless of what the question asks about. If a figure has a month but no year and the Source publish date is "not provided", record it exactly as stated and add "(year not stated in source)". Do NOT assume it refers to {today}'s year or the year the question asks about.
- If you are tempted to write something the source does not actually say, omit it instead. Missing information must be reported as missing, not inferred.
- Do not write vague placeholders such as "this link contains information", "the article discusses", or "can be found at this URL" unless you also state the concrete information.
- Use any relevant information from a URL, even if it goes beyond that URL's intended purpose.
- Preserve source URLs next to important facts.
- Include dates, vote counts, thresholds, named stakeholders, procedural rules, and concrete evidence when present.
- Note failed or thin scrapes only when they affect coverage.
- Do not forecast or state whether the event will happen.
- If a category still lacks enough useful information, request more scraping for that category.
- Pick lacking categories only from the exact category names listed above.
- If no more scraping is needed, use an empty lacking_groups list.

Output format — exactly two parts, in this order:

PART 1: {part_one_shape} Write it directly — do NOT wrap it in
JSON, quotes, or a code fence.

PART 2: After the report, one fenced JSON block containing ONLY the lacking
categories, in exactly this shape:

```json
{"lacking_groups": [{"group": "Exact category name", "reason": "What is still missing or unusable."}]}
```

If no more scraping is needed, end with:

```json
{"lacking_groups": []}
```

Previous compiled report:
{previous_report or 'No previous report yet.'}

New scrape packets:
{scrape_text or 'No scrape packets available.'}
````
