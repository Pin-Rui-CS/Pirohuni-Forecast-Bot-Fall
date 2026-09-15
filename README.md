# Pirohuni Forecast Bot Fall

It fetches open tournament questions, researches each one, asks an ensemble of LLMs for forecasts, aggregates the runs, saves every intermediate output locally, and optionally submits forecasts plus private rationale comments to Metaculus.

## Current project structure

```text
forecasting_bot.py          - CLI entry point, arg parsing, startup validation
orchestrator.py             - per-question flow: research -> forecast -> artifacts -> submit
config.py                   - env vars, constants, tournament aliases, ensemble pool
llm_provider.py             - every endpoint the bot can reach (OpenRouter, OpenAI, SoCLaaS):
                              the route table, request shaping, rate gates, pricing, preflight
llm_client.py               - shared async LLM client, retries, truncation detection, transcripts
monetary_cost_manager.py    - per-call token / cost / quota ledger and hard-limit enforcement
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

apiagent_bridge.py          - Python access to apiagent-kit's 24 public data APIs
apiagent-kit/               - TypeScript library (Node >= 22.6) wrapping those APIs;
                              cli.ts is the JSON shim the bridge drives

Adapters/                   - per-host extractors (Metaculus, Wikipedia, PDF, Google
                              Sheets, Google Trends, Yahoo Quotes, Wayback)
Crawl4AI/crawl.py           - headless-browser markdown fallback + scrape dedupe registry
artifacts.py                - per-question run folder writer
provenance.py               - stamps each forecast.json with run id, commit, models, schema version
source_ledger.py            - URL event ledger behind audit.md
research_trace.py           - step-by-step research trace behind evolution.md
run_logging.py / utils.py   - logging setup, shared helpers

tests/                      - offline tests: request shaping, library publishing, outcome scoring
eval_tools/                 - offline dry-run, replay, compile-replay, scoring
eval_tools/publish_runs.py / score_outcomes.py / supabase_rest.py - the forecast library
db/migrations/              - the forecast library's database schema
prompts/                    - every prompt template, extracted verbatim from source
SOCLAAS.md                  - reference for the NUS SoC LLM gateway
```

## Runtime flow

1. Parse CLI args and validate the configuration. `llm_provider.preflight()`
   fails the run before the first question if any model has no route, any
   endpoint it can reach is missing its key, or a fallback points back at the
   endpoint it is meant to replace.
2. Build a list of `(question_id, post_id)` pairs from either example questions or tournament questions.
3. For each question:
   - Fetch post details from Metaculus.
   - Skip if `SKIP_PREVIOUSLY_FORECASTED_QUESTIONS` is enabled and a prior forecast exists.
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

## Choosing models

Every LLM call names a **role**, not a vendor: `anthropic/claude-opus-5` means
"the strong model" (compiler, forecaster, tiebreaker) and
`anthropic/claude-sonnet-5` means "the utility model" (every research call).
Two variables decide which concrete model fills each role:

```bash
TIER1_MODEL=openai/gpt-6-astra      # compiler, forecaster, tiebreaker
TIER2_MODEL=qwen3.8:27b             # every research and utility call
```

The endpoint follows the model: an id in the SoC gateway's `family:size` form
goes to SoCLaaS, anything else to OpenRouter. Setting `TIER2_MODEL` also sets
the ensemble to one model of each tier, with the raw-research run on the Tier 2
model, so the pairing above forecasts as:

| Run | Model | Endpoint | Reads |
|---|---|---|---|
| 1 | `openai/gpt-6-astra` | OpenRouter | compiled brief |
| 2 | `qwen3.8:27b` | SoCLaaS | compiled brief |
| 3 | `qwen3.8:27b` | SoCLaaS | raw research |

The ensemble only takes that shape at `--num-runs 3`; with one run, only the
Tier 1 model forecasts.

Leave both unset and `LLM_ROUTING` picks a preset instead: `openrouter`
(default: Opus 5 and GPT-5.6 Sol on OpenRouter) or `openai` (GPT-5.6 on the
direct OpenAI API). `LLM_PROVIDER` is still accepted as the old name for
`LLM_ROUTING`. `FORECASTER_MODELS` and `HETEROGENEOUS_RUN_MODEL` override the
ensemble when you want something unusual.

What is **not** configurable is how a request is shaped. Whether a model
accepts `temperature`, needs extra room for hidden reasoning under
`max_tokens`, understands `cache_control` breakpoints, or reports its own cost
are facts about a model and endpoint, so they live in `llm_provider.py` rather
than in config. Getting one wrong costs money or returns an empty completion.

### SoCLaaS

The NUS SoC LLM gateway serves open-weight models (including `qwen3.8:27b`)
with no charge in money, but it is metered and rate-limited. Details are in
`SOCLAAS.md`; the parts that shape this bot:

- The bot spaces requests to **24 per minute** against a measured limit of 30,
  process-wide. Tier 2 makes most of a question's calls, so this can add real
  wall-clock time to a run.
- Usage counts against a daily and monthly allowance, recorded in `audit.md` as
  `quota ud` (microdollars, not money).
- There is **no automatic fallback yet**. Each SoCLaaS route declares a paid
  OpenRouter route to fall back to, and startup checks that its key is set,
  but nothing switches to it at runtime. If the gateway is down, Tier 2 calls
  fail after their retries: research providers degrade, the compiler uses its
  heuristic brief, and Qwen forecast runs drop out of the ensemble.
- The API host is reachable from off-campus, including GitHub Actions. The
  portal's budget endpoint is not, so the bot does not poll it.

## Artifacts

Each question writes `docs/runs/<timestamp>/<question_id>_<slug>/`:

| File | Contents |
|---|---|
| `research.md` | evidence plan, every provider's raw output, the compiled brief |
| `runs.md` | the forecast prompt once, then each ensemble run's transcript |
| `audit.md` | per-call token / cost / quota table plus a URL ledger with per-tool rollups |
| `forecast.json` | machine-readable record for scoring and replay |
| `evolution.md` + `trace/` | how the research changed, step by step |

The shared `docs/runs/<timestamp>/run.log` holds the whole run. `docs/` is
gitignored; the GitHub Actions workflows upload it as a build artifact instead.

## Cost and usage tracking

`MonetaryCostManager` records every LLM call. Before the call it estimates input
tokens from the serialized request (`CHARACTERS_PER_TOKEN = 3.2`; every budget
constant in `monetary_cost_manager.py` is in those units) and refuses the call
if it would breach the per-question hard limit. After the call it records what
the endpoint reported.

How cost is worked out depends on the route that served the call:

| `cost_source` | Used by | Cost figure |
|---|---|---|
| `native` | OpenRouter | the cost the response reports |
| `price_table` | OpenAI direct | computed from `llm_provider.PRICES` (keep it in sync with OpenAI's pricing) |
| `quota` | SoCLaaS | `$0`, with usage recorded separately as quota microdollars |

`audit.md`'s table carries `endpoint`, `cost usd` and `quota ud` columns for
every call, so a `$0.000000` row can be told apart as a free route, a model
missing from the price table, or a call that never billed. The summary also
reports `total_paid_input_tokens` (excluding free routes),
`total_quota_microdollars` and `endpoints_used`.

On OpenRouter the bot also logs the key's remaining credit before and after each
run via `GET https://openrouter.ai/api/v1/key`, as a billing-side comparison.

A per-question hard limit can be set with `OPENROUTER_COST_HARD_LIMIT_USD` or
`--token-limit` / `--cost-limit`. Despite the name, the value is **estimated
tokens**; `0` tracks usage without enforcing a limit. Forecast runs and the
binary tiebreaker are also capped at `FORECAST_MAX_OUTPUT_TOKENS` (20000) of
visible output.

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

## apiagent-kit

`apiagent-kit/` wraps 24 public data APIs (FRED, World Bank, CISA KEV, FDIC,
USGS, Cboe VIX, Wikipedia pageviews, SEC EDGAR, Kalshi, Polymarket, Metaculus
and others), each tagged with a provenance tier. 22 need no key; `fred` needs
`FRED_API_KEY`, and `metaculus` reuses `METACULUS_TOKEN`.

It is TypeScript, so `apiagent_bridge.py` runs `apiagent-kit/cli.ts` under Node
for each call and reads JSON back. It never raises into the pipeline: a missing
Node install, a timeout or an API error comes back as a result with
`ok=False`.

```python
import apiagent_bridge as ab

await ab.find_apis("How many CVEs will CISA add in August?")   # -> cisa_kev, ... (offline, free)
await ab.call_api("cisa_kev", {"addedSince": "2026-08-01"})   # -> rows + an exact matched count
```

Results are capped at 25 rows; where it matters, the adapter's `note` carries
figures computed over the full range (exact counts, period high/low).

**Status:** the bridge works, but the research pipeline does not call it yet.

## Forecast library

Every run is also published to a private Supabase project, so past forecasts
outlive GitHub's 14-day artifact retention and can be browsed and analysed from
a separate website repo.

- **What is stored.** One row per question per run, one row per LLM call, and
  later an outcome and a score. The question's files (`forecast.json`, the
  markdown files gzipped, `trace/` as a tarball) go to a private storage bucket
  under `runs/<run_id>/<question_id>/`. Tournament submissions, dry runs and
  manual tests are all recorded, told apart by `workflow` and `submitted`.
- **Provenance.** Each `forecast.json` now records its run id, commit, workflow,
  the resolved models and a `schema_version` (`provenance.py`). That is what
  makes accuracy comparable across code versions; it cannot be added to old
  runs afterwards.
- **Changing `forecast.json`.** The database stores each file verbatim in a
  `raw` JSON column and promotes only stable fields to columns. Adding a field
  needs nothing. Renaming or restructuring one means bumping `SCHEMA_VERSION` in
  `provenance.py` and teaching `eval_tools/publish_runs.py` and the views in
  `db/migrations/` to read both shapes; stored rows are never rewritten. Records
  from before provenance existed are published as `schema_version` 1, with
  per-call costs recovered from the rendered usage table.
- **Publishing.** Every workflow runs `eval_tools/publish_runs.py` after the bot.
  It is idempotent and can never fail a forecast run: it does nothing without
  credentials, and a database error marks only that step.
- **Scoring.** `score-outcomes.yaml` runs daily, checks unresolved questions
  against Metaculus, and scores forecasts once they resolve (Brier for binary
  and multiple choice, CRPS for numeric) using the same code as
  `eval_tools/score_forecasts.py`.
- **Access.** Row-level security lets only a signed-in user read anything; the
  anon key alone reads nothing. `SUPABASE_SERVICE_KEY` has full write access and
  belongs only in GitHub secrets and your local `.env`.

Views ready for analysis: `forecast_library` (forecasts with outcomes and
scores), `accuracy_by_model`, `cost_by_week`.

### Setting it up

1. Create a Supabase project and note its URL and service-role (secret) key.
2. In the SQL editor, run `db/migrations/001_schema.sql`, then
   `002_supabase_security.sql`. Both are safe to re-run. The second creates the
   private `forecast-runs` bucket.
3. Under Authentication, create your own user, then turn off new sign-ups so
   that user is the only one who can read the library.
4. Add `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` as GitHub repository secrets
   (and to `.env` to publish local runs).
5. Dispatch **Backfill the forecast library from saved artifacts** once, to
   publish the result artifacts GitHub still holds before they expire.

To check a runs folder without writing anything:

```bash
poetry run python eval_tools/publish_runs.py docs/runs --dry-run
```

## Setup

```bash
poetry install
cp .env.example .env
```

For apiagent-kit, install Node 22.6 or later, then `cd apiagent-kit && npm ci`.

There is **one** `.env`, at the repository root. apiagent-kit inherits it from
the Python process, so don't create `apiagent-kit/.env`. `.env.example`
documents every variable the code reads; anything not listed below has a
working default.

| Variable | When needed | Purpose |
|---|---|---|
| `METACULUS_TOKEN` | always | Fetch question details and submit forecasts |
| `ASKNEWS_CLIENT_ID` + `ASKNEWS_SECRET` | always | AskNews OAuth credentials (or `ASKNEWS_API_KEY` instead, never both) |
| `OPENROUTER_API_KEY` | any model on OpenRouter (the default) | LLM calls |
| `OPENAI_API_KEY` | `LLM_ROUTING=openai` | LLM calls on the direct OpenAI API |
| `SOCLAAS_API_KEY` | a Tier model on the SoC gateway | LLM calls on SoCLaaS |
| `SERPAPI_API_KEY` / `TAVILY_API_KEY` / `FIRECRAWL_API_KEY` | recommended | Web search and scraping |
| `FRED_API_KEY` | optional | apiagent-kit's FRED adapter |
| `TIER1_MODEL` / `TIER2_MODEL` | optional | Which models fill each role (see Choosing models) |
| `LLM_ROUTING` | optional | Preset when the tier models are unset: `openrouter` (default) or `openai` |
| `OPENROUTER_COST_HARD_LIMIT_USD` | optional | Per-question estimated-token limit; `0` tracks only |
| `QUESTION_CONCURRENCY` | optional | Questions forecast in parallel; `0` (default) runs all at once |
| `ENABLE_*_RESEARCH` | optional | Per-provider toggles, all `true` by default |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | optional | Publish runs to the forecast library |

`preflight()` reports any key the chosen models need but can't find, so a
missing credential fails at startup rather than partway through a question.

Never commit `.env`.

### GitHub Actions

Workflows read credentials from **repository secrets** and settings from
**repository variables** (Settings → Secrets and variables → Actions). Each
workflow lists the names it passes through, so a secret or variable that no
workflow references never reaches the bot.

- **Secrets:** `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_CLIENT_ID`,
  `ASKNEWS_SECRET`, `SERPAPI_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`,
  `SOCLAAS_API_KEY`, and for the forecast library `SUPABASE_URL` and
  `SUPABASE_SERVICE_KEY`.
- **Variables:** `LLM_ROUTING`, `TIER1_MODEL`, `TIER2_MODEL`, and optionally
  `OPENROUTER_COST_HARD_LIMIT_USD` and the `ENABLE_*_RESEARCH` toggles.

Repository variables apply to **every** workflow, including the scheduled runs
in `main.yaml` and `main_ec2.yml` (at :04, :24 and :44 past each hour). Setting
`TIER1_MODEL` / `TIER2_MODEL` therefore switches the tournament runs too, not
just a manual test.

The workflows do not install Node yet, so apiagent-kit does not run in CI.

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

Dry run with Astra and Qwen:

```bash
TIER1_MODEL=openai/gpt-6-astra TIER2_MODEL=qwen3.8:27b \
  poetry run python forecasting_bot.py --mode tournament --no-submit
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
| `--num-runs` | `3` | Number of forecast runs per question; must be at least 1 |
| `--token-limit`, `--cost-limit` | `OPENROUTER_COST_HARD_LIMIT_USD` | Optional estimated-token hard limit per question; `0` tracks only |

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

## Testing

```bash
python tests/test_route_equivalence.py
python tests/test_publish_runs.py
python tests/test_score_outcomes.py
```

`test_route_equivalence.py` checks that request shaping for the `openrouter`
and `openai` presets still matches 550 saved request snapshots. Run it after any
change to `llm_provider.py`; regenerate the snapshots (`--regenerate`) only when
a change to the requests is intended.

The other two exercise the forecast library offline, with no network or
database: what the publisher sends for current and legacy records, and that
daily scoring neither skips nor repeats forecasts.

## Before submitting for real

1. Run with `--no-submit`.
2. Inspect the generated run folder under `docs/runs/`, including `audit.md` for cost.
3. Confirm the startup check passes: `METACULUS_TOKEN`, AskNews credentials, and a key for every endpoint your chosen models use.
4. Use `--num-runs 1` while debugging to reduce cost (keeping in mind a single run skips the ensemble).
