# CAPABILITIES.md

> **STALENESS NOTICE — read before trusting anything below.**
>
> This document was derived from the tree at commit `b1d9743` (2026-07-29) and has not
> been regenerated since. It is still useful as a map, but it is known-wrong in these ways:
>
> | Area | Status |
> |---|---|
> | **Line references** (`file.py:123`) | **Do not trust.** They were already drifting at the commit above and several files have changed size since. Search by symbol name instead. |
> | **§5 Dead or unreachable code** | **Largely obsolete — the code it lists was deleted.** The LLM tool-use loop, the legacy resolution scraper, the unused `query_maker` entry points, `scrape_metaculus`, and the `*_research_to_dict` serializers are gone. |
> | **Provider layer** | Predates `llm_provider.py`. Every LLM call now routes through it, and `LLM_PROVIDER=openai` remaps models, drops `temperature`, and prices calls locally. Model names in §3 are the OpenRouter side only. |
> | **`research/asknews_filter.py`** | Not covered. It is now stage 1.5 of the pipeline. |
> | **`series_gate.py` / `series_discovery.py` / `series_reduce.py`** | Not covered, despite being added in the same commit. See INCIDENTS.md 2026-07-29. |
> | **`prompts/`** | Not covered. Added later; note its README mis-files "Resolution Source Compile" as active — that prompt was only reachable through the deleted legacy path. |
> | **§6.11 truncation** | Partly fixed: the artifact check now uses head/tail truncation via `fit_artifact_check_sections`. |
> | **§6.1 search-chain order** | Fixed. The `config.py` and `.env.example` comments now match the code (`SerpAPI -> Tavily -> Firecrawl`). |
> | **Tournament** | Migrated to Fall FutureEval 2026 in `5992fa6`. |
>
> `§6` (duplication) was re-verified and still holds, apart from the rows about deleted code.

What this project **actually does right now**, derived by reading the code only. README.md,
INCIDENTS.md, comments, and docstrings were treated as untrusted; where they disagree with the
code, the disagreement is recorded in §6.

Scope of the read: every `.py` file outside `.venv/`, plus `.github/workflows/*`, `.env`,
`.env.example`, `pyproject.toml`, `.gitignore`.

Repo-state note that affects everything below: `.gitignore` excludes `feedback_loop/`, `tests/`,
`docs/`, and `.claude/`. Those directories **exist on disk and are importable/runnable**, but are
not committed. The document covers what is on disk.

---

## 1. Entry points

Every way execution can start.

### 1.1 CLI entry points (`__main__` guards)

| Entry point | Trigger | Handler |
|---|---|---|
| `forecasting_bot.py` | `poetry run python forecasting_bot.py [--mode …]` | [forecasting_bot.py:150-196](forecasting_bot.py#L150-L196) → `asyncio.run(forecast_questions(...))` |
| `feedback_loop/run_bot.py` | `poetry run python feedback_loop/run_bot.py [--mode …]` | [feedback_loop/run_bot.py:33](feedback_loop/run_bot.py#L33) `main()` → `forecast_questions_feedback(...)` |
| `feedback_loop/dry_run_one.py` | `poetry run python feedback_loop/dry_run_one.py --question-id N --post-id M` | [feedback_loop/dry_run_one.py:24](feedback_loop/dry_run_one.py#L24) `main()`; `submit_prediction=False` hard-coded |
| `eval_tools/dry_run.py` | `poetry run python eval_tools/dry_run.py [tournament] [--runs N]` | [eval_tools/dry_run.py:29](eval_tools/dry_run.py#L29) `main()` |
| `eval_tools/replay.py` | `poetry run python eval_tools/replay.py <question_dir> [--runs N] [--save]` | [eval_tools/replay.py:47](eval_tools/replay.py#L47) `main()` |
| `eval_tools/compile_replay.py` | `poetry run python eval_tools/compile_replay.py <question_dir> [--save]` | [eval_tools/compile_replay.py:90](eval_tools/compile_replay.py#L90) `main()` |
| `eval_tools/score_forecasts.py` | `poetry run python eval_tools/score_forecasts.py [roots…]` | [eval_tools/score_forecasts.py:178-185](eval_tools/score_forecasts.py#L178-L185) |
| `Crawl4AI/crawl.py` | `python Crawl4AI/crawl.py <url> [--output f] [--timeout n] [--print]` | [Crawl4AI/crawl.py:228](Crawl4AI/crawl.py#L228) `main()` |
| `research/kalshi_research.py` | `python research/kalshi_research.py "<question>"` | [research/kalshi_research.py:486-488](research/kalshi_research.py#L486-L488) |

`forecasting_bot.py`'s flags: `--mode {tournament,examples}` (default `tournament`),
`--tournament` (nargs `+`), `--no-submit`, `--num-runs`, `--cost-limit`/`--token-limit`
([forecasting_bot.py:33-88](forecasting_bot.py#L33-L88)). There is **no** `--skip-previously-forecasted`
flag; the CLI always passes the module constant (see §4).

`feedback_loop/run_bot.py` imports `parse_arguments` from `forecasting_bot`, so it accepts the
identical flag set ([feedback_loop/run_bot.py:22-26](feedback_loop/run_bot.py#L22-L26)).

### 1.2 Scheduled jobs (GitHub Actions cron)

| Workflow | Schedule | What it runs |
|---|---|---|
| `.github/workflows/main.yaml` | `4,24,44 * * * *` (3×/hour) + `workflow_dispatch` | `poetry run python forecasting_bot.py --mode tournament --tournament fall-2026-ai` (`.github/workflows/main.yaml`, "Run forecasting bot" step) |
| `.github/workflows/main_ec2.yml` | `4,24,44 * * * *` + `workflow_dispatch`, on `[self-hosted, linux, x64, ec2-bot]` | `forecasting_bot.py --mode tournament --tournament fall-2026-ai minibench` |

Both use `concurrency.group: ${{ github.workflow }}` with `cancel-in-progress: false`, so runs of
the *same* workflow queue rather than overlap — but `main.yaml` and `main_ec2.yml` are separate
groups and **both fire on the same cron**, so they can run concurrently against the same Metaculus
account (see §6).

### 1.3 Manual dispatch workflows

| Workflow | Inputs | How it starts the bot |
|---|---|---|
| `external-cron-forecast.yaml` | `tournament`, `num_runs`, `cost_limit`, `no_submit` | Builds an argv array and shells out to `forecasting_bot.py` (lines 114-137) |
| `external-cron-metaculus-cup-test.yaml` | `num_runs`, `cost_limit`, `no_submit`, `skip_previously_forecasted` | Heredoc-inlined Python that imports `orchestrator.forecast_questions` directly (line 120 onward, call at ~line 210) |
| `test-one-metaculus-cup-question.yaml` | `question_id`, `post_id`, `submit`, `num_runs`, `skip_previously_forecasted`, `max_questions`, `question_type` | Heredoc-inlined Python importing `orchestrator.forecast_questions` (line 93 onward, call at ~line 239) |

The two inline-Python workflows bypass `forecasting_bot.py` entirely: they select questions
themselves (using `orchestrator.forecast_is_already_made` + `metaculus_client.get_post_details`)
and call `forecast_questions(...)` with their own `skip_previously_forecasted` value. This is why
`forecast_questions` re-initializes logging itself ([orchestrator.py:331-338](orchestrator.py#L331-L338)).

### 1.4 Not present

No HTTP routes, no webhook receivers, no message/chat handlers, no long-running server. The only
inbound network surface is nothing — the bot is purely an outbound client.

---

## 2. Capabilities

### 2.1 Select questions to forecast

**Trigger:** `--mode tournament` (default) on any CLI entry point.

**Steps:**
1. `get_tournament_ids` maps aliases → IDs via `TOURNAMENT_MAPPING`, else parses an int, else raises
   ([forecasting_bot.py:125-147](forecasting_bot.py#L125-L147), [config.py:139-151](config.py#L139-L151)).
2. For each tournament, `get_open_question_ids_from_tournament` calls `list_posts_from_tournament`
   ([metaculus_client.py:297](metaculus_client.py#L297), [metaculus_client.py:273](metaculus_client.py#L273)).
3. `list_posts_from_tournament` GETs `{API_BASE_URL}/posts/` with `limit=50, offset=0,
   order_by=-hotness, forecast_type=binary,multiple_choice,numeric,discrete, statuses=open`
   ([metaculus_client.py:276-293](metaculus_client.py#L276-L293)). **No pagination loop** — one page, max 50 posts.
4. Only posts whose `question.status == "open"` are kept; `(question_id, post_id)` pairs are
   deduped by `question_id` across tournaments ([forecasting_bot.py:166-172](forecasting_bot.py#L166-L172)).

**Reads:** Metaculus REST API. **Writes:** stdout only. **Returns:** `list[tuple[int, int]]`.

`--mode examples` instead uses the hardcoded `EXAMPLE_QUESTIONS` list ([config.py:153-158](config.py#L153-L158)).

### 2.2 Forecast a batch of questions (main architecture)

**Trigger:** `orchestrator.forecast_questions(...)` ([orchestrator.py:324](orchestrator.py#L324)).

**Steps:**
1. `setup_run_logging(run_log_file_path())` — idempotent; console INFO, file DEBUG for project
   loggers only ([run_logging.py:60-92](run_logging.py#L60-L92)).
2. Log OpenRouter key balance (`GET https://openrouter.ai/api/v1/key`)
   ([orchestrator.py:51-78](orchestrator.py#L51-L78), [monetary_cost_manager.py:592](monetary_cost_manager.py#L592)).
3. Open a run-level `MonetaryCostManager` with **both token hard limits set to 0 (= disabled)**
   ([orchestrator.py:342-345](orchestrator.py#L342-L345)) — it is a pure accumulator.
4. `asyncio.gather` **all** questions concurrently with `return_exceptions=True`
   ([orchestrator.py:346-357](orchestrator.py#L346-L357)). There is no per-batch concurrency cap;
   throttling comes only from `llm_rate_limiter` (5) and `METACULUS_API_RATE_LIMITER` (1).
5. Log totals, per-question summaries, and a second key-balance reading.
6. If any question raised, log every traceback and **`raise RuntimeError`** → non-zero exit
   ([orchestrator.py:405-409](orchestrator.py#L405-L409)).

### 2.3 Forecast one question (main architecture)

**Trigger:** `forecast_individual_question_with_timeout` wraps
`forecast_individual_question` in `asyncio.wait_for(timeout=QUESTION_TIMEOUT_SECONDS)` (default
20 min) ([orchestrator.py:296-321](orchestrator.py#L296-L321)).

**Steps** ([orchestrator.py:105-293](orchestrator.py#L105-L293)):
1. Set the per-question ContextVar scopes: `set_scrape_dedupe_scope(f"question:{qid}:{pid}")` and
   `source_ledger.set_source_scope(same)` (lines 116-118).
2. `get_post_details(post_id)` (GET `/posts/{id}/`).
3. Skip if `question.status != "open"` (line 137) or if a forecast already exists **and**
   `skip_previously_forecasted_questions` (line 145). `forecast_is_already_made` checks
   `question.my_forecasts.latest.forecast_values is not None` ([orchestrator.py:81-94](orchestrator.py#L81-L94)).
4. Create `QuestionArtifacts` (makes `docs/runs/<ts>/<qid>_<slug>/`) and
   `research_trace.begin_question(dir)` (lines 149-150).
5. Open the per-question `MonetaryCostManager(hard_limit=<CLI --cost-limit>,
   reserved_input_tokens=research_reserve_input_tokens(num_runs))` (lines 156-159). Input/output
   limits take the defaults 375 000 / 62 500.
6. `run_research(...)` → `ResearchBundle` (§2.4).
7. `artifacts.save_research(...)` → `research.md`.
8. Dispatch by `question.type`: `binary` / `numeric`|`discrete` / `multiple_choice`; anything else
   raises `ValueError` (lines 175-197). Each forecaster receives **both** the compiled brief and
   `research_bundle.raw_research_view`.
9. Compute timings + `format_usage_yaml_table()`; exit the cost manager.
10. If fewer runs completed than requested, append an `ENSEMBLE DEGRADED:` line to the summary and
    log a warning (lines 234-241).
11. `create_forecast_payload(result.forecast, question_type)` (line 254) — note this runs even
    when abstaining, producing a payload with `None`.
12. Write `runs.md`, `audit.md` (draining the source ledger), `evolution.md` +`trace/`
    (`research_trace.finalize()`), and `forecast.json` (lines 256-280).
13. If `result.forecast is None` → abstain, submit nothing. Else if `submit_prediction`, POST the
    forecast then POST a **private** comment (lines 282-291).

**Writes per question:** `docs/runs/<timestamp>/<qid>_<slug>/{research.md, runs.md, audit.md,
forecast.json, evolution.md, trace/*}` and the shared `docs/runs/<timestamp>/run.log`
([artifacts.py:14-20](artifacts.py#L14-L20), [artifacts.py:41-42](artifacts.py#L41-L42)).

**Returns:** a multi-line human-readable summary string.

### 2.4 Research pipeline (`research/pipeline.py:65` `run_research`)

Six stages. All provider calls go through `run_provider`, which swallows every exception except
`HardLimitExceededError` and converts it into the string
`"<name> research unavailable: <Type>: <msg>"` ([research/pipeline.py:78-98](research/pipeline.py#L78-L98)).
A result containing `"research unavailable"` (or the market "no results" markers) is filtered out by
`should_include_provider_result` ([research/pipeline.py:100-115](research/pipeline.py#L100-L115)).

**Stage 1 (parallel start):** resolution-source scraper task created immediately (if
`ENABLE_RESOLUTION_SOURCE_RESEARCH`); AskNews awaited inline ([research/pipeline.py:190-211](research/pipeline.py#L190-L211)).

**Stage 2:** `build_evidence_plan` — one Sonnet call returning JSON, rendered to Markdown
([research/evidence_plan.py:15-60](research/evidence_plan.py#L15-L60)). On failure it emits a
hardcoded fallback plan ([research/evidence_plan.py:183](research/evidence_plan.py#L183)) — never raises.

**Stage 3:** Kalshi/Manifold/Polymarket launched as tasks (`asyncio.to_thread`); web-search
providers run as a **sequential fallback chain**, order `SerpAPI Google → Tavily Search →
Firecrawl Search` ([research/pipeline.py:254-267](research/pipeline.py#L254-L267),
[research/pipeline.py:575-617](research/pipeline.py#L575-L617)). The chain stops at the first
usable result. Two skip conditions: the provider is in the process-global
`_exhausted_search_providers` set, or `MonetaryCostManager.would_breach_input_reserve(62_500)`
returns True.

**Stage 4:** `verify_required_artifact` — one Sonnet call judging whether the evidence plan's
required artifact was actually retrieved; returns `{status, what_was_found, what_is_missing,
closest_available, forecast_swing, retry_queries}` ([research/pipeline.py:894-948](research/pipeline.py#L894-L948)).
The provider text handed to it is **head-truncated at 8 000 chars per provider**
([research/pipeline.py:903](research/pipeline.py#L903)) and the whole excerpt at 40 000
([research/pipeline.py:28](research/pipeline.py#L28)). The verdict then passes through a
deterministic future-date gate ([research/pipeline.py:856-891](research/pipeline.py#L856-L891)).

**Stage 5 (focused retry):** fires only when `status ∈ {missing, partial}` **and** `retry_queries`
is non-empty **and** a retry-capable provider exists ([research/pipeline.py:354-467](research/pipeline.py#L354-L467)).
Provider preference: reuse whichever search provider already succeeded; else the first
non-exhausted one ([research/pipeline.py:620-638](research/pipeline.py#L620-L638)). Capped at
4 queries, 1 scrape cycle, and `ARTIFACT_RETRY_TIMEOUT_SECONDS` (150 s).

**Stage 5.5:** if the retry added a result, re-run `verify_required_artifact` with `prior_check`
so the stale "missing" verdict is reconciled ([research/pipeline.py:474-490](research/pipeline.py#L474-L490)).

**Stage 6:** `compile_research_report` (§2.8), then two **deterministic post-compile injections**
that the compiler LLM cannot soften: `_apply_artifact_status_banner`
([research/pipeline.py:711-780](research/pipeline.py#L711-L780)) and `_apply_degradation_warning`
([research/pipeline.py:667-682](research/pipeline.py#L667-L682)). Finally
`_build_raw_research_view` joins the included provider sections (hard cap 200 000 chars, with an
in-band truncation notice) ([research/pipeline.py:692-708](research/pipeline.py#L692-L708)).

**Returns:** `ResearchBundle(evidence_plan, provider_results, compiled_report, artifact_check,
degraded_search_providers, raw_research_view)` ([research/pipeline.py:40-62](research/pipeline.py#L40-L62)).

### 2.5 Web-search research providers (SerpAPI / Tavily / Firecrawl)

All three implement the same five-step shape; SerpAPI is canonical and the other two import its
internals.

| Step | SerpAPI | Tavily | Firecrawl |
|---|---|---|---|
| Entry | [serp_research.py:160](research/serp_research.py#L160) | [tavily_research.py:68](research/tavily_research.py#L68) | [firecrawl_research.py:78](research/firecrawl_research.py#L78) |
| Query gen | `generate_google_search_query_plan` (Sonnet, ≤8 queries) + the raw title prepended ([query_maker.py:108](query_maker.py#L108)) | same | same |
| Search API | `GET serpapi.com/search` ([serp_research.py:694-724](research/serp_research.py#L694-L724)) | `POST api.tavily.com/search` ([tavily_research.py:361-391](research/tavily_research.py#L361-L391)) | `POST api.firecrawl.dev/v2/search` ([firecrawl_research.py:380-414](research/firecrawl_research.py#L380-L414)) |
| Rank | Sonnet groups ≤20 URLs by purpose ([serp_research.py:315](research/serp_research.py#L315)) | [tavily_research.py:227](research/tavily_research.py#L227) | [firecrawl_research.py:256](research/firecrawl_research.py#L256) |
| Scrape+extract | **shared** `run_scrape_cycles` ([serp_research.py:368](research/serp_research.py#L368)) | shared | shared |

Search concurrency is capped at 3 in-flight queries (`_limited_gather(..., limit=3)`); scraping at
2 (`_limited_gather(..., limit=2)`, [serp_research.py:1155-1158](research/serp_research.py#L1155-L1158)).
Social-media hosts are stripped from the *ranking* payload but their snippets survive in the output
([serp_research.py:106-134](research/serp_research.py#L106-L134)).

**Scrape cycles** ([serp_research.py:368-466](research/serp_research.py#L368-L466)): up to
`DEFAULT_MAX_SCRAPE_CYCLES = 3` rounds. Each round scrapes one URL per still-"lacking" group, then
runs a Sonnet extract that returns prose + a fenced `{"lacking_groups": [...]}` trailer. Loop exits
when nothing is lacking, targets run out, or `would_breach_input_reserve(31_250)` is True. Extract
failures get one retry ([serp_research.py:469-487](research/serp_research.py#L469-L487)); if that
also fails, a previously-validated cycle report is salvaged provided it passes a quality floor
([serp_research.py:490-505](research/serp_research.py#L490-L505)).

**Per-URL scrape ladder** ([serp_research.py:874-1153](research/serp_research.py#L874-L1153)), in order:
1. `Adapters.find_adapter(url)` — if one claims the URL, use it exclusively (no fallback beyond it).
2. `claim_scrape_url` dedupe; on a repeat, serve `get_cached_scrape_content` or emit a
   `skipped-duplicate` tombstone.
3. Firecrawl `/v2/scrape` (gated on `ENABLE_FIRECRAWL_GENERAL_SCRAPE` and the credit budget).
4. Crawl4AI headless-browser markdown (`basic_crawl_markdown`).
5. Wayback latest-snapshot text (`Adapters.Wayback.snapshot_fallback_text`).

Every outcome is written to both `source_ledger` and `research_trace`. Scraped content is truncated
to `_MAX_SCRAPE_CHARS = 18_000`; the extract prompt to `_MAX_EXTRACT_INPUT_CHARS = 90_000`
([serp_research.py:1190](research/serp_research.py#L1190)).

**Returns:** a large formatted block (`format_serp_research` etc.) containing generated queries,
ranked groups, per-cycle scrape statuses, the compiled scraped report, and **all raw search
snippets** — with the snippet replaced by `SNIPPET_OMITTED_NOTE` only for URLs that were scraped in
full ([serp_research.py:611-681](research/serp_research.py#L611-L681)).

### 2.6 Resolution-source scraper

**Trigger:** stage 1 of the pipeline, when `ENABLE_RESOLUTION_SOURCE_RESEARCH`.

**Steps** (`scrape_resolution_sources`, [resolution_criteria_scraper.py:1123](resolution_criteria_scraper.py#L1123)):
1. Regex-extract URLs from the resolution criteria **and** the question context, criteria-first,
   deduped, capped at `max_urls=10` (lines 1144-1160).
2. Scrape all of them concurrently (`max_concurrent=5`, 30 s each) via `_scrape_resolution_url`
   ([resolution_criteria_scraper.py:680](resolution_criteria_scraper.py#L680)), whose ladder is:
   Google Sheets CSV adapter → Yahoo quotes adapter → Firecrawl (`priority=True`) → Crawl4AI →
   Wayback snapshot.
3. `release_resolution_reserve()` in a `finally`, handing the remaining Firecrawl credits to the
   general research path (lines 1170-1174).
4. Heuristic clean per source: criteria URLs get `_LLM_MAX_INPUT` (100 000 chars), background-only
   URLs get `_BACKGROUND_CONTENT_CHARS` (20 000) (lines 1188-1196).
5. Wayback **history** pass on the primary URL — CDX listing, ≤4 evenly-spread captures over 18
   months, one Sonnet call summarizing value series/cadence/changes. Skipped for market/quote
   domains (lines 1211-1229, [Adapters/Wayback.py:201](Adapters/Wayback.py#L201)).
6. Batch the cleaned sources into ≤100 000-char summarizer inputs, at most
   `_MAX_SUMMARY_CALLS = 3` calls; sources beyond that are named as un-summarized in the output
   (lines 1231-1258, [resolution_criteria_scraper.py:624-663](resolution_criteria_scraper.py#L624-L663)).

**Returns:** a `# Resolution Criteria Sources` markdown block; `""` when no URLs were found.

### 2.7 Prediction-market research

Three near-identical modules, all synchronous and called via `asyncio.to_thread`.

| Module | API | Flow |
|---|---|---|
| Kalshi | `external-api.kalshi.com/trade-api/v2/markets` — fetches up to 3 pages × 1000 open markets, filters **locally** | [kalshi_research.py:455](research/kalshi_research.py#L455) `scrape_kalshi` |
| Polymarket | `gamma-api.polymarket.com/public-search` per query | [polymarket_research.py:379](research/polymarket_research.py#L379) `scrape_polymarket` |
| Manifold | `api.manifold.markets/v0` search | [manifold_research.py:345](research/manifold_research.py#L345) `scrape_manifold` |

Each: LLM-generates 3-4 short search terms (falling back to stop-word keyword extraction), fetches
candidates, parses them, runs **one** Sonnet relevance-scoring call (0-10), keeps the top 3 scoring
≥ 5.0, and formats a block with odds/volume/liquidity/URLs. Below threshold they return a
`"No sufficiently relevant …"` string, which `should_include_provider_result` then filters out.

These three call OpenRouter through their own `OpenAI` (sync) clients, **not** through
`llm_client.call_llm` — they wire up `MonetaryCostManager.start_openrouter_call` manually.

### 2.8 Research compiler (`compiler.py:133` `compile_research_report`)

**Steps:**
1. `_prepare_sections` cleans each provider section. AskNews output is parsed into `Article`
   objects and near-duplicates grouped ([compiler.py:281-378](compiler.py#L281-L378)); market
   sections are line-filtered and truncated at `_MAX_PROVIDER_CHARS = 24_000`
   ([compiler.py:235-278](compiler.py#L235-L278)); all other sections pass through **untruncated**.
2. Build a deterministic `heuristic_report` up front ([compiler.py:825](compiler.py#L825)) — it is
   the fallback and costs no tokens.
3. `_fit_sections_to_budget` ([compiler.py:493](compiler.py#L493)): if the combined sections exceed
   `_COMPILER_INPUT_BUDGET_CHARS = 120_000`, the largest **non-resolution** sections are compressed
   by up to `_MAX_PRECOMPRESS_CALLS = 3` Sonnet "lossless compressor" calls; only if still over
   budget is anything cut, and then with a loud in-band `[COMPILER INPUT BUDGET TRUNCATION …]`
   marker ([compiler.py:469-490](compiler.py#L469-L490)).
4. Emit the byte-exact compiler input to the trace ([compiler.py:600-613](compiler.py#L600-L613)).
5. One Opus call (`temperature=0.1, max_tokens=6000`) producing a fixed section list:
   Extracted Artifact Rows / Resolution Mechanics / Key Evidence (≤15 `[E#]` items with `[D#]`
   source-document tags) / Balance Check / Derived Implications / Market Signals / Gaps And
   Cautions ([compiler.py:765-822](compiler.py#L765-L822)).
6. Any failure — including `HardLimitExceededError` — returns the heuristic report instead of
   raising ([compiler.py:162-183](compiler.py#L162-L183)).

### 2.9 Forecasting — shared ensemble machinery

`gather_forecast_runs` ([forecasters/base.py:110](forecasters/base.py#L110)) runs N prompts in
parallel, one model per run cycling `FORECASTER_MODELS`. Per run: a hard call failure is caught and
the run is marked invalid rather than sinking the question; a response failing the caller's
validator gets **exactly one** repair retry with the error text appended.

`heterogeneous_run_setup` ([forecasters/base.py:85](forecasters/base.py#L85)) swaps the **last**
run to the raw (uncompiled) research on `HETEROGENEOUS_RUN_MODEL`, when
`HETEROGENEOUS_RUN_ENABLED` and `raw_prompt` and `num_runs >= 2`. It is a swap, not an addition.

Prompts are sent with `cache_static_prefix=True`, i.e. wrapped in an Anthropic-style
`cache_control: ephemeral` content block ([llm_client.py:290-305](llm_client.py#L290-L305)).

### 2.10 Binary forecasting

[forecasters/binary.py:250](forecasters/binary.py#L250). A ~190-line phased superforecaster prompt
(Phase 0 research audit → 0.5 resolution mechanics/event chain → 1 outside view → 2 inside view →
3 adversarial → 4 pre-mortem), ending in `"Probability: ZZ%"`.

Parsing takes the **last** `\d+%` in the response and clamps to `[1, 99]`
([forecasters/binary.py:212-221](forecasters/binary.py#L212-L221)).

Aggregation: median of valid runs. **Unless** `max - min >= SPREAD_THRESHOLD (30 pp)`, in which
case a single tiebreaker call on `FORECASTER_TIEBREAKER_MODEL` sees all rationales and issues the
final number ([forecasters/binary.py:324-367](forecasters/binary.py#L324-L367)).

If every run fails: returns **0.5** with an explanatory comment (`run_values=[]`) —
[forecasters/binary.py:305-317](forecasters/binary.py#L305-L317). Note this still gets submitted.

### 2.11 Multiple-choice forecasting

[forecasters/multiple_choice.py:278](forecasters/multiple_choice.py#L278). Parsing scans every line
for the last number and takes the final N numbers, N = option count
([forecasters/multiple_choice.py:199-224](forecasters/multiple_choice.py#L199-L224)).
Normalization clamps each option to `[0.01, 0.99]`, renormalizes, and dumps the rounding residual
onto the **last** option ([forecasters/multiple_choice.py:236-242](forecasters/multiple_choice.py#L236-L242)).

Aggregation: plain per-option mean across runs. No tiebreaker. All-runs-fail → uniform distribution
([forecasters/multiple_choice.py:341-355](forecasters/multiple_choice.py#L341-L355)).

### 2.12 Numeric / discrete forecasting

The largest capability ([forecasters/numeric.py](forecasters/numeric.py), 2 297 lines).

**Prompt/geometry** (`build_numeric_prompt`, [numeric.py:1858](forecasters/numeric.py#L1858)):
reads `scaling.range_min/range_max/zero_point`, `open_upper_bound`, `open_lower_bound`, `unit`.
For `discrete`, outcome count comes from `scaling.inbound_outcome_count`, else the grid length,
else the integer span ([numeric.py:1839-1855](forecasters/numeric.py#L1839-L1855)); `cdf_size =
outcome_count + 1`. For `numeric`, `cdf_size = 201`.
PMF routing: `use_pmf = outcome_count <= 30 or (integer step and outcome_count <= 100)`
([numeric.py:1882-1884](forecasters/numeric.py#L1882-L1884)).

**Model output contract:** a JSON `components` list (1-3 smooth families: normal, skew_normal,
student_t, lognormal, gamma, truncated_normal, beta) or, for count questions, a
`{"distribution": {"type": "pmf", ...}}`.

**Per-run processing** (`numeric_response_to_raw_cdf`, [numeric.py:1938](forecasters/numeric.py#L1938)):
1. Parse ([numeric.py:1693](forecasters/numeric.py#L1693)) — tolerant of prose-wrapped JSON.
2. **Unit-slip auto-correction**: if the component location params sit off the grid by a clean
   power of ten (three gates, tolerance 0.15 in log10), every value-axis parameter is rescaled
   ([numeric.py:445-607](forecasters/numeric.py#L445-L607)).
3. **Guardrails** ([numeric.py:1595-1682](forecasters/numeric.py#L1595-L1682)): drop unbuildable
   components → warn on stated-vs-actual CI mismatch >2× → renormalize → drop components under
   5 % weight → cap at 3 → apply a minimum-width floor of 2 % of the question range → merge
   same-family components within 5 % of range → rebuild. If nothing survives: a broad normal
   (`mean = midpoint, std = 0.25 × range`), flagged as `used_fallback`.
4. Evaluate the CDF on `nominal_grid`, which honours `zero_point` log-scaling
   ([numeric.py:859-880](forecasters/numeric.py#L859-L880)).

**Run filtering:** a run whose entire distribution lies off the grid (flat CDF, corroborated by its
own location params) is dropped as uninformative ([numeric.py:2048-2077](forecasters/numeric.py#L2048-L2077)).

**Coarse-PMF repair** ([numeric.py:731-831](forecasters/numeric.py#L731-L831)): a PMF run that
skipped grid bins has its mass spread over centered cells — but only into bins that *another* run
also places mass on (single-run ensembles fill by default). Mass never leaves the run's own support
span; bin 0 is never touched.

**Aggregation** ([numeric.py:1800-1810](forecasters/numeric.py#L1800-L1810)): PMF-space mean for
count questions, quantile (horizontal) averaging otherwise. Quantile averaging recomposes
out-of-bound tail mass separately to avoid boundary spikes
([numeric.py:1740-1797](forecasters/numeric.py#L1740-L1797)).

**Standardization:** `NumericDistribution._standardize_cdf` applies the Metaculus minimum-slope
rule and binary-searches a scale so no bin exceeds the per-bin PMF cap
([numeric.py:1374-1427](forecasters/numeric.py#L1374-L1427)). Final length is asserted against
`geometry["cdf_size"]`, raising rather than submitting a wrong-length CDF
([numeric.py:2251-2255](forecasters/numeric.py#L2251-L2255)).

**Comb check** ([numeric.py:834-856](forecasters/numeric.py#L834-L856)): counts material sawtooth
alternations in the submitted curve; warn-only, persisted to `forecast.json` as `extra.comb_check`.

**Abstention:** if no run survives, returns `forecast=None` and the orchestrator submits nothing
([numeric.py:2184-2207](forecasters/numeric.py#L2184-L2207)). This is the only forecaster that
abstains.

### 2.13 Submission to Metaculus

`post_question_prediction` POSTs `[{"question": qid, **payload}]` to `/questions/forecast/` and
prints the payload inside a `::group::` fold ([metaculus_client.py:236-247](metaculus_client.py#L236-L247)).
`post_question_comment` POSTs to `/comments/create/` with **`is_private: True`** and
`included_forecast: True` ([metaculus_client.py:222-233](metaculus_client.py#L222-L233)).
Payload shape is chosen by question type in `create_forecast_payload`
([metaculus_client.py:250-270](metaculus_client.py#L250-L270)).

All Metaculus HTTP goes through retry helpers: 3 attempts, exponential backoff seeded at
`INITIAL_API_GET_RETRY_WAIT_SECONDS`, honouring `Retry-After`, with a fixed
`METACULUS_REQUEST_INTERVAL` sleep **before every attempt** and a global semaphore of
`METACULUS_MAX_CONCURRENT_REQUESTS` (default 1) ([metaculus_client.py:66-219](metaculus_client.py#L66-L219)).

### 2.14 Cost/usage tracking

`MonetaryCostManager` ([monetary_cost_manager.py:176](monetary_cost_manager.py#L176)) is a
ContextVar-stacked recorder. Every OpenRouter call registers via `start_openrouter_call`, which
estimates input tokens as `ceil(chars / 3.2)` and **raises `HardLimitExceededError` pre-flight** if
that would exceed the input limit or the combined limit ([monetary_cost_manager.py:404-440](monetary_cost_manager.py#L404-L440)).
Responses record provider-reported native token counts, cached tokens, reasoning tokens, and
`usage.cost + cost_details.upstream_inference_cost` ([monetary_cost_manager.py:500-518](monetary_cost_manager.py#L500-L518)).

`would_breach_input_reserve` is the **soft** gate: it never raises, and only fires on managers that
declare *both* an input hard limit and a non-zero reserve ([monetary_cost_manager.py:346-367](monetary_cost_manager.py#L346-L367)).
The reserve size is `87 500 + 12 500×runs (+37 500 if the heterogeneous run applies)`, overridable
by `RESEARCH_RESERVE_INPUT_TOKENS` ([monetary_cost_manager.py:64-83](monetary_cost_manager.py#L64-L83)).

### 2.15 Auditing artifacts

- **`audit.md`** — token-usage YAML plus a full URL ledger with per-tool rollups and one row per
  URL event (`candidate` / `ranked-for-scrape` / `scraped`, engine, ok/failed, chars)
  ([artifacts.py:98-114](artifacts.py#L98-L114), [artifacts.py:139-221](artifacts.py#L139-L221),
  [source_ledger.py](source_ledger.py)).
- **`trace/` + `evolution.md`** — every research state change written as a numbered payload file
  plus a JSONL line; the renderer produces a timeline, a failures list, consecutive-version diffs
  of chained documents (extract reports, artifact checks, briefs) with **verbatim removed lines**,
  a scrape-novelty table, and a citation-survival table marking scraped-OK URLs never cited in the
  final brief ([research_trace.py:229-389](research_trace.py#L229-L389)). Every public function
  swallows its own exceptions; tracing can never fail a run.

### 2.16 URL adapters

`find_adapter` returns the first match in registry order ([Adapters/registry.py:12-27](Adapters/registry.py#L12-L27)):

| Adapter | Claims | Does |
|---|---|---|
| `MetaculusAdapter` | `metaculus.com/questions/<id>` | GETs `/posts/{id}/`, formats question + community prediction ([Adapters/Metaculus.py:28](Adapters/Metaculus.py#L28)) |
| `GoogleTrendsAdapter` | `trends.google.com/trends/explore`, `serpapi.com?engine=google_trends` | Calls SerpAPI's Google Trends engine ([Adapters/GoogleTrends.py:67](Adapters/GoogleTrends.py#L67)) |
| `GoogleSheetsAdapter` | `docs.google.com/spreadsheets/d/…` | Fetches CSV via `/export` then `/gviz/tq`; keeps 30 head + 170 tail rows ([Adapters/GoogleSheets.py:42](Adapters/GoogleSheets.py#L42)) |
| `WikipediaAdapter` | Wikipedia page URLs | Fetches the REST `with_html` payload, then makes **its own Sonnet call** to extract ([Adapters/Wikipedia.py:35](Adapters/Wikipedia.py#L35), [Adapters/Wikipedia.py:170](Adapters/Wikipedia.py#L170)) |
| `YahooQuotesAdapter` | `finance.yahoo.com/quote/<symbol>` | Calls `query1.finance.yahoo.com/v8/finance/chart/` (3 mo daily) ([Adapters/YahooQuotes.py:63](Adapters/YahooQuotes.py#L63)) |
| `PdfAdapter` | any path ending `.pdf` | Streams ≤25 MB, parses ≤40 pages with pymupdf4llm, falls back to pypdf, raises on a scanned PDF ([Adapters/Pdf.py:48](Adapters/Pdf.py#L48)) |

`Adapters/Wayback.py` is deliberately **not** in the registry; it is called directly by the
resolution scraper and by the scrape ladder's last rung.

### 2.17 Feedback-loop architecture (parallel, gitignored)

A second, complete architecture that composes the same modules
([feedback_loop/orchestrator.py](feedback_loop/orchestrator.py)):

1. `run_research_single_cycle` — a modified copy of `run_research` with `max_scrape_cycles=1` and
   **stages 5/5.5 removed** ([feedback_loop/research_pipeline.py:97](feedback_loop/research_pipeline.py#L97)).
2. Chain rounds (default 2, [feedback_loop/config.py:9](feedback_loop/config.py#L9)): one Opus
   forecast per round with a `PHASE 5 — RESEARCH GAP ANALYSIS` block appended, asking for
   machine-readable gaps + queries ([feedback_loop/gap_analysis.py:33-66](feedback_loop/gap_analysis.py#L33-L66)).
3. Gate A closes if no gap has `expected_swing ∈ {moderate, decisive}` with queries. Otherwise a
   focused research pass runs the forecaster's queries through the provider fallback chain
   ([feedback_loop/research_rounds.py:71](feedback_loop/research_rounds.py#L71)); Gate B closes if
   nothing usable came back.
4. New evidence is appended to the brief as a dated **addendum** (the compiler is never re-run),
   and the prior round's rationale is forwarded with its point estimates masked
   ([feedback_loop/gap_analysis.py:182-217](feedback_loop/gap_analysis.py#L182-L217)).
5. Final: the unchanged main ensemble on the final brief. Chain estimates are recorded in
   `chain.md`/`forecast.json` but **never averaged into the submission**.

Time gates shed chain rounds when under `FEEDBACK_MIN_SECONDS_REMAINING` (420 s) of the 30-minute
per-question budget.

### 2.18 Offline evaluation tools

- `eval_tools/replay.py` — re-runs the forecaster against a saved `research.md` compiled brief.
- `eval_tools/compile_replay.py` — reconstructs the compiler's raw inputs from a saved run and
  re-runs `compile_research_report` with current code, re-applying the future-date gate at the
  **original** run date.
- `eval_tools/score_forecasts.py` — walks `docs/runs/**/forecast.json`, fetches resolutions, scores
  binary/MC by Brier and numeric by CRPS in location space.

---

## 3. External dependencies

| Service | Endpoint / SDK | Called from |
|---|---|---|
| **Metaculus API** | `https://www.metaculus.com/api` — `/posts/`, `/posts/{id}/`, `/questions/forecast/`, `/comments/create/` | [metaculus_client.py](metaculus_client.py); also [Adapters/Metaculus.py:33](Adapters/Metaculus.py#L33) |
| **OpenRouter (chat)** | `https://openrouter.ai/api/v1` | [llm_client.py:325](llm_client.py#L325) (shared client); **plus 5 independent clients**: [compiler.py:624](compiler.py#L624), [resolution_criteria_scraper.py:353](resolution_criteria_scraper.py#L353)/[:471](resolution_criteria_scraper.py#L471)/[:565](resolution_criteria_scraper.py#L565), [kalshi_research.py:325](research/kalshi_research.py#L325), [polymarket_research.py:249](research/polymarket_research.py#L249), [manifold_research.py:241](research/manifold_research.py#L241) |
| **OpenRouter (key usage)** | `https://openrouter.ai/api/v1/key` | [monetary_cost_manager.py:592](monetary_cost_manager.py#L592) |
| **AskNews** | `asknews_sdk.AsyncAskNewsSDK` news search (2 calls: "latest news" ×6 + "news knowledge" ×10) | [research/asknews_research.py:144-185](research/asknews_research.py#L144-L185) |
| **SerpAPI** | `https://serpapi.com/search` (google engine); `search.json` (google_trends engine) | [serp_research.py:33](research/serp_research.py#L33), [Adapters/GoogleTrends.py:17](Adapters/GoogleTrends.py#L17) |
| **Firecrawl** | `api.firecrawl.dev/v2/search`, `api.firecrawl.dev/v2/scrape` | [firecrawl_research.py:48](research/firecrawl_research.py#L48), [firecrawl_scrape.py:51](research/firecrawl_scrape.py#L51) |
| **Tavily** | `https://api.tavily.com/search` | [tavily_research.py:44](research/tavily_research.py#L44) |
| **Kalshi** | `https://external-api.kalshi.com/trade-api/v2` | [kalshi_research.py:46](research/kalshi_research.py#L46) |
| **Polymarket** | `https://gamma-api.polymarket.com` | [polymarket_research.py:45](research/polymarket_research.py#L45) |
| **Manifold** | `https://api.manifold.markets/v0` | [manifold_research.py:46](research/manifold_research.py#L46) |
| **Internet Archive** | `web.archive.org/cdx/search/cdx`, `web.archive.org/web/{ts}id_/{url}` | [Adapters/Wayback.py:56-58](Adapters/Wayback.py#L56-L58) |
| **Wikipedia REST** | `<lang>.wikipedia.org` page-with-html endpoint | [Adapters/Wikipedia.py:42](Adapters/Wikipedia.py#L42) |
| **Google Sheets** | `docs.google.com/spreadsheets/d/…/export`, `/gviz/tq` | [Adapters/GoogleSheets.py:51-54](Adapters/GoogleSheets.py#L51-L54) |
| **Yahoo Finance** | `query1.finance.yahoo.com/v8/finance/chart/` | [Adapters/YahooQuotes.py:35](Adapters/YahooQuotes.py#L35) |
| **Headless Chromium** | `crawl4ai.AsyncWebCrawler` (Playwright) | [Crawl4AI/crawl.py:41-72](Crawl4AI/crawl.py#L41-L72) |

### Models actually referenced in code

| Constant | Value | Used for |
|---|---|---|
| `DEFAULT_FORECASTER_MODEL` | `anthropic/claude-opus-5` | forecaster pool member 1; chain rounds |
| `FORECASTER_MODELS` (default) | `[anthropic/claude-opus-5, openai/gpt-5.6-sol]` | ensemble pool, cycled |
| `FORECASTER_TIEBREAKER_MODEL` | `anthropic/claude-opus-5` | binary tiebreaker only |
| `HETEROGENEOUS_RUN_MODEL` | `anthropic/claude-sonnet-5` | last (raw-research) ensemble run |
| `compiler._DEFAULT_MODEL` | `anthropic/claude-opus-5` | research brief |
| `compiler._PRECOMPRESS_MODEL` | `anthropic/claude-sonnet-5` | over-budget section compression |
| `ARTIFACT_CHECK_MODEL` | `anthropic/claude-sonnet-5` | artifact verification |
| `DEFAULT_EVIDENCE_PLAN_MODEL` | `anthropic/claude-sonnet-5` | evidence plan |
| `DEFAULT_QUERY_GENERATION_MODEL` | `anthropic/claude-sonnet-5` | search-query generation |
| `DEFAULT_SERP_RANKING_MODEL` / `DEFAULT_EXTRACT_MODEL` | `anthropic/claude-sonnet-5` | URL ranking / scrape extraction |
| `_KALSHI/_POLYMARKET/_MANIFOLD_SCORING_MODEL` | `anthropic/claude-sonnet-5` | market relevance scoring |
| `Wikipedia.DEFAULT_EXTRACT_MODEL` | `anthropic/claude-sonnet-5` | Wikipedia page extraction |
| `resolution_criteria_scraper` `llm_model` default | `anthropic/claude-sonnet-5` | page summary, compile, wayback history |
| `AskNewsSearcher._default_model` | `deepseek-basic` | **dead** — only reachable from unused deep-research methods |

### Python dependencies (`pyproject.toml`)

`python-dotenv, numpy, pydantic, openai, asknews, forecasting-tools, httpx, aiohttp, statsmodels,
crawl4ai, sentence-transformers, pymupdf4llm, pypdf`. Requires Python `>=3.11,<4.0`.

`forecasting-tools` is used for exactly one thing: `AskNewsCache`
([research/asknews_research.py:127-129](research/asknews_research.py#L127-L129)). `pymupdf` is
imported by `Adapters/Pdf.py` but is only a transitive dependency of `pymupdf4llm`.

---

## 4. Configuration

### 4.1 Environment variables read by code

| Variable | Read at | Default | Effect | Status |
|---|---|---|---|---|
| `METACULUS_TOKEN` | [config.py:80](config.py#L80) | — | Auth for all Metaculus calls; **required** | set in `.env` + all workflows |
| `OPENROUTER_API_KEY` | [config.py:84](config.py#L84) | — | All LLM calls; **required** | set |
| `ASKNEWS_CLIENT_ID` / `ASKNEWS_SECRET` | [config.py:81-82](config.py#L81-L82) | — | OAuth for AskNews | set |
| `ASKNEWS_API_KEY` | [config.py:83](config.py#L83) | — | Alternative AskNews auth; mutually exclusive with OAuth | in workflows/`.env.example`, **not in `.env`** |
| `SERPAPI_API_KEY` | [config.py:85](config.py#L85) | — | SerpAPI search + Google Trends adapter | set |
| `FIRECRAWL_API_KEY` | [config.py:86](config.py#L86) | — | Firecrawl search + scrape | set |
| `TAVILY_API_KEY` | [config.py:87](config.py#L87) | — | Tavily search | set |
| `OPENROUTER_COST_HARD_LIMIT_USD` | [config.py:88](config.py#L88) | `0` | Default for `--cost-limit`; despite the name the value is **estimated tokens**, and `0` disables | in workflows/`.env.example` |
| `METACULUS_MAX_CONCURRENT_REQUESTS` | [config.py:76](config.py#L76) | `1` | Semaphore over all Metaculus HTTP | set |
| `METACULUS_REQUEST_INTERVAL` | [config.py:78](config.py#L78) | `3.0` | Sleep before every Metaculus attempt | set |
| `INITIAL_API_GET_RETRY_WAIT_SECONDS` | [config.py:163](config.py#L163) | `3.0` | Metaculus retry backoff seed | set |
| `ASKNEWS_CACHE_MODE` | [asknews_research.py:90](research/asknews_research.py#L90) | `no_cache` | `use_cache` / `use_cache_with_fallback` / `no_cache` | in workflows/`.env.example` |
| `ENABLE_ASKNEWS_RESEARCH` | [config.py:91](config.py#L91) | `true` | Provider toggle | in workflows/`.env.example` |
| `ENABLE_RESOLUTION_SOURCE_RESEARCH` | [config.py:92](config.py#L92) | `true` | Provider toggle; also gates the Firecrawl reserve ([firecrawl_scrape.py:144](research/firecrawl_scrape.py#L144)) | in workflows/`.env.example` |
| `ENABLE_SERPAPI_RESEARCH` / `ENABLE_TAVILY_RESEARCH` / `ENABLE_FIRECRAWL_RESEARCH` | [config.py:97-99](config.py#L97-L99) | `true` | Membership + order of the search chain | in workflows/`.env.example` |
| `ENABLE_PREDICTION_MARKET_RESEARCH` | [config.py:100](config.py#L100) | `true` | Kalshi/Manifold/Polymarket toggle | in workflows/`.env.example` |
| `FIRECRAWL_SEARCH_TBS` | [config.py:101](config.py#L101) | `""` | Firecrawl time filter (`qdr:w` etc.) | in workflows/`.env.example` |
| `TAVILY_SEARCH_DEPTH` | [config.py:116](config.py#L116) | `basic` | Tavily depth/credit cost | in `.env.example` only — **not** in any workflow |
| `OPENROUTER_MAX_ATTEMPTS` | [llm_client.py:21](llm_client.py#L21) | `3` | OpenRouter retry count | in `.env.example` only |
| `OPENROUTER_RETRY_BASE_SECONDS` | [llm_client.py:22](llm_client.py#L22) | `2.0` | Retry backoff base | in `.env.example` only |
| `FORECASTER_MODELS` | [config.py:49](config.py#L49) | `opus-5,gpt-5.6-sol` | **Ensemble composition** — comma-separated | **read, never set anywhere** |
| `FORECASTER_TIEBREAKER_MODEL` | [config.py:58](config.py#L58) | `opus-5` | Binary tiebreaker | **read, never set** |
| `HETEROGENEOUS_RUN_ENABLED` | [config.py:71](config.py#L71) | `true` | Whether the last run reads raw research | **read, never set** |
| `HETEROGENEOUS_RUN_MODEL` | [config.py:72](config.py#L72) | `sonnet-5` | Model for that run | **read, never set** |
| `ENABLE_FIRECRAWL_GENERAL_SCRAPE` | [config.py:105](config.py#L105) | `true` | Firecrawl-first in the general scrape ladder | **read, never set** |
| `FIRECRAWL_QUESTION_CREDIT_CAP` | [config.py:111](config.py#L111) | `25` | Per-question Firecrawl credit cap | **read, never set** (except in a test) |
| `FIRECRAWL_RESOLUTION_CREDIT_RESERVE` | [config.py:112](config.py#L112) | `10` | Credits reserved for resolution URLs | **read, never set** (except in a test) |
| `ENABLE_RESEARCH_TRACE` | [config.py:120](config.py#L120) | `true` | `trace/` + `evolution.md` | **read, never set** |
| `LLM_CONCURRENT_REQUESTS` | [config.py:165](config.py#L165) | `5` | Global LLM semaphore | **read, never set** |
| `QUESTION_TIMEOUT_SECONDS` | [orchestrator.py:31](orchestrator.py#L31) | `1200` | Per-question wall clock (main arch) | **read, never set** |
| `ARTIFACT_RETRY_TIMEOUT_SECONDS` | [research/pipeline.py:37](research/pipeline.py#L37) | `150` | Focused-retry budget; also reused by gap rounds | **read, never set** |
| `RESEARCH_RESERVE_INPUT_TOKENS` | [monetary_cost_manager.py:66](monetary_cost_manager.py#L66) | computed | Overrides the reserve formula | **read, never set** (except in a test) |
| `METACULUS_RESEARCH_TOURNAMENT` | [Adapters/Metaculus.py:15](Adapters/Metaculus.py#L15) | `DEFAULT_TOURNAMENT_ID` | Only feeds `_fetch_all_posts` → `scrape_metaculus`, which is dead (§5) | **read, never set, drives only dead code** |
| `FEEDBACK_CHAIN_ROUNDS` | [feedback_loop/config.py:9](feedback_loop/config.py#L9) | `2` | Chain rounds | **read, never set** |
| `FEEDBACK_MIN_SECONDS_REMAINING` | [feedback_loop/config.py:14](feedback_loop/config.py#L14) | `420` | Time gate | **read, never set** |
| `FEEDBACK_QUESTION_TIMEOUT_SECONDS` | [feedback_loop/config.py:19](feedback_loop/config.py#L19) | `1800` | Per-question budget | **read, never set** |
| `FEEDBACK_MAX_SCRAPE_CYCLES` | [feedback_loop/config.py:26](feedback_loop/config.py#L26) | `1` | Scrape cycles in the feedback arch | **read, never set** |
| `FEEDBACK_ALLOW_NON_OPEN` | [feedback_loop/orchestrator.py:218](feedback_loop/orchestrator.py#L218) | unset | Lets a dry run forecast a closed question; force-disables submission | set only by `dry_run_one.py --allow-closed` |
| `PYTHONUTF8` / `PYTHONIOENCODING` | [Crawl4AI/crawl.py:13-14](Crawl4AI/crawl.py#L13-L14) | set via `setdefault` | Windows console encoding | written, not read by this code |

### 4.2 Set but never read

- **`GITHUB_API_TOKEN`** — present in `.env`; no `os.getenv("GITHUB_API_TOKEN")` anywhere.
- **`JINA_API_KEY`** — present in `.env`; no reader. (Presumably a removed scraper.)
- Workflow-local shell variables `DISPATCH_*`, `QUESTION_ID`, `POST_ID`, `SUBMIT_PREDICTION`,
  `NUM_RUNS`, `SKIP_PREVIOUSLY_FORECASTED`, `MAX_QUESTIONS`, `QUESTION_TYPE`, `TOKEN_LIMIT` are
  read only by the inline shell/Python in those workflows — not by the package.

### 4.3 Config files

- **`.env`** — loaded by `dotenv.load_dotenv()` at [config.py:8](config.py#L8) (also independently
  in `eval_tools/*`). Contains 12 keys (see §4.1); gitignored.
- **`.env.example`** — documentation only; **not loaded**. It lists 5 variables the code reads and
  omits 16 that the code reads (§4.1), and its comment about search-chain order is wrong (§6).
- **`pyproject.toml` / `poetry.lock`** — dependency pinning.
- **`.gitignore`** — excludes `docs/` (all run artifacts), `tests/`, `feedback_loop/`, `.claude/`,
  and the two root-level post-mortem folders.

### 4.4 Hardcoded constants that change behavior

Module-level, not env-overridable:

| Constant | Value | File:line | Effect |
|---|---|---|---|
| `NUM_RUNS_PER_QUESTION` | `3` | [config.py:26](config.py#L26) | Default ensemble size (CLI-overridable) |
| `SKIP_PREVIOUSLY_FORECASTED_QUESTIONS` | `True` | [config.py:75](config.py#L75) | Hardcoded; no CLI flag, no env var |
| `DEFAULT_TOURNAMENT_ID` | `fall-futureeval-2026` | [config.py:137](config.py#L137) | Used when `--tournament` omitted |
| `EXAMPLE_QUESTIONS` | 4 hardcoded IDs | [config.py:153](config.py#L153) | `--mode examples` |
| `MAX_API_GET_RETRIES` | `3` | [config.py:162](config.py#L162) | Metaculus retries |
| `list_posts_from_tournament` `count` | `50` | [metaculus_client.py:274](metaculus_client.py#L274) | Hard cap on questions per tournament |
| `CHARACTERS_PER_TOKEN` | `3.2` | [monetary_cost_manager.py:31](monetary_cost_manager.py#L31) | Denominates every token budget |
| `DEFAULT_INPUT_TOKEN_HARD_LIMIT` | `375_000` | [monetary_cost_manager.py:40](monetary_cost_manager.py#L40) | Per-question input ceiling |
| `DEFAULT_OUTPUT_TOKEN_HARD_LIMIT` | `62_500` | [monetary_cost_manager.py:41](monetary_cost_manager.py#L41) | Per-question output ceiling |
| Reserve components | `87_500 / 12_500 / 37_500` | [monetary_cost_manager.py:59-61](monetary_cost_manager.py#L59-L61) | Research soft-stop threshold |
| `_EXPECTED_SEARCH_PROVIDER_INPUT_TOKENS` | `62_500` | [research/pipeline.py:35](research/pipeline.py#L35) | Gate on entering a search provider |
| `_EXPECTED_ARTIFACT_RETRY_INPUT_TOKENS` | `37_500` | [research/pipeline.py:36](research/pipeline.py#L36) | Gate on the focused retry |
| `_EXPECTED_CYCLE_INPUT_TOKENS` | `31_250` | [serp_research.py:45](research/serp_research.py#L45) | Gate on another scrape cycle |
| `_MAX_RETRY_QUERIES` | `4` | [research/pipeline.py:29](research/pipeline.py#L29) | Retry query cap |
| `_MAX_ARTIFACT_CHECK_INPUT_CHARS` | `40_000` | [research/pipeline.py:28](research/pipeline.py#L28) | Artifact-check total input |
| per-provider artifact-check cut | `8_000` | [research/pipeline.py:903](research/pipeline.py#L903) | **Head**-truncation per provider |
| `_RAW_VIEW_MAX_CHARS` | `200_000` | [research/pipeline.py:689](research/pipeline.py#L689) | Raw-research view cap |
| `_COMPILER_INPUT_BUDGET_CHARS` | `120_000` | [compiler.py:35](compiler.py#L35) | Triggers precompression |
| `_MAX_PROVIDER_CHARS` | `24_000` | [compiler.py:27](compiler.py#L27) | Market sections only |
| `_MAX_KEY_EVIDENCE_ITEMS` | `10` | [compiler.py:41](compiler.py#L41) | Heuristic fallback only |
| compiler `max_tokens` | `6000` | [compiler.py:652](compiler.py#L652) | Brief length ceiling |
| `DEFAULT_QUERY_COUNT` | `8` | [query_maker.py:12](query_maker.py#L12) | Queries per provider |
| `DEFAULT_MAX_RANKED_URLS` | `20` | [serp_research.py:31](research/serp_research.py#L31) | URLs selected for scraping |
| `DEFAULT_MAX_SCRAPE_CYCLES` | `3` | [serp_research.py:32](research/serp_research.py#L32) | Scrape rounds |
| `_MAX_SCRAPE_CHARS` | `18_000` | [serp_research.py:35](research/serp_research.py#L35) | Per-page content cap |
| `_MAX_EXTRACT_INPUT_CHARS` | `90_000` | [serp_research.py:36](research/serp_research.py#L36) | Extract prompt cap |
| `SOCIAL_MEDIA_HOSTS` | 10 hosts | [serp_research.py:106](research/serp_research.py#L106) | Excluded from ranking |
| `MARKET_DATA_DOMAINS` | 6 hosts | [Adapters/Wayback.py:39](Adapters/Wayback.py#L39) | Wayback never used for these |
| resolution `max_urls` | `10` | [resolution_criteria_scraper.py:1130](resolution_criteria_scraper.py#L1130) | URLs scraped per question |
| `_MAX_SUMMARY_CALLS` | `3` | [resolution_criteria_scraper.py:621](resolution_criteria_scraper.py#L621) | Summary batches |
| `_LLM_MAX_INPUT` | `100_000` | [resolution_criteria_scraper.py:187](resolution_criteria_scraper.py#L187) | Summarizer input |
| `_BACKGROUND_CONTENT_CHARS` | `20_000` | [resolution_criteria_scraper.py:104](resolution_criteria_scraper.py#L104) | Background-only URL budget |
| `SPREAD_THRESHOLD` | `30` pp | [forecasters/binary.py:324](forecasters/binary.py#L324) | Tiebreaker trigger |
| probability clamp | `[1, 99]` | [forecasters/binary.py:218](forecasters/binary.py#L218) | Binary output floor/ceiling |
| MC option clamp | `[0.01, 0.99]` | [forecasters/multiple_choice.py:237](forecasters/multiple_choice.py#L237) | Per-option floor/ceiling |
| `MAX_PMF_OUTCOMES` / `MAX_PMF_INTEGER_OUTCOMES` | `30` / `100` | [numeric.py:1821](forecasters/numeric.py#L1821), [numeric.py:1836](forecasters/numeric.py#L1836) | PMF-vs-continuous routing |
| `MIN_SCALE_FRACTION` / `MIN_LOG_SIGMA` | `0.02` / `0.03` | [numeric.py:1464-1466](forecasters/numeric.py#L1464-L1466) | Anti-spike width floor |
| `COMPONENT_WEIGHT_FLOOR` / `MAX_COMPONENTS` / `COMPONENT_MERGE_FRACTION` | `0.05` / `3` / `0.05` | [numeric.py:1468-1473](forecasters/numeric.py#L1468-L1473) | Mixture guardrails |
| `UNIT_SCALE_SNAP_TOL` | `0.15` | [numeric.py:442](forecasters/numeric.py#L442) | Unit-slip detection tolerance |
| `MAX_NUMERIC_PMF_VALUE` | `0.2` | [numeric.py:1017](forecasters/numeric.py#L1017) | Metaculus per-bin cap |
| market `_MAX_RESULTS` / `_MIN_RELEVANCE_SCORE` | `3` / `5.0` | kalshi/manifold/polymarket | Markets kept |
| Kalshi fetch | `3 pages × 1000` | [kalshi_research.py:48-49](research/kalshi_research.py#L48-L49) | Local filtering corpus |
| `AskNewsSearcher._default_rate_limit` | `12` s | [asknews_research.py:79](research/asknews_research.py#L79) | Sleep between the two AskNews calls |
| Wayback `_DEFAULT_MONTHS_BACK` / `_DEFAULT_MAX_SNAPSHOTS` | `18` / `4` | [Adapters/Wayback.py:64-65](Adapters/Wayback.py#L64-L65) | History depth |
| `execute_python_code` timeout | `30` s | [llm_client.py:68](llm_client.py#L68) | Dead path (§5) |

---

## 5. Dead or unreachable code

Each entry states the evidence.

### 5.1 The entire LLM tool-use loop

`call_llm(use_tools=True)` is **never invoked**: a repo-wide grep for `use_tools` finds 11 call
sites and every one passes `False` ([Adapters/Wikipedia.py:174](Adapters/Wikipedia.py#L174),
[compiler.py:423](compiler.py#L423), [forecasters/base.py:159](forecasters/base.py#L159),
[query_maker.py:133](query_maker.py#L133), [research/evidence_plan.py:41](research/evidence_plan.py#L41),
[firecrawl_research.py:282](research/firecrawl_research.py#L282),
[research/pipeline.py:923](research/pipeline.py#L923),
[serp_research.py:342](research/serp_research.py#L342)/[:533](research/serp_research.py#L533),
[tavily_research.py:254](research/tavily_research.py#L254)). Therefore dead:

- `RUN_PYTHON_CODE_TOOL` ([llm_client.py:29-55](llm_client.py#L29-L55))
- `execute_python_code` ([llm_client.py:58-82](llm_client.py#L58-L82)) — this is the local
  `subprocess.run(sys.executable, ...)` sandbox; **currently unreachable**
- `_validate_tool_loop_response` ([llm_client.py:190-210](llm_client.py#L190-L210))
- the whole agentic branch ([llm_client.py:369-459](llm_client.py#L369-L459)), including the
  `ValueError("reached maximum tool-use iterations")` terminal

Note `docs/templatePrompts.py` still contains a `## TOOLS` prompt section instructing the model to
call `run_python_code`; that file is imported by nothing.

### 5.2 Legacy resolution-scraper path

`_legacy_scrape_resolution_sources_with_followups` ([resolution_criteria_scraper.py:932-1120](resolution_criteria_scraper.py#L932-L1120))
has no callers anywhere. Transitively dead with it:

- `_extract_follow_up_links` ([:507](resolution_criteria_scraper.py#L507)) — referenced only at
  `:1016` and `:1077`, both inside the legacy function
- `_compile_summaries` ([:556](resolution_criteria_scraper.py#L556)) — referenced only at `:1101`
- `_build_compile_prompt` ([:527](resolution_criteria_scraper.py#L527)) — referenced only by
  `_compile_summaries`
- `_FOLLOW_UP_SECTION` regex ([:501](resolution_criteria_scraper.py#L501))

Also dead independently: **`_build_summary_prompt`** ([:251-323](resolution_criteria_scraper.py#L251-L323))
— never called by anything; `_llm_summarize` uses `_build_resolution_summary_prompt` instead. (Its
prompt text also contains a corrupted bullet: line 315 `"weekly report listed) — older entries
matter as much as recent ones for"` is a dangling fragment with no sentence around it.)

### 5.3 Compiler's `raw_research` parameter

`compile_research_report(..., raw_research=None)` ([compiler.py:139](compiler.py#L139)). All four
call sites ([research/pipeline.py:495](research/pipeline.py#L495),
[feedback_loop/research_pipeline.py:311](feedback_loop/research_pipeline.py#L311),
[eval_tools/compile_replay.py:144](eval_tools/compile_replay.py#L144), one test) pass
`provider_results` only. Therefore the `Raw Research` branch of `_prepare_sections`
([compiler.py:198-199](compiler.py#L198-L199)) can never execute.

Also unused in `compiler.py`: `_HEADING_PATTERN` ([compiler.py:52](compiler.py#L52)) — compiled,
never referenced.

### 5.4 `query_maker.py` unused API surface

Three of five public functions have no callers:
- `generate_google_search_queries` ([query_maker.py:141](query_maker.py#L141))
- `generate_google_search_query_plan_from_question_details` ([:167](query_maker.py#L167)) — called
  only by the next one
- `generate_google_search_queries_from_question_details` ([:189](query_maker.py#L189))
- and therefore `_extract_question_fields` ([:207](query_maker.py#L207))

Only `generate_google_search_query_plan` is live (called by all three search providers).

### 5.5 `Adapters/Metaculus.py` community-prediction path

`scrape_metaculus` ([Adapters/Metaculus.py:52](Adapters/Metaculus.py#L52)) has no callers. Dead
with it: `_get_all_posts`, `_fetch_all_posts`, the `_CACHE` global, `_TOURNAMENT_IDS`, and the
`METACULUS_RESEARCH_TOURNAMENT` env var. `MetaculusAdapter.extract` (the registry path) is live and
does **not** use them.

### 5.6 `*_research_to_dict` serializers

`serp_research_to_dict` ([serp_research.py:684](research/serp_research.py#L684)),
`firecrawl_research_to_dict` ([firecrawl_research.py:368](research/firecrawl_research.py#L368)),
`tavily_research_to_dict` ([tavily_research.py:350](research/tavily_research.py#L350)) — no
callers. The pipeline consumes the formatted strings, not the dataclasses.

### 5.7 AskNews deep-research methods

`AskNewsSearcher.call_preconfigured_version` ([asknews_research.py:197](research/asknews_research.py#L197)),
`get_formatted_deep_research` ([:239](research/asknews_research.py#L239)), and `run_deep_research`
([:268](research/asknews_research.py#L268)) are never called. `run_asknews_research` only calls
`get_formatted_news_async`. Dead with them: `_default_search_depth`, `_default_max_depth`,
`_default_model` (`deepseek-basic`), `_default_sources`, and the two `DeepNewsModel` /
`CreateDeepNewsResponse` try-imports at [:15-19](research/asknews_research.py#L15-L19).
`get_formatted_news` (the sync `asyncio.run` wrapper, [:131](research/asknews_research.py#L131)) is
also uncalled.

### 5.8 Unused registry/ledger helpers

- `Crawl4AI.crawl.is_duplicate_scrape_payload` ([:178](Crawl4AI/crawl.py#L178)) — no callers.
  `_compact_scrape_content_for_prompt` re-implements the same check with a literal string prefix
  ([serp_research.py:1342](research/serp_research.py#L1342)) instead of calling it.
- `Crawl4AI.crawl.reset_scrape_dedupe_scope` ([:155](Crawl4AI/crawl.py#L155)) — no callers; the
  orchestrator sets the scope and never resets it.
- `Crawl4AI.crawl.reset_scrape_dedupe_registry` ([:182](Crawl4AI/crawl.py#L182)) — production-dead
  (tests only).
- `source_ledger.get_events` ([:150](source_ledger.py#L150)), `reset` ([:163](source_ledger.py#L163)),
  `reset_source_scope` ([:68](source_ledger.py#L68)), `reset_source_context` ([:77](source_ledger.py#L77))
  — no callers. Only `set_source_scope`, `set_source_context`, `current_context`,
  `record_url_event`, `record_text_urls`, `drain_events` are live.
- `research/firecrawl_scrape.firecrawl_budget_allows` ([:131](research/firecrawl_scrape.py#L131))
  — no callers; the budget is enforced only by `_try_charge` inside the scrape.
- `reset_firecrawl_budget` ([:175](research/firecrawl_scrape.py#L175)) — tests only. **Consequence:**
  the `_spent` / `_priority_spent` / `_reserve_released` dicts are never cleared during a run, so
  they grow one entry per question for the process lifetime (small, but unbounded).

### 5.9 `MonetaryCostManager` unused surface

- `log_usage_when_called` constructor flag ([:195](monetary_cost_manager.py#L195)) — every
  construction site omits it, so the branch at [:429](monetary_cost_manager.py#L429) is unreachable.
- `get_active_cost_managers` ([:341](monetary_cost_manager.py#L341)) — production-dead (tests only).
- `OpenRouterUsageHandle.input_characters` / `.input_tokens` properties
  ([:127-133](monetary_cost_manager.py#L127-L133)) — no readers.
- `MonetaryCostManager.get_usage_records` is live only via `format_usage_yaml_table`.

### 5.10 Unreachable branches in `numeric.py`

- `build_distribution` accepts `"mixture_normal"` ([:317](forecasters/numeric.py#L317)) and
  `"uniform"` ([:367](forecasters/numeric.py#L367)), but neither name is in `_COMPONENT_FAMILIES`
  ([:1475-1478](forecasters/numeric.py#L1475-L1478)), so `_validate_component` rejects them before
  they can reach `build_distribution` via the mixture path — **and** neither appears in the
  prompt's parameter reference. `MixtureNormal` ([:262](forecasters/numeric.py#L262)) is therefore
  constructible only from a hand-written spec, which nothing produces.
- `NumericDistribution.get_cdf` ([:1235](forecasters/numeric.py#L1235)) has no callers. The class
  is instantiated once, at [:2225](forecasters/numeric.py#L2225), with `standardize_cdf=False` and
  `strict_validation=False`, purely to reach the private `_standardize_cdf`. Because of those two
  flags, the following validators can never run in production:
  `_check_percentile_spacing`, `_check_too_far_from_bounds`, `_check_distribution_too_tall`,
  `_check_and_update_repeating_values` ([:1068-1078](forecasters/numeric.py#L1068-L1078) gates them).
  `_add_explicit_upper_lower_bound_percentiles`, `_get_cdf_at`,
  `_nominal_location_to_cdf_location`, `_cdf_location_to_nominal_location`,
  `_percentile_list_to_dict`, `_dict_to_percentile_list` are reachable only from `get_cdf` → also dead.
- `_check_log_scaled_fields` and `_check_percentiles_increasing` *do* run (they are outside the
  `strict_validation` gate), but on the 2-point synthetic percentile list only.

### 5.11 Whole files nothing imports

- `docs/templatePrompts.py` — no importer; contains an older tool-using binary prompt.
- `Crawl4AI/NOTES.md`, `docs/*.md` — documentation.
- `tests/` and `feedback_loop/tests/` — plain scripts, no test runner configured in `pyproject.toml`.

### 5.12 Conditions that can never be true

- `compiler._prepare_sections`'s `raw_research` branch (§5.3).
- In `_scrape_targets`, the `Scrape(cycle=0, ...)` values ([serp_research.py:936](research/serp_research.py#L936)
  and 8 other sites) are always discarded — the caller rebuilds every `Scrape` with the real
  `cycle_no` at [:1159-1171](research/serp_research.py#L1159-L1171). The literal `0` is inert.
- `research/pipeline._is_unavailable_result` is used, but the `degraded_search_providers.append`
  at [:445](research/pipeline.py#L445) requires `chosen_search_result is None`, which by
  construction means the retry provider was the *fallback* pick — so this line only fires when the
  entire main chain failed **and** the retry also failed.

---

## 6. Duplicated or conflicting logic

This is the section you asked for most. Ordered roughly by how likely each is to bite.

### 6.1 The search-chain order is documented three ways, two of them wrong

- Code: `SerpAPI → Tavily → Firecrawl` ([research/pipeline.py:254-260](research/pipeline.py#L254-L260)).
- The comment 5 lines above it says the same ([research/pipeline.py:249](research/pipeline.py#L249)) ✅.
- `config.py:93-96` says **"SerpAPI -> Firecrawl -> Tavily"** ❌.
- `.env.example` (search-provider comment block) says **"SerpAPI -> Firecrawl -> Tavily"** ❌.

`feedback_loop/research_pipeline.py:243-248` and `research_rounds._candidate_gap_providers`
([feedback_loop/research_rounds.py:59-64](feedback_loop/research_rounds.py#L59-L64)) each hardcode
the order a *third* time. Four independent copies of one ordering.

### 6.2 Two full research pipelines that have already drifted

`research/pipeline.run_research` (981 lines of module) vs
`feedback_loop/research_pipeline.run_research_single_cycle`. The copy's docstring claims the only
intentional differences are scrape cycles and the removal of stages 5/5.5. Actual additional drift:

| Behaviour | `run_research` | `run_research_single_cycle` |
|---|---|---|
| `research_trace.emit("provider", …)` per provider | yes ([pipeline.py:307-315](research/pipeline.py#L307-L315)) | **absent** |
| `research_trace.emit("artifact_check"/"retry_decision"/"brief")` | yes | **absent** |
| Post-compile `brief` trace | yes ([pipeline.py:503-516](research/pipeline.py#L503-L516)) | **absent** |
| Stage 5/5.5 | present | removed (intended) |

So a feedback-loop run produces a `trace/` folder containing scrape and precompress events but no
provider/brief events — `evolution.md`'s citation-survival table degrades to
`"No brief event captured — table unavailable."` ([research_trace.py:361](research_trace.py#L361)).

`should_include_provider_result` and `run_provider` are copied **verbatim** into
[feedback_loop/research_pipeline.py:54-94](feedback_loop/research_pipeline.py#L54-L94) (the file
says so). Any change to the "no results" markers must be made twice.

### 6.3 The budget reserve is silently inactive in the feedback architecture

Main: `MonetaryCostManager(hard_limit=…, reserved_input_tokens=research_reserve_input_tokens(n))`
([orchestrator.py:156-159](orchestrator.py#L156-L159)).
Feedback: `MonetaryCostManager(hard_limit=per_question_token_hard_limit)` — **no
`reserved_input_tokens`** ([feedback_loop/orchestrator.py:243](feedback_loop/orchestrator.py#L243)).

`would_breach_input_reserve` skips managers with no reserve
([monetary_cost_manager.py:360-361](monetary_cost_manager.py#L360-L361)). So all three soft-stop
gates — search-chain entry, artifact retry, scrape cycles — are **no-ops** under the feedback
architecture, and research can spend right up to the 375 000-token hard limit before the compile
call is refused. That is precisely the failure mode the reserve was added to prevent.

### 6.4 Two orchestrators with divergent per-question logic

`orchestrator.forecast_individual_question` vs
`feedback_loop.forecast_individual_question_feedback`. Both duplicate: scope setup, post fetch,
status/already-forecasted skips, artifact writing, abstention handling, submission. Divergences
found:

| | main | feedback |
|---|---|---|
| Timeout | 20 min (`QUESTION_TIMEOUT_SECONDS`) | 30 min (`FEEDBACK_QUESTION_TIMEOUT_SECONDS`) |
| `research_trace.begin_question` / `finalize` | called ([orchestrator.py:150](orchestrator.py#L150), [:265](orchestrator.py#L265)) | **never called** — so `evolution.md` is not written and `emit()` early-returns on an empty ContextVar |
| Non-open questions | always skipped | `FEEDBACK_ALLOW_NON_OPEN` escape hatch |
| `ENSEMBLE DEGRADED` warning | present ([orchestrator.py:234-241](orchestrator.py#L234-L241)) | **absent** |
| `forecast.json` `architecture` key | absent | `"feedback_loop"` |

The `research_trace` gap is total: **no file under `feedback_loop/` imports `research_trace`**, and
`begin_question`/`finalize` have exactly two call sites, both in `orchestrator.py` (lines 150 and
265). Since `emit()` early-returns on an empty `_ACTIVE` ContextVar
([research_trace.py:95-97](research_trace.py#L95-L97)), the shared code that *does* emit (scrape,
extract, compiler, precompress) silently writes nothing during a feedback-loop run. The feedback
architecture produces **no** `trace/` folder and **no** `evolution.md`.

### 6.5 The Firecrawl → Crawl4AI → Wayback scrape ladder exists twice

- General research: [serp_research.py:989-1153](research/serp_research.py#L989-L1153)
- Resolution path: [resolution_criteria_scraper.py:826-892](resolution_criteria_scraper.py#L826-L892)

They differ in ways that matter:

| | general | resolution |
|---|---|---|
| Firecrawl `max_age_ms` | `GENERAL_MAX_AGE_MS` (6 h) | `RESOLUTION_MAX_AGE_MS` (1 h) |
| Firecrawl `priority` | `False` | `True` |
| Firecrawl gated by | `ENABLE_FIRECRAWL_GENERAL_SCRAPE` | nothing (always tried) |
| Content cap | `_MAX_SCRAPE_CHARS` = 18 000 | `_CRAWL4AI_CONTENT_BUDGET` = 100 000 |
| Adapter dispatch | full registry via `find_adapter` | **only** GoogleSheets + YahooQuotes, hand-coded |
| Budget-exceeded handling | catches `FirecrawlBudgetExceededError` explicitly | falls into the generic `except Exception` |

Consequence of the adapter row: a resolution-criteria URL pointing at a **PDF**, a **Wikipedia**
page, a **Google Trends** URL, or a **Metaculus** question does *not* get its adapter — it goes
straight to Firecrawl/Crawl4AI. The `PdfAdapter` exists specifically because browser scrapers
return nothing for PDFs, yet the resolution path (the one place a PDF is most likely to *be* the
resolution source) never reaches it.

### 6.6 Three near-identical search-provider modules

`serp_research.py`, `tavily_research.py`, `firecrawl_research.py` each contain their own copy of:

- `_queries_with_title` — **byte-identical** in all three
  ([serp:1417](research/serp_research.py#L1417), [tavily:512](research/tavily_research.py#L512),
  [firecrawl:552](research/firecrawl_research.py#L552))
- `_dedupe_results` — identical modulo the result type
  ([serp:1430](research/serp_research.py#L1430), [tavily:525](research/tavily_research.py#L525),
  [firecrawl:576](research/firecrawl_research.py#L576))
- `_build_ranking_prompt` — ~65 lines each, differing only in the provider name and which
  metadata fields are printed ([serp:765](research/serp_research.py#L765),
  [tavily:420](research/tavily_research.py#L420), [firecrawl:459](research/firecrawl_research.py#L459))
- `_validate_*_key`, `_coerce_optional_int`, `_normalise_report_heading`

**Behavioural divergence inside the duplication:** serp and tavily both emit two `research_trace`
events (`search_results` before ranking, `rank` prompt + `rank` groups). Firecrawl emits **none** —
`rank_firecrawl_urls` ([firecrawl_research.py:256-287](research/firecrawl_research.py#L256-L287))
has no `research_trace.emit` calls at all, and `build_firecrawl_research_result` never emits
`search_results`. So when the chain falls through to Firecrawl, `evolution.md` loses the search and
ranking stages entirely.

Second divergence: `_normalise_report_heading` in both tavily and firecrawl rewrites
`"# SerpAPI Scraped Research"` in the extract output ([tavily:543](research/tavily_research.py#L543),
[firecrawl:593](research/firecrawl_research.py#L593)) — but the shared extract prompt
([serp_research.py:1235](research/serp_research.py#L1235)) only says *"Start it with a `# ` heading"*
and never mentions "SerpAPI Scraped Research". The rename is a no-op against the current prompt.

### 6.7 Three near-identical prediction-market modules

`kalshi_research.py`, `manifold_research.py`, `polymarket_research.py` each carry their own copy of
`_STOP_WORDS`, `_extract_keywords`, `_unique_nonempty`, `_safe_float`, `_get_openai_client` +
module-global `_openai_client`, `_generate_search_queries` (same prompt, provider name swapped),
`_score_markets`/`_score_events` (same 0-10 rubric), `_fmt_volume`, and `scrape_*`.

Divergences inside the duplication:
- Kalshi's `_STOP_WORDS` has 8 extra entries (`after, during, how, many, much, through, what,
  which`) that Manifold's and Polymarket's lack, which are otherwise identical to each other
  ([kalshi:58-66](research/kalshi_research.py#L58-L66) vs
  [polymarket:54-61](research/polymarket_research.py#L54-L61)).
- Kalshi's `_extract_keywords` uses `\b[\w'-]+\b`; the other two use `\b\w+\b`.
- Kalshi fetches **all** open markets (3 × 1000) and filters locally with a hand-rolled scoring
  function; the other two use the provider's own search endpoint.
- All three modules do `sys.path.insert(0, _REPO_ROOT)` at import time
  ([kalshi:28-30](research/kalshi_research.py#L28-L30) and equivalents) — a leftover from being
  runnable as scripts, executed on every import.

### 6.8 Four copies of the tolerant-JSON extractor

- `research/pipeline._extract_json_object` ([:951](research/pipeline.py#L951))
- `research/evidence_plan._extract_json_object` ([:122](research/evidence_plan.py#L122))
- `feedback_loop/gap_analysis._extract_json_object` ([:268](feedback_loop/gap_analysis.py#L268)) —
  its docstring explicitly says it is a copy "so this package never imports private helpers"
- plus two copies of the *object-or-array* variant: `serp_research._extract_json_value`
  ([:1504](research/serp_research.py#L1504)) and `query_maker._extract_json_value`
  ([:235](query_maker.py#L235))

All five are the same algorithm (`json.loads`, then scan for the first `{`/`[` that `raw_decode`s).
Only the error message differs.

### 6.9 Two token-accounting stories in one file

`monetary_cost_manager.CHARACTERS_PER_TOKEN = 3.2` ([:31](monetary_cost_manager.py#L31)), but the
`MonetaryCostManager` class docstring 150 lines below still states *"Tokens are estimated with the
coarse rule requested for this project: 1 token = 4 characters"*
([:182-183](monetary_cost_manager.py#L182-L183)). Every budget constant in the file is denominated
in 3.2-char units.

Relatedly: `OPENROUTER_COST_HARD_LIMIT_USD` is a **token** count, not USD
([config.py:88](config.py#L88), acknowledged in `.env.example`), and the CLI exposes it as
`--cost-limit`/`--token-limit` while `forecasting_bot.py:185` prints it as
`"OpenRouter per-question estimated-token hard limit"`. Three names for one number.

### 6.10 Two ways of enforcing the budget that disagree on failure mode

- `raise_error_if_limit_would_be_reached` — pre-flight, **raises** `HardLimitExceededError`
  ([monetary_cost_manager.py:370](monetary_cost_manager.py#L370)), called on every OpenRouter call.
- `would_breach_input_reserve` — advisory, **never raises**
  ([monetary_cost_manager.py:346](monetary_cost_manager.py#L346)), called at 3 sites.

They read different thresholds (hard limit vs limit-minus-reserve), so a call can pass the soft
gate and then be hard-refused mid-provider. The compiler swallows that refusal and falls back to
the heuristic brief ([compiler.py:172-183](compiler.py#L172-L183)); `run_provider` deliberately
re-raises it ([research/pipeline.py:88-89](research/pipeline.py#L88-L89)); the forecasters catch it
as a generic exception and drop the run ([forecasters/base.py:176](forecasters/base.py#L176)).
Three different policies for the same exception.

### 6.11 Truncation strategy is inconsistent across the pipeline

The codebase has clearly been migrating away from head-truncation (`[:N]`), and the migration is
partial:

| Site | Strategy |
|---|---|
| Compiler research sections | LLM compression, then **visible** marker ([compiler.py:493](compiler.py#L493)) ✅ |
| Compiler market sections | silent `_truncate_text` at 24 000 ([compiler.py:278](compiler.py#L278)) |
| Artifact check, per provider | silent head cut at **8 000** ([research/pipeline.py:903](research/pipeline.py#L903)) |
| Extract prompt | silent head cut at 90 000 ([serp_research.py:1190](research/serp_research.py#L1190)) |
| Per-page scrape | silent head cut at 18 000 ([serp_research.py:942](research/serp_research.py#L942) et al.) |
| Raw-research view | cut at 200 000 **with** an in-band notice ([research/pipeline.py:701-707](research/pipeline.py#L701-L707)) ✅ |
| Resolution sources | **batched**, never cut ([resolution_criteria_scraper.py:624](resolution_criteria_scraper.py#L624)) ✅ |
| Research context join | silent cut at 18 000 with a short note ([research/pipeline.py:972-981](research/pipeline.py#L972-L981)) |

The 8 000-char artifact-check cut is the smallest budget applied to the largest inputs, and it is
the one that decides the authoritative `MISSING/PARTIAL/COMPLETE` banner every forecaster reads.

### 6.12 `options` is threaded everywhere but never supplied

`run_serp_research` / `run_tavily_research` / `run_firecrawl_research` all accept
`options: list[str] | None = None` and forward it to `generate_google_search_query_plan`, which
renders it into the query-generation prompt ([query_maker.py:96](query_maker.py#L96)). **No caller
ever passes it** — `research/pipeline.py`'s `serpapi_call`/`tavily_call`/`firecrawl_call` omit the
argument ([research/pipeline.py:143-175](research/pipeline.py#L143-L175)), as does
`feedback_loop/research_pipeline.py`. So for multiple-choice questions the option list never
reaches query generation, and the prompt always renders `"Not applicable."`.

### 6.13 Variable shadowing in two forecasters

[forecasters/binary.py:279](forecasters/binary.py#L279) and
[forecasters/multiple_choice.py:312](forecasters/multiple_choice.py#L312) both rebind `models`
(the ensemble model assignment returned by `heterogeneous_run_setup`) to an empty list that then
collects the models of *valid* runs. Functionally fine today because the original value is consumed
by `gather_forecast_runs` on the preceding line — but the two variables mean different things and
share a name, and `numeric.py` does **not** do this (it uses `kept_records`).

### 6.14 Duplicate ensemble bookkeeping, three shapes

Each forecaster builds its own `ensemble: list[dict]` record with slightly different keys:
binary adds `probability`; MC adds nothing; numeric adds `used_fallback`, `off_grid`,
`coarse_support_repaired`, `error`. Consumers of `forecast.json` must special-case per type.
Similarly `run_values` is `list[float]` (binary), `list[dict[str, float]]` (MC), or
`list[dict]` of parsed distribution specs (numeric).

### 6.15 Two "compiled brief" formats

`compiler._build_compiler_prompt` asks for sections
`Extracted Artifact Rows / Resolution Mechanics / Key Evidence / Balance Check / Derived
Implications / Market Signals / Gaps And Cautions` ([compiler.py:765-822](compiler.py#L765-L822)).
`_build_heuristic_report` — the fallback that ships whenever the LLM pass fails or the budget
refuses it — emits a **completely different** section set:
`Key Facts And Evidence / Required Evidence Artifact / Direct Evidence / Near Proxy Evidence /
Weak Proxy Evidence / Background Color / Market Signals / Resolution Source Findings / News And
External Evidence / Other Provider Output / Uncertainties And Gaps`
([compiler.py:859-921](compiler.py#L859-L921)).

Every forecaster prompt instructs the model to cite `[E#]` IDs and to read the "Balance Check" and
"Required Artifact Status" sections. The heuristic brief contains **no `[E#]` labels at all** —
`_select_key_evidence` emits bare bullet lines ([compiler.py:925-947](compiler.py#L925-L947)).
So a budget-refused compile silently hands the forecasters a brief whose citation contract they
cannot satisfy, while the prompt still demands `[E#]` citations for every adjustment.

### 6.16 Duplicated Wayback fallback block

[serp_research.py:1110-1132](research/serp_research.py#L1110-L1132) and
[resolution_criteria_scraper.py:874-888](resolution_criteria_scraper.py#L874-L888) are the same
try/except around `wayback_module.snapshot_fallback_text`, with independent logging strings.

### 6.17 Both hourly cron workflows are live simultaneously

`main.yaml` (GitHub-hosted) and `main_ec2.yml` (self-hosted) have **identical** schedules
(`4,24,44 * * * *`) and different concurrency groups. If a self-hosted EC2 runner is registered,
both fire every 20 minutes against the same `METACULUS_TOKEN`, differing only in tournament list
(`fall-2026-ai` vs `fall-2026-ai minibench`). Nothing in either workflow disables the other.

### 6.18 Same fact scraped twice per question, by design and by accident

`scrape_resolution_sources` extracts URLs from *both* the resolution criteria *and* the question
background/fine-print ([resolution_criteria_scraper.py:1144-1150](resolution_criteria_scraper.py#L1144-L1150)).
The search providers then independently rank and scrape URLs that may include the same pages. The
`claim_scrape_url` registry prevents a second *fetch*, but the resolution scraper's copy is
summarized by an LLM while the research copy goes through the extract stage — the same page can
therefore appear twice in the compiler input under two different section names, with two different
summaries.

---

## 7. Things I can't determine

Open questions — I would be guessing.

1. **Which architecture is actually in production.** `feedback_loop/` is gitignored, so the
   committed workflows can only run the main orchestrator. But the package is complete and has its
   own CLI. I cannot tell from code whether you run `feedback_loop/run_bot.py` manually.

2. **Whether the EC2 self-hosted runner exists.** `main_ec2.yml` targets
   `[self-hosted, linux, x64, ec2-bot]`. If no such runner is registered the job queues and
   eventually fails; if one is, §6.17's double-run is live. Not determinable from the repo.

3. **Whether `openai/gpt-5.6-sol` is reachable on the configured OpenRouter key.** `FORECASTER_MODELS`
   defaults to a two-model pool, but nothing in the code verifies model availability; a 404 from
   OpenRouter would surface as a dropped ensemble run
   ([forecasters/base.py:174-185](forecasters/base.py#L174-L185)). The `config.py:43-47` comment
   mentions a BYOK Google key with a daily limit of 0 for Gemini, which suggests model reachability
   has been a live issue.

4. **Whether the binary all-runs-fail default of 0.5 is intended to be submitted.** Numeric abstains
   (`forecast=None` → nothing submitted); binary returns `0.5` and MC returns a uniform, both of
   which the orchestrator treats as valid forecasts and submits. This is either a deliberate
   asymmetry or an oversight — the code carries no comment either way.

5. **What `JINA_API_KEY` and `GITHUB_API_TOKEN` in `.env` were for.** No reader exists in the
   current tree; I cannot tell whether they are leftovers or belong to a tool outside this repo.

6. **Whether the 50-post cap in `list_posts_from_tournament` is deliberate.** There is no pagination
   and no comment. If a tournament ever has >50 open questions the tail is silently invisible, but
   `order_by=-hotness` suggests someone may have intended a top-N selection.

7. **Whether `docs/runs/` artifacts are ever collected.** The workflows upload them as GitHub
   Actions artifacts with 14-day retention, and `.gitignore` excludes `docs/`. `score_forecasts.py`
   reads them from disk. Whether there is a downstream collection step, I can't see.

8. **The intended lifetime of `_exhausted_search_providers`.** It is a module-global set
   ([research/pipeline.py:564](research/pipeline.py#L564)) shared across all questions in a process
   and never cleared. For a batch run that is presumably the point (don't re-probe a dead key), but
   it also means one transient auth blip early in a batch disables that provider for every later
   question. The comment says only non-recoverable signals get added, and the marker list includes
   the very broad `"missing "` ([research/pipeline.py:559](research/pipeline.py#L559)) — I can't
   tell whether the breadth is intentional.

9. **Whether `AskNewsCache` from `forecasting-tools` respects `no_cache` the way the code assumes.**
   `get_formatted_news_async` calls `self.cache.get(query)` unconditionally
   ([research/asknews_research.py:139](research/asknews_research.py#L139)) regardless of
   `cache_mode`; whether `no_cache` makes that a guaranteed miss is a property of the external
   library, which I did not read.

10. **Whether `heterogeneous_run_setup` ignoring its caller's `models` is intended.** It calls
    `models_for_runs(num_runs, None)` ([forecasters/base.py:105](forecasters/base.py#L105)), which
    falls back to the configured pool. Today every caller passes `None` anyway, so it is
    unobservable — but the parameter exists on `gather_forecast_runs`, so a future caller supplying
    models would find them silently discarded.

11. **Whether the `Scrape.cycle=0` placeholder is a bug or a convention.** It is always overwritten
    (§5.12), so it has no effect today; I can't tell if the rebuild was added later to fix
    something or was always the design.

12. **Whether the two root-level folders `44805_…` and `44807_…` are inputs to anything.** They
    contain `postmortem-*.md` files alongside normal run artifacts and are explicitly gitignored by
    name. Nothing in the code reads from the repo root, so they look like manual analysis copies —
    but I cannot rule out an external script.
