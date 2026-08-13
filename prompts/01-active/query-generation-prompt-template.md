# Search Query Generation

**Name in code:** `QUERY_GENERATION_PROMPT_TEMPLATE` (query_maker.py)  
**Source:** [query_maker.py:34](../../query_maker.py#L34)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `DEFAULT_QUERY_GENERATION_MODEL`, temperature 0.2

## Intended use

**Pipeline stage:** Research step 3 — turns the question + evidence plan into search engine queries.

Produces up to `DEFAULT_QUERY_COUNT = 8` Google-style queries, prioritised to find the required artifact from the evidence plan first, then base rates, then recent events. The queries feed whichever search provider wins the fallback chain.

## Inserts

- `{today}`
- `{title}`
- `{options}`
- `{resolution_criteria}`
- `{background}`
- `{fine_print}`
- `{asknews_research}`
- `{max_queries}`
- `{"query": "google query string",
      "purpose": "brief reason this query is worth runn...}`

`{asknews_research}` carries BOTH the AskNews research and the evidence plan, concatenated, truncated to `DEFAULT_ASKNEWS_CHAR_LIMIT = 40,000`. The evidence plan is placed first deliberately — concatenated after AskNews it was truncated out entirely.

## Length

- **Scaffold (this file's template text):** 1,271 chars, 9 slot(s)
- **Filled prompt as sent:** ~3K – 45K chars

## Template

```text
You are a research assistant who generates Google search queries for a forecasting question.

Your job is to generate Google Search Queries that will help generate useful URLs in a research pipeline.

Today is {today}.

Forecasting question:
{title}

Options, if applicable:
{options}

Resolution criteria:
{resolution_criteria}

Background:
{background}

Fine print:
{fine_print}

AskNews research and evidence plan already gathered:
{asknews_research}

Create up to {max_queries} Google search queries.

Search Queries Priorities:
1. Find the required evidence artifact named in the evidence plan, especially official or primary-source data.
2. Establish a base-rate using historical trends and direct market predictions.
3. Check for recent events that are unique to this instance.
4. Find statistical evidence and concrete values.
5. Check volatility and uncertainty drivers.

Style of Queries:
1. Concise enough to work well in Google.
2. Avoid duplicate queries
3. Favour open-ended but specific questions

Return only valid JSON in this exact shape:
{{
  "queries": [
    {{
      "query": "google query string",
      "purpose": "brief reason this query is worth running",
      "priority": 1
    }}
  ]
}}

Priority scale: 1 = must-run, 2 = useful, 3 = optional.
```
