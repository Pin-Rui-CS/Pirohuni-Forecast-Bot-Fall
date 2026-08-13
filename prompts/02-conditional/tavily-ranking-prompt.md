# Tavily URL Ranking

**Name in code:** `_build_ranking_prompt` (research/tavily_research.py)  
**Source:** [research/tavily_research.py:444](../../research/tavily_research.py#L444)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** temperature 0.1 at the call site (0.2 default on the module entry point)

## Intended use

**Pipeline stage:** Research step 4, Tavily branch.

**Trigger:** Search providers are a priority fallback chain (SerpAPI → Tavily → Firecrawl) and only the first usable one runs. This fires only when SerpAPI is unusable.

Same job as the SerpAPI ranker: choose and group URLs to scrape.

**Related prompts:** Near-duplicate of [serp-ranking-prompt](../01-active/serp-ranking-prompt.md) (~2.5K, effectively the same text) and [firecrawl-ranking-prompt](firecrawl-ranking-prompt.md) (~4.1K, has diverged).

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

Candidates render with a Tavily relevance `Score` and full `Content`, not a snippet.

## Length

- **Scaffold (this file's template text):** 2,531 chars, 8 slot(s)
- **Filled prompt as sent:** ~5K – 25K chars

## Template

```text
You are ranking Tavily search result URLs for a forecasting research pipeline.

Today's date is {today}. Date discipline: a result whose "Date" line is missing or whose
content shows a date WITHOUT a year (e.g. "Aug 7") must never be assumed to be from the
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

Candidate Tavily search results:
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

Return only valid JSON in this exact shape:
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
