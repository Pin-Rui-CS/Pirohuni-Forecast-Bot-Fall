# Evidence Plan

**Name in code:** `_build_prompt` (research/evidence_plan.py)  
**Source:** [research/evidence_plan.py:74](../../research/evidence_plan.py#L74)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `call_llm(..., _label="evidence-plan")`, `DEFAULT_EVIDENCE_PLAN_MODEL`

## Intended use

**Pipeline stage:** Research step 2, immediately after AskNews and before query generation.

Decides what the run is actually looking for before any searching happens: names the ONE required evidence artifact (the base-rate anchor), the resolution mechanics, ideal sources, and the fields to extract. Returns JSON. Its `required_artifact` is the yardstick that [artifact-check](../01-active/artifact-check-prompt.md) later grades the research against, so a vague plan here silently weakens the audit downstream.

## Inserts

- `{title}`
- `{resolution_criteria or 'Not provided.'}`
- `{background or 'Not provided.'}`
- `{fine_print or 'Not provided.'}`
- `{_truncate_text(asknews_research or 'No AskNews research was available.', _MAX_ASKNEWS_C...}`
- `{"name": "The one evidence artifact most needed before forecasting",
    "why_it_matters...}`
- `{"source_update_behavior": "Does the resolution source update or publish again before th...}`

AskNews block is truncated to `_MAX_ASKNEWS_CHARS = 40,000`; question fields fall back to "Not provided."

## Length

- **Scaffold (this file's template text):** 2,950 chars, 7 slot(s)
- **Filled prompt as sent:** ~4K – 45K chars (dominated by the AskNews block)

## Template

```text
You are planning research for a forecasting bot.

The bot will search and scrape after this step. Your job is to decide what
information matters most, especially the required base-rate artifact.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or 'Not provided.'}

Background:
{background or 'Not provided.'}

Fine print:
{fine_print or 'Not provided.'}

AskNews research already gathered:
{_truncate_text(asknews_research or 'No AskNews research was available.', _MAX_ASKNEWS_CHARS)}

Return only valid JSON in this shape:
{
  "required_artifact": {
    "name": "The one evidence artifact most needed before forecasting",
    "why_it_matters": "Why this is the base-rate anchor",
    "ideal_sources": ["official source", "primary dataset"],
    "fields_to_extract": ["year", "value", "source_url", "notes"]
  },
  "resolution_mechanics": {
    "source_update_behavior": "Does the resolution source update or publish again before the resolution deadline? State the known or likely cadence, the next expected update, and what evidence would pin it down",
    "update_content_limits": "What new information CAN appear in the source before the deadline, and what CANNOT arrive in time (reporting calendars, disclosure deadlines, data-pipeline or publication lags)",
    "mechanics_queries": ["search query targeting the source's update schedule or the underlying reporting calendar"]
  },
  "direct_evidence": ["Evidence that directly resolves or measures the question"],
  "near_proxy_evidence": ["Evidence that is close but not exact"],
  "weak_proxy_evidence": ["Evidence that is only indirectly related"],
  "background_color": ["Context that may be interesting but should not move the forecast much"],
  "contradictions_to_check": ["Conflicting claims or missing facts to audit"],
  "search_queries": ["concise search query", "another concise search query"]
}

Rules:
- Prefer official, source-of-truth artifacts over broad news.
- For count questions, the required artifact is usually a historical count table.
- For direct prediction markets, distinguish exact-human/outcome markets from adjacent proxy markets.
- When the question resolves off a curated page, tracker, leaderboard, or scheduled data release ("as shown/displayed on X as of date D"), the source's update mechanics — whether it refreshes before the deadline and what a refresh can legally or physically contain — are often MORE decision-relevant than the underlying race. Fill resolution_mechanics with concrete cadences, calendars, and lags to verify (e.g. filing deadlines, release schedules, disclosure windows), and add mechanics_queries that target them specifically.
- When the question resolves by direct observation of an event rather than a published source, set both resolution_mechanics text fields to "Not applicable — resolves by direct observation of the event" and leave mechanics_queries empty.
- Do not forecast or estimate probabilities.
```
