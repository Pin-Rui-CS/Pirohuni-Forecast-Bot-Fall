# Prompt Templates

Every prompt template in the bot, one per file, extracted **verbatim from source**.

- **33 templates**, 110,950 chars of scaffold in total.
- Bodies are AST-extracted from the Python source, so the text here is byte-identical
  to what the code holds. Slots appear exactly as the code writes them — a plain
  `{title}` for `.format()` templates, and the literal expression (e.g.
  `{resolution_criteria or 'Not provided.'}`) for f-string-built prompts.
- Files marked ᶠ are **fragments**: text appended to or formatted into another prompt
  at runtime, never sent on its own. Each names its parent.

## Model tiers

Model names carry a role, and `llm_provider.resolve_model` maps them onto whichever
provider `LLM_PROVIDER` selects:

| Tier | OpenRouter | OpenAI | Used for |
| --- | --- | --- | --- |
| **Tier 1** | `anthropic/claude-opus-5` | `gpt-5.6-sol` | Forecasting, compiling, tiebreaking |
| **Tier 2** | `anthropic/claude-sonnet-5` | `gpt-5.6-terra` | All research and utility steps |

Counts: **2** Tier 1, **19** Tier 2, **6** split across both, **6** never called.

The 6 split prompts are the 3 forecaster templates and their 3 repair fragments: the
ensemble sends runs 1–2 to Tier 1 on the compiled brief and run 3 to Tier 2 on the raw
research, and a repair retry inherits whichever model its failed run used.

## 1. Active — runs on every question (17)

| Prompt | Tier | Scaffold | Source |
| --- | --- | ---: | --- |
| [Evidence Plan](01-active/evidence-plan-prompt.md) | T2 | 2,950 | [`research/evidence_plan.py:74`](../research/evidence_plan.py#L74) |
| [AskNews Filter](01-active/asknews-filter-prompt.md) | T2 | 2,392 | [`research/asknews_filter.py:149`](../research/asknews_filter.py#L149) |
| [Search Query Generation](01-active/query-generation-prompt-template.md) | T2 | 1,271 | [`query_maker.py:34`](../query_maker.py#L34) |
| [SerpAPI URL Ranking](01-active/serp-ranking-prompt.md) | T2 | 2,532 | [`research/serp_research.py:984`](../research/serp_research.py#L984) |
| [Per-Cycle Scrape Extract](01-active/serp-extract-prompt.md) | T2 | 3,116 | [`research/serp_research.py:1560`](../research/serp_research.py#L1560) |
| [Artifact Check (research audit)](01-active/artifact-check-prompt.md) | T2 | 4,607 | [`research/pipeline.py:857`](../research/pipeline.py#L857) |
| [Kalshi Search Query Generation](01-active/kalshi-query-generation-prompt.md) | T2 | 677 | [`research/kalshi_research.py:80`](../research/kalshi_research.py#L80) |
| [Kalshi Relevance Scoring](01-active/kalshi-relevance-scoring-prompt.md) | T2 | 575 | [`research/kalshi_research.py:343`](../research/kalshi_research.py#L343) |
| [Manifold Search Query Generation](01-active/manifold-query-generation-prompt.md) | T2 | 627 | [`research/manifold_research.py:77`](../research/manifold_research.py#L77) |
| [Manifold Relevance Scoring](01-active/manifold-relevance-scoring-prompt.md) | T2 | 585 | [`research/manifold_research.py:251`](../research/manifold_research.py#L251) |
| [Polymarket Search Query Generation](01-active/polymarket-query-generation-prompt.md) | T2 | 615 | [`research/polymarket_research.py:76`](../research/polymarket_research.py#L76) |
| [Polymarket Relevance Scoring](01-active/polymarket-relevance-scoring-prompt.md) | T2 | 579 | [`research/polymarket_research.py:256`](../research/polymarket_research.py#L256) |
| [Resolution Source Summary](01-active/resolution-summary-prompt.md) | T2 | 2,363 | [`resolution_criteria_scraper.py:245`](../resolution_criteria_scraper.py#L245) |
| [Research Compiler (evidence brief)](01-active/compiler-prompt.md) | T1 | 14,973 | [`compiler.py:762`](../compiler.py#L762) |
| [Binary Forecaster](01-active/binary-prompt-template.md) | T1×2 + T2×1 | 17,249 | [`forecasters/binary.py:22`](../forecasters/binary.py#L22) |
| [Multiple Choice Forecaster](01-active/multiple-choice-prompt-template.md) | T1×2 + T2×1 | 13,566 | [`forecasters/multiple_choice.py:18`](../forecasters/multiple_choice.py#L18) |
| [Numeric Forecaster](01-active/numeric-prompt-template.md) | T1×2 + T2×1 | 16,931 | [`forecasters/numeric.py:24`](../forecasters/numeric.py#L24) |

Pipeline order:

```
AskNews  →  asknews-filter  →  evidence-plan  →  query-generation  →  serp-ranking
   →  serp-extract  (×3 scrape cycles)  →  artifact-check  →  compiler  →  forecaster (×3)

in parallel:  resolution-summary (per URL)
in parallel:  kalshi / manifold / polymarket  ×  (query-gen + scoring)  =  6 calls
```

## 2. Conditional — only on a trigger or fallback path (10)

| Prompt | Tier | Scaffold | Source |
| --- | --- | ---: | --- |
| [Section Pre-Compression](02-conditional/precompress-prompt.md) | T2 | 1,147 | [`compiler.py:392`](../compiler.py#L392) |
| [Tavily URL Ranking](02-conditional/tavily-ranking-prompt.md) | T2 | 2,531 | [`research/tavily_research.py:444`](../research/tavily_research.py#L444) |
| [Firecrawl URL Ranking](02-conditional/firecrawl-ranking-prompt.md) | T2 | 4,082 | [`research/firecrawl_research.py:561`](../research/firecrawl_research.py#L561) |
| [Wikipedia Adapter Extract](02-conditional/wikipedia-extract-prompt.md) | T2 | 2,030 | [`Adapters/Wikipedia.py:121`](../Adapters/Wikipedia.py#L121) |
| [Wayback Snapshot History](02-conditional/wayback-history-prompt.md) | T2 | 1,445 | [`resolution_criteria_scraper.py:396`](../resolution_criteria_scraper.py#L396) |
| [Artifact Check — Prior Check Section](02-conditional/artifact-check-prior-section.md) ᶠ | T2 | 589 | [`research/pipeline.py:917`](../research/pipeline.py#L917) |
| [Binary Repair Instruction](02-conditional/binary-repair-instruction.md) ᶠ | T1×2 + T2×1 | 128 | [`forecasters/binary.py:244`](../forecasters/binary.py#L244) |
| [Multiple Choice Repair Instruction](02-conditional/mc-repair-instruction.md) ᶠ | T1×2 + T2×1 | 186 | [`forecasters/multiple_choice.py:296`](../forecasters/multiple_choice.py#L296) |
| [Numeric Repair Instruction](02-conditional/numeric-repair-instruction.md) ᶠ | T1×2 + T2×1 | 273 | [`forecasters/numeric.py:2403`](../forecasters/numeric.py#L2403) |
| [Binary Tiebreaker / Synthesis](02-conditional/binary-tiebreaker-prompt.md) ᶠ | T1 | 584 | [`forecasters/binary.py:334`](../forecasters/binary.py#L334) |

## 3. Inactive — dead code or parallel architecture (6)

| Prompt | Tier | Scaffold | Source |
| --- | --- | ---: | --- |
| [Legacy Binary Prompt (tool-era)](03-inactive/legacy-binary-prompt-template.md) | — | 4,902 | source deleted (the `run_python_code` tool loop) |
| [Resolution Source Compile](01-active/resolution-compile-prompt.md) | — | 1,035 | source deleted; was reachable only from the legacy resolution scraper |
| [Dead Resolution Page Summary](03-inactive/dead-resolution-summary-prompt.md) | — | 3,299 | [`resolution_criteria_scraper.py:306`](../resolution_criteria_scraper.py#L306) |
| [Chain Round — Gap Analysis Section](03-inactive/chain-gap-analysis-section.md) ᶠ | — | 1,604 | [`feedback_loop/gap_analysis.py:33`](../feedback_loop/gap_analysis.py#L33) |
| [Chain Round — Prior Round Appendix](03-inactive/prior-round-appendix-template.md) ᶠ | — | 1,059 | [`feedback_loop/gap_analysis.py:233`](../feedback_loop/gap_analysis.py#L233) |
| [Chain Round — Research Addendum Header](03-inactive/research-addendum-header-template.md) ᶠ | — | 448 | [`feedback_loop/research_rounds.py:35`](../feedback_loop/research_rounds.py#L35) |

`feedback_loop/` is a complete second architecture that coexists with the live one; nothing
outside the package references it, so none of its prompts run today.

## Things this compilation makes visible

1. **The tier split is stark.** Every research step that decides *what evidence exists* is
   Tier 2; Tier 1 only sees what those steps chose to pass on. The compiler and forecaster
   are the sole Tier 1 consumers, and 2 of 3 forecaster runs read only the compiler's output.

2. **The ranking family has drifted.** `serp` and `tavily` rankers are effectively the same
   ~2.5K text; `firecrawl`'s is ~4.1K and is the *only* one carrying the date-discipline
   paragraph about undated results. That rule sits on the rung that almost never runs.

3. **Six market calls, three copies each.** The Kalshi / Manifold / Polymarket query-gen and
   scoring prompts are near-identical, differing mainly in platform name and punctuation.

4. **Tiebreaking is binary-only.** `SPREAD_THRESHOLD = 30` triggers a Tier 1 synthesis pass
   for binary questions. Numeric questions — where disagreeing runs smear the aggregated CDF,
   since `quantile_average_cdfs` does no regime matching — have no equivalent.

5. **The forecaster prompts demand arithmetic they cannot perform.** They ask for shown
   calculations throughout, while `gather_forecast_runs` hard-codes `use_tools=False`. The
   inactive [legacy prompt](03-inactive/legacy-binary-prompt-template.md) was written when
   the `run_python_code` tool was live and says "never do mental arithmetic".

6. **Prompt length is not where the tokens are.** The largest scaffold is ~17K chars, but
   filled prompts reach 120K–200K+ because of insert budgets (`_COMPILER_INPUT_BUDGET_CHARS`,
   `_MAX_EXTRACT_INPUT_CHARS`, `_RAW_VIEW_MAX_CHARS`). Editing scaffolds barely moves cost;
   editing budgets does.

7. **The binary prompt has structure the other two lack** — the EVENT CHAIN block, the
   Directional audit, and the Discount ledger appear only in `binary`, in condensed form or
   not at all in `multiple_choice` and `numeric`.

---

*Template blocks were extracted from source; the headers around them are hand-written. If you
edit a prompt in the code, the corresponding `## Template` block here goes stale — this folder
is a review surface, not a source of truth.*
