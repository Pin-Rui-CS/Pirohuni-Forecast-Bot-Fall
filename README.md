# Pirohuni Forecast Bot Fall

It fetches open tournament questions, asks an LLM for forecasts, aggregates repeated runs, saves the LLM outputs locally, and optionally submits forecasts plus private rationale comments to Metaculus.

## Current project structure

```text
forecasting_bot.py          - CLI entry point, arg parsing, env validation
orchestrator.py             - per-question flow: research -> forecast -> artifacts -> submit
config.py                   - env vars, constants, tournament aliases, ensemble pool
llm_provider.py             - the ONLY place OpenRouter and OpenAI differ (urls, models,
                              request params, pricing). Switch with LLM_PROVIDER
llm_client.py               - shared async LLM client, retries, transcripts
monetary_cost_manager.py    - per-call token/cost ledger and hard-limit enforcement
metaculus_client.py         - Metaculus API: list posts, fetch details, submit, comment

research/pipeline.py        - the six-stage research pipeline (see Research below)
research/asknews_research.py / asknews_filter.py - news retrieval, then relevance filter
research/serp_research.py   - SerpAPI search + the shared scrape/extract cycle engine
research/tavily_research.py / firecrawl_research.py / firecrawl_scrape.py
research/kalshi_research.py / manifold_research.py / polymarket_research.py
research/evidence_plan.py   - decides what evidence the question actually needs
query_maker.py              - search-query generation
resolution_criteria_scraper.py - scrapes URLs found in the question's own text
series_gate.py              - deterministic parser: is this payload a historical series?
series_discovery.py         - free ladder to find the data behind a JS-rendered page
series_reduce.py            - reduce a retrieved series to a ~2KB forecast-ready table
compiler.py                 - distills all provider output into one evidence brief

forecasters/base.py         - ensemble runner, repair retry, heterogeneous run
forecasters/binary.py       - binary prompt, parser, median + tiebreaker
forecasters/numeric.py      - numeric/discrete: mixtures, guardrails, CDF standardisation
forecasters/multiple_choice.py - multiple-choice prompt, parser, aggregation

Adapters/                   - per-host extractors (Metaculus, Wikipedia, PDF, Google
                              Sheets, Google Trends, Yahoo Quotes, Wayback)
Crawl4AI/crawl.py           - headless-browser markdown fallback + scrape dedupe registry
artifacts.py                - per-question run folder writer
source_ledger.py            - URL event ledger behind audit.md
research_trace.py           - step-by-step research trace behind evolution.md
run_logging.py / utils.py   - logging setup, shared helpers

eval_tools/                 - offline dry-run, replay, compile-replay, scoring
prompts/                    - every prompt template, extracted verbatim from source
```

## Runtime flow

1. Parse CLI args.
2. Build a list of `(question_id, post_id)` pairs from either example questions or tournament questions.
3. For each question:
   - Fetch post details from Metaculus.
   - Skip if `SKIP_PREVIOUSLY_FORECASTED_QUESTIONS` is enabled and a prior forecast exists.
   - Dispatch to the forecaster for the question type.
   - Run the LLM `--num-runs` times.
   - Run the research pipeline, then compile it into one evidence brief.
   - Run the forecast `--num-runs` times across the model ensemble. With the
     heterogeneous run enabled, the **last** run reads the raw (uncompiled)
     research instead of the brief, so one ensemble member can still catch
     evidence the compile step dropped.
   - Aggregate the runs:
     - binary: median probability - unless the runs disagree by 30 points or
       more, which triggers one tiebreaker call that sees every rationale
     - numeric/discrete: PMF-space mean for count questions, quantile
       (horizontal) averaging otherwise, then Metaculus CDF standardisation
     - multiple choice: mean probability per option
   - Write the run folder (see Artifacts below).
   - Submit forecast and private comment unless `--no-submit` is set.

A forecaster may **abstain**: if no numeric run survives the guardrails the
forecast is `None` and nothing is submitted. Binary and multiple choice instead
fall back to 0.5 / uniform, which *are* submitted.

Any per-question failure is reported and then causes the process to exit with a nonzero status.

## Artifacts

Each question writes `docs/runs/<timestamp>/<question_id>_<slug>/`:

| File | Contents |
|---|---|
| `research.md` | evidence plan, every provider's raw output, the compiled brief |
| `runs.md` | the forecast prompt once, then each ensemble run's transcript |
| `audit.md` | per-call token/cost table plus a URL ledger with per-tool rollups |
| `forecast.json` | machine-readable record for scoring and replay |
| `evolution.md` + `trace/` | how the research changed, step by step |

The shared `docs/runs/<timestamp>/run.log` holds the whole run. `docs/` is
gitignored; the GitHub Actions workflows upload it as a build artifact instead.

## Monetary Cost Manager

The bot tracks LLM usage by character count instead of trusting response
billing fields. Before each call it records the task name, model, and
serialized input character count; after the response it records output
characters. Tokens are estimated with `CHARACTERS_PER_TOKEN = 3.2`
(`monetary_cost_manager.py`), and every budget constant in that file is
denominated in those units.

Under `LLM_PROVIDER=openai` there is no per-call cost field in the response, so
dollar figures are computed from the local price table in `llm_provider.PRICES`.
Keep that table in sync with OpenAI's published pricing.

On OpenRouter the bot also checks the active key's remaining spend before and
after each run through `GET https://openrouter.ai/api/v1/key` (the OpenAI API
has no per-key credit endpoint). The important field for this is
`data.limit_remaining`, because it reflects the current API key's remaining
credit limit. This is useful as a billing-side comparison only; local run logs
use the character/token ledger.

`monetary_cost_manager.py` provides `MonetaryCostManager`, which exposes:

| Property | Meaning |
|---|---|
| `current_usage` | Backward-compatible numeric usage value: total estimated tokens |
| `total_input_characters` / `total_output_characters` | Raw character totals |
| `total_input_tokens` / `total_output_tokens` | Token estimates using 3.2 chars/token |
| `format_usage_yaml_table()` | YAML-compatible log block containing the requested usage table |

The orchestrator wraps each forecast in a per-question `MonetaryCostManager()`
and wraps the whole run in a parent manager used only as a pooled ledger.
Forecast summaries include per-question token usage, and the final run summary
includes a YAML-compatible table with columns for no., task name, input/output
characters, input/output tokens, and model used.

A hard limit can be set with either `OPENROUTER_COST_HARD_LIMIT_USD` or the
`--token-limit` / `--cost-limit` CLI flag. The old env var name is kept for
compatibility, but the value is now interpreted as estimated tokens per
question. A value of `0` disables enforcement while still tracking usage.

## Research

Research is `research/pipeline.py:run_research()`, in six stages:

1. **AskNews + resolution sources.** The resolution-source scraper starts as a
   parallel task (it is the slowest provider and needs only the question text).
   AskNews is awaited inline.
2. **1.5 - AskNews filter.** Cuts the AskNews block down to what bears on the
   question, by relevance rather than by position, before anything reads it.
3. **Evidence plan.** Decides what evidence the question actually needs and
   names the artifact that would settle it.
4. **Providers.** Kalshi / Manifold / Polymarket run in parallel. The three web
   search providers run as a **fallback chain** - SerpAPI, then Tavily, then
   Firecrawl - stopping at the first usable result, so enabling all three
   conserves credits rather than spending them in parallel.
5. **Artifact check**, then a focused retry aimed only at what is missing, then
   a re-check if that retry added anything.
6. **Compile.** `compiler.py` distills everything into one evidence brief, then
   two deterministic banners are applied that the compiler cannot soften.

Scraping any URL walks a ladder: a per-host adapter if one claims it, then
Firecrawl `/v2/scrape`, then the local Crawl4AI headless browser, then a Wayback
snapshot. Every outcome is recorded in `audit.md` and in the trace.

For numeric and discrete questions the primary resolution URL additionally goes
through a **free** series ladder (`series_discovery.py`): the page as served,
JSON embedded in inline scripts, iframes one level deep, then data endpoints
regexed out of the page's own JS bundles. Anything found is reduced at the point
of retrieval (`series_reduce.py`) to a ~2 KB table of levels, period means and
empirical ahead-ratio quantiles. No LLM calls, no Firecrawl credits, no browser.

## Setup

```bash
poetry install
cp .env.example .env
```

Required environment variables:

| Variable | Required | Purpose |
|---|---:|---|
| `METACULUS_TOKEN` | Yes | Fetch authenticated question details and submit forecasts |
| `OPENROUTER_API_KEY` **or** `OPENAI_API_KEY` | Yes | Whichever `LLM_PROVIDER` selects; only that one is required |
| `ASKNEWS_CLIENT_ID` + `ASKNEWS_SECRET` | Yes | AskNews OAuth credentials for research |

Instead of `ASKNEWS_CLIENT_ID` + `ASKNEWS_SECRET`, you can set `ASKNEWS_API_KEY`. Do not set both authentication methods at the same time.

Optional environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `METACULUS_MAX_CONCURRENT_REQUESTS` | `1` | Semaphore limit for Metaculus API calls |
| `METACULUS_REQUEST_INTERVAL` | `3.0` | Delay before each Metaculus request |
| `INITIAL_API_GET_RETRY_WAIT_SECONDS` | `3.0` | Initial retry delay |
| `ASKNEWS_CACHE_MODE` | `no_cache` | AskNews cache behavior: `use_cache`, `use_cache_with_fallback`, or `no_cache` |
| `OPENROUTER_COST_HARD_LIMIT_USD` | `0` | Optional per-question estimated-**token** hard limit; the old USD name is kept for compatibility |
| `LLM_PROVIDER` | `openrouter` | `openrouter` or `openai`. Also swaps the model pool, drops `temperature`, and prices calls locally - see `llm_provider.py` |
| `OPENAI_REASONING_EFFORT[_<MODEL>]` | per-model | OpenAI only. Defaults: `high` on the forecaster/compiler/tiebreaker model, `low` on research utilities |
| `OPENAI_PROMPT_CACHE` | `explicit` | OpenAI only. `explicit` means no cache writes, avoiding a 1.25x premium on prompts that are unique per question |
| `FORECASTER_MODELS` | provider-dependent | Comma-separated ensemble pool, mapped onto runs in order |
| `HETEROGENEOUS_RUN_ENABLED` / `HETEROGENEOUS_RUN_MODEL` | `true` | Whether the last run reads raw research, and on which model |
| `ENABLE_*_RESEARCH` | `true` | Per-provider toggles; see `.env.example` for the full list |
| `QUESTION_TIMEOUT_SECONDS` | `1200` | Per-question wall clock |

Never commit `.env`.

## Usage

Dry run with examples:

```bash
poetry run python forecasting_bot.py --mode examples --no-submit
```

Dry run on the default tournament:

```bash
poetry run python forecasting_bot.py --mode tournament --no-submit
```

Dry run on specific tournaments:

```bash
poetry run python forecasting_bot.py --mode tournament --tournament fall-2026-ai minibench --no-submit
```

Submit forecasts:

```bash
poetry run python forecasting_bot.py --mode tournament --tournament fall-2026-ai
```

Use fewer runs while debugging:

```bash
poetry run python forecasting_bot.py --mode tournament --tournament fall-2026-ai --num-runs 1 --no-submit
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--mode` | `tournament` | `tournament` or `examples` |
| `--tournament` | `fall-2026-ai` (Fall FutureEval 2026) | One or more tournament aliases or raw integer IDs |
| `--no-submit` | off | Dry run; no forecasts or comments are posted |
| `--num-runs` | `3` | Number of LLM runs per question; must be at least 1 |
| `--token-limit`, `--cost-limit` | `OPENROUTER_COST_HARD_LIMIT_USD` | Optional OpenRouter estimated-token hard limit per question; `0` tracks only |

## Tournament aliases

| Alias | Tournament |
|---|---|
| `fall-2026-ai` | **Fall FutureEval 2026 — current default** (project 33121) |
| `metaculus-cup` | Metaculus Cup Fall 2026 |
| `minibench` | MiniBench |
| `spring-2026-ai` | Spring 2026 AI Benchmarking |
| `summer-2026-ai` | Summer FutureEval 2026 |
| `summer-2026-cup` | Metaculus Cup Summer 2026 |
| `fall-2025-ai` | Fall 2025 AI Benchmarking |
| `q1-2025-ai` | Q1 2025 AI Benchmarking |
| `q4-2024-ai` | Q4 2024 AI Benchmarking |
| `q1-2025-cup` | Q1 2025 Quarterly Cup |
| `q4-2024-cup` | Q4 2024 Quarterly Cup |
| `axc-2025` | AXC 2025 |
| `ai-2027` | AI 2027 |

## Before submitting for real

1. Run with `--no-submit`.
2. Inspect the generated run folder under `docs/runs/`.
3. Confirm `METACULUS_TOKEN`, the API key for your `LLM_PROVIDER`, and AskNews credentials are set.
4. Use `--num-runs 1` while debugging to reduce cost.
