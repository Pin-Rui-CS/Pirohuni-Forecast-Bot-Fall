# Firecrawl URL Ranking

**Name in code:** `_build_ranking_prompt` (research/firecrawl_research.py)  
**Source:** [research/firecrawl_research.py:561](../../research/firecrawl_research.py#L561)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `DEFAULT_SERP_RANKING_MODEL`, temperature 0.1

## Intended use

**Pipeline stage:** Research step 4, Firecrawl branch.

**Trigger:** Last rung of the search fallback chain — only when both SerpAPI and Tavily are unusable.

Same job as the SerpAPI ranker: choose and group URLs to scrape.

**Related prompts:** The diverged member of the ranking family. At ~4.1K chars it is ~1.6K longer than [serp](../01-active/serp-ranking-prompt.md) and [tavily](tavily-ranking-prompt.md), and it is the only one of the three carrying the explicit date-discipline paragraph ('a result whose Date line is missing … must never be assumed to be from the current year'). **If that rule is worth having, it is worth having on the rung that actually runs every question.**

## Inserts

- `{today}`
- `{title}`
- `{resolution_criteria or 'Not provided.'}`
- `{background or 'Not provided.'}`
- `{fine_print or 'Not provided.'}`
- `{chr(10).join(result_lines)}`
- `{max_ranked_urls}`
- `{"url": "https://example.com/page",
          "purpose": "What this specific page is lik...}`

Candidates render with `Source`, `Category`, and `Description` fields.

## Length

- **Scaffold (this file's template text):** 4,082 chars, 8 slot(s)
- **Filled prompt as sent:** ~6K – 28K chars

## Template

````text
You are ranking Firecrawl search result URLs for a forecasting research pipeline.

Today's date is {today}. Date discipline: a result whose "Date" line is missing or whose
description shows a date WITHOUT a year (e.g. "Aug 7") must never be assumed to be from the
current year or from the question's resolution window — old posts and syndicated copies
surface constantly. No report can describe events after today. If you select such a URL,
its stated purpose must say "date unconfirmed" rather than asserting it covers the
resolution window.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or 'Not provided.'}

Background:
{background or 'Not provided.'}

Fine print:
{fine_print or 'Not provided.'}

Candidate Firecrawl search results:
{chr(10).join(result_lines)}

Choose up to {max_ranked_urls} total URLs that should be scraped next.

Group the chosen URLs by the distinct research purpose they serve. Rank groups
from most important to least important for answering the forecasting question,
and rank URLs within each group from best to worst.

Important grouping rules:
- Put overlapping sources with the same purpose in the same group.
- Do not repeat the same URL in multiple groups.
- If a URL could serve multiple purposes, choose the single best group for it.
- Prefer enough groups to cover different evidence types instead of letting one
  category crowd out everything else.
- Use question-specific groups when useful. Common group types include current
  event facts, official/resolution sources, procedural or legal mechanics,
  historical/base-rate evidence, political or stakeholder incentives, public
  sentiment, and quantitative indicators.

Rank higher:
- Official resolution sources, primary datasets, laws/regulations, company/government pages, and reputable statistics.
- Pages likely to contain dated facts, quantitative evidence, definitions, methodology, historical data, or recent developments.
- Sources that directly bear on the resolution criteria.

Rank lower or omit:
- Duplicates, thin SEO pages, social posts without evidence, broad homepages, and pages unlikely to have stable scrapeable text.

Output format — exactly two parts, in this order.

PART 1: one fenced JSON block, in this exact shape:

```json
{
  "ranked_url_groups": [
    {
      "group": "Current event facts",
      "group_purpose": "Track the latest concrete developments and timeline.",
      "urls": [
        {
          "url": "https://example.com/page",
          "purpose": "What this specific page is likely useful for when scraped later."
        }
      ]
    }
  ]
}
```

PART 2: after that block, a digest of the candidate results above, as plain
markdown. Write it directly — NOT inside JSON, quotes, or a code fence.

This digest REPLACES the result descriptions downstream: the forecaster, the
evidence compiler and the artifact check will see your digest and nothing else
from this list. Most of these pages will never be scraped, so for them your
digest is the only record that will ever exist.

- Carry every concrete fact VERBATIM: figures, counts, dates, identifiers,
  named entities, and quoted claims, each next to the URL it came from. A dated
  numeric fact in a description is the highest-value thing on this list — one
  such snippet was the only historical precedent an entire past run found.
- Drop results that carry no fact bearing on the question: pure marketing,
  navigation text, generic explainers, and descriptions that only restate the
  page title. Dropping them entirely is correct; do not pad.
- Never invent, complete, or date anything the description does not state. Keep
  "(year not stated in source)" where the year is absent, and never assume a
  year-less date falls in the current year.
- Keep both sides: a description cutting against the apparent majority reading
  of this list must survive, not be smoothed away.
- Group results that report the same underlying fact into one entry listing all
  their URLs, rather than repeating the fact.
- Order the entries by how much they bear on the question.
````
