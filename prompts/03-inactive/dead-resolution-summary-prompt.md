# Dead Resolution Page Summary

**Name in code:** `_build_summary_prompt` (resolution_criteria_scraper.py)  
**Source:** [resolution_criteria_scraper.py:306](../../resolution_criteria_scraper.py#L306)  
**Tier:** n/a — never called

> **Not in the pipeline.** Defined but called from nowhere. The live path is [resolution-summary-prompt](../01-active/resolution-summary-prompt.md) (`_build_resolution_summary_prompt`). Its only would-be caller, `_legacy_scrape_resolution_sources_with_followups`, is itself dead.

## Intended use

**Pipeline stage:** None.

An earlier, longer take on resolution-page reading, with an explicit two-step EXTRACT-then-summarise structure and detailed guidance on ignoring site chrome in full-page markdown scrapes. Some of that navigation-stripping guidance never made it into the live prompt.

## Inserts

- `{question_text}`
- `{resolution_criteria}`
- `{key_terms_section}`
- `{url}`
- `{content[:_LLM_MAX_INPUT]}`

Page content head-truncated at `_LLM_MAX_INPUT = 100,000`.

## Length

- **Scaffold (this file's template text):** 3,299 chars, 5 slot(s)
- **Filled prompt as sent:** ~3.3K chars scaffold

## Template

```text
You are a research assistant helping a forecaster understand a resolution source.

## Forecast Question
{question_text}

## Resolution Criteria
{resolution_criteria}

{key_terms_section}## Web Page Content (from {url})
{content[:_LLM_MAX_INPUT]}

## IMPORTANT: How to read this content
The content above is a full-page scrape rendered as markdown. It includes navigation menus, header/footer links, and other site chrome mixed in with the actual page data. Navigation menus typically appear as bulleted link lists near the top and bottom of the content. IGNORE these — focus only on the substantive content in the middle of the page (headings, data entries, tables, paragraphs). Labels and entry types (e.g. 'Grand Chamber Judgment', 'Chamber Judgment') may appear as markdown link text in the form [Label](url) — treat the text inside the brackets as the label, not the URL.

## Task

### Step 1: EXTRACT
Scan the web page content and list up to 10 specific entries that are most relevant to the resolution criteria. For each, quote the exact text showing dates, labels, values, or status fields that matter. If the resolution criteria mention a specific label or term, search for that exact string — including as markdown link text in the form [Label](url) — and report whether it appears, how many times, and in what context.

### Step 2: SUMMARIZE
Using your extractions above, write a structured summary with exactly these four sections:

**1. CURRENT STATE:** What does the resolution source currently show? List the most recent 5-10 relevant entries with their exact dates and labels. Explicitly state whether any entries fall within the resolution criteria's date range or match its required labels. If none do, say so clearly and state what the most recent qualifying entry is and when it appeared.

**2. GAP TO RESOLUTION:** What exactly would need to appear/change on the resolution source for this question to resolve Yes? Has any part of the criteria already been met?

**3. HISTORICAL PATTERN:** List the dates of the most recent 5-10 qualifying entries to establish the cadence. Calculate the gaps between them. Note the longest gap and the average gap. State how long it has been since the last qualifying entry.

**4. KEY AMBIGUITY:** Is there any mismatch between what the resolution criteria require (exact labels, specific page, date ranges) and what the source actually displays? Flag any labeling, formatting, or scoping issues.

## IMPORTANT RULES
- Base your summary ONLY on what is actually present in the web page content provided above.
- Do not identify, request, or recommend follow-up links. The crawler has already gathered the source material to use.
weekly report listed) — older entries matter as much as recent ones for - If a field or label is visible in the content, cite it exactly as it appears (including if it is inside markdown link syntax like [Label](url)).
- If information is missing from the scrape, say 'not present in scraped content' — do not speculate about what the page 'likely' or 'appears to' contain.
- Do not use external knowledge about the source to fill gaps in the scraped data.
- When stating that something is absent, confirm you searched for it by noting the exact string you looked for, including its markdown link form if applicable.
```
