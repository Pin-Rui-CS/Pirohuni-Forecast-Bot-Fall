# Per-Task Breakdown — Q44879 & Q44880

Every task in both runs: what fed it, how big, where it went, what processed it, and
whether it did its job. Built by joining `audit.md`'s OpenRouter table to `trace/trace.jsonl`,
then verifying claims against the payload files and the source.

- **Audit #** = row number in `audit.md` → `usage_yaml_table` (completion order).
- **Trace** = `trace/NNN_*.md` payload file (logical order).
- Rows are listed in **pipeline order**, not audit order, so the two columns diverge.
- **★** = did this step do its job, and was it worth its cost. Not "was the output nice."

> **Reading the "In" column.** `audit.md` records
> `count_serialized_characters(request_payload)` = `len(json.dumps(payload))`
> ([monetary_cost_manager.py:411](monetary_cost_manager.py#L411)) — **not** the prompt length.
> It counts the `{"messages":[{"role":"user",…}]}` wrapper and the JSON escaping, where every
> newline and every `"` costs one extra character. On the artifact check that is 673 newlines +
> 178 quotes + ~87 wrapper = **938 chars, ~1.4%**. Verified: predicted 69,584 vs recorded 69,581.
> So every "In" figure below runs ~1–1.5% above the real prompt, scaling with newline density.

> **Naming.** "**Section**" means a whole provider block as recorded in `trace.jsonl`
> (Firecrawl Search = 62,507). "**Search-results block**" means the part inside it labelled
> `Raw Firecrawl search results considered:` (42,648). They are nested, not alternatives.

---

## Corrections from my first pass

| # | What I said | What's actually true |
|---|---|---|
| 1 | AskNews absent from the tables | **Omission.** AskNews is the 2nd-largest section in *both* compiler inputs. It makes zero OpenRouter calls, so it has no audit row and my join couldn't see it. Rows added below. |
| 2 | 44879 evidence plan "didn't list the CSV despite it being in the already-scraped page" | **Void.** The evidence plan never sees the scrapes — the resolution scraper is a parallel task, deliberately kept off its critical path ([pipeline.py:204](research/pipeline.py#L204)). |
| 3 | 44879 query-gen "never generated a query for the CSV/feed" | **Re-attributed.** The evidence plan contributes **zero bytes** to query-gen (see Finding B). The call was asked to plan searches without the plan. |
| 4 | 44880's Metaculus FAQ "96K dominates the summariser input" | **Overstated.** Capped at `_BACKGROUND_CONTENT_CHARS = 20_000` before the LLM. Still ~20% of a 100K call for zero value, but not 96K. |
| 5 | 44879 markets "excluded — correct answer" | **Understated.** Manifold and Polymarket found **zero markets** (6-char scoring outputs). The audit's "1 candidate" each is a logging artifact — all three are the CISA resolution URL scraped out of the echoed query. |
| 6 | Wayback history ★5 | **★4.** The output was the run's best evidence, but CDX had listed up to 18 monthly captures and `max_snapshots=4` discarded ~14 before fetching — see Finding A. |
| 7 | URL ranking listed, but nothing that *produced* the URLs | **Omission.** The **Firecrawl `/v2/search` execution** and the **Tavily attempt that failed before it** are both real tasks with no audit row and no trace stage. Rows added to Phase 4/5. See Finding G. |
| 8 | Finding B: the evidence plan's "only reader is the compiler" | **Wrong.** It has three readers — market providers, `artifact-check` (twice over), and the compiler. The severance is specifically from **web query generation**, the one consumer that decides what gets scraped. See Finding B and A.3. |
| 9 | A.3: "Firecrawl surrenders 42–50% of its middle — that is where the extract reports live" | **Wrong for 44879** (extract report survives whole; 100% of the elision hits the search-results block), **right for 44880** and consequential — the elided 6,507 chars are exactly the 2026 poll table. See A.3. |
| 10 | Every "In" figure read as prompt length | **They are JSON-serialized payload lengths**, ~1–1.5% above the prompt. A.1's per-candidate 700 → ~686; A.2's per-packet ~900 → ~750–800. See preamble. |
| 11 | Retry search = "5 queries = title + 4 `retry_queries`" | **4 queries.** `_queries_with_title` runs only in the non-preset branch, so the retry prepends no title and generates nothing — the 4 come verbatim from artifact-check v1. 44879's search-call total is 13, not ~14. See Finding G. |
| 12 | Provider **sections** had no rows at all — only the steps feeding them | **Omission.** Trace `016`/`017` (44879) and `015`/`016` (44880) were referenced nowhere, and the retry's 83,261-char section appeared only as a parenthetical. Section-assembly rows added to Phase 4 and Phase 5 — this is the unit the compiler, artifact check and raw-research forecaster actually consume. |
| 13 | *(withdrawn)* I claimed ranking #2 read ~40 URLs including resurfaced main-pass ones | **That revision was wrong; the original account stands.** `search_results` is the same list passed to `rank_firecrawl_urls` and rendered into the section ([firecrawl_research.py:165–207](research/firecrawl_research.py#L165)), and the rendered block has **25 entries** — so the ranker read 25, nothing from the main pass. The 112,326 is **description bulk alone**: ~4,320 chars per entry, with 6 descriptions later replaced by an 89-char note in the saved copy. See A.1. |
| 14 | A.5: "the compiler never sees the verdict; only the forecasters do" | **Wrong.** `_format_artifact_check` ([compiler.py:717](compiler.py#L717)) puts all five narrative fields into the compiler's prompt, with an instruction to carry `closest_available` forward into Key Evidence unless the research corrects it. What the compiler is *forbidden* is restating the **verdict** ([compiler.py:819](compiler.py#L819)) — that is the banner's exclusive job. The verdict reaches the forecaster twice, by two different routes. |
| 15 | Row 19 precompress ★2, "fit to 24,331 budget" | **★1 — it never fitted to the budget; it was cut off by `max_tokens` mid-URL at entry [5] of 25**, then labelled itself "all distinct claims retained". See Finding I. |

---

# Q44879 — CISA KEV additions, August 2026

`$2.183767 · 412,445 tokens · 23 LLM calls · 16 scrapes · 671.6 s (research 542.8 / forecast 128.8) · Firecrawl 15/25 credits`

**Submitted:** 7.75 / 20.45 / 34.4 / 26.4 / 11.0 across `17 or fewer` / `18–22` / `23–27` / `28–32` / `33 or more`

## Phase 1 — Resolution source (parallel task, off the evidence-plan path)

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| — | 001 | scrape KEV catalog | resolution URL | — | 41,315 | page-summary | firecrawl-scrape | — | fetch resolution source | Got the page, the CSV/JSON feed links, and `Showing 1 to 20 of 1656`. Only 20 of 1,656 rows are on the page | 3 |
| — | 002 | scrape "reducing significant risk" | resolution URL | — | 14,256 | page-summary | firecrawl-scrape | — | fetch 2nd resolution page | Background only, no counts | 3 |
| 2 | — | **wayback-history** | archive.org CDX + 4 captures | 64,601 | 1,703 | Resolution section | Sonnet 5 | $0.0687 | same-source flow rate | **Best evidence in the run** — produced 1,250 → 1,373 → 1,484 → 1,631 → 1,656, which became `[E2]` and the `[I1]` July cross-check. But sampled 4 captures 6 months apart for a *monthly* question (Finding A) | 4 |
| 10 | — | page-summary | scrapes 001+002, cleaned | 46,011 | 4,237 | Resolution section (6,489) | Sonnet 5 | $0.0527 | clean + structure resolution pages | Summarised correctly; never promoted the linked CSV to an artifact target | 3 |

**Why wayback (64,601) is bigger than page-summary (46,011):** wayback fetches 4 archived
copies of *one* page, uncleaned, each capped at `_MAX_SNAPSHOT_CHARS = 15_000` → 60,000 +
~4,600 prompt. Page-summary takes 2 *live* pages that were heuristically cleaned first
(`_clean_content`) → 55,571 raw becomes ~44,000 + prompt. Wayback does **not** read the scrapes;
it's an independent retrieval.

## Phase 2 — Planning

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 003 | evidence-plan | 4 question fields + AskNews[:12,000] | 15,792 | 5,706 | compiler input only | Sonnet 5 | $0.0339 | name the required artifact | Correctly named "monthly historical counts of KEV additions" with ideal sources incl. the CSV/JSON feed. **Severed at both ends** (Finding B) — blind to the resolution scrapes, and dropped from query-gen | 4 |
| 6 | — | google-query-generation | AskNews[:14,000] + question fields | 16,190 | 1,553 | Firecrawl Search | Sonnet 5 | $0.0186 | build 8 SERP queries | Yielded 94 candidates. Wrote queries from news articles alone — **the evidence plan contributed 0 bytes** | 3 |

**Evidence-plan input, traceable:** `title` / `resolution_criteria` / `description` / `fine_print`
all from `forecast.json → question_details` (fine_print was empty → literal `"Not provided."`),
plus `trace/021_provider_AskNews.md` head-truncated to `_MAX_ASKNEWS_CHARS = 12_000`, plus
~2,600 chars of fixed template ([evidence_plan.py:70](research/evidence_plan.py#L70)).
AskNews is ~76% of the input.

## Phase 3 — Prediction markets — **zero markets found, nothing reached a forecaster**

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 3 / 4 / 5 | — | search-query-gen ×3 | question + evidence plan | 4,801 / 4,751 / 4,739 | 104 / 86 / 104 | market APIs | Sonnet 5 | $0.0121 | build market queries | ran | 1 |
| 8 | — | kalshi/relevance-scoring | candidates | 11,473 | 61 | — | Sonnet 5 | $0.0099 | score markets | ~6.6K more input than the others — consistent with having had real candidates and rejecting them all | 1 |
| 7 / 9 | — | manifold / polymarket scoring | candidates | 4,869 / 4,851 | **6 / 6** | — | Sonnet 5 | $0.0073 | score markets | 6-char outputs = empty list. Nothing to score | 1 |
| — | 018/019/020 | Kalshi / Manifold / Polymarket sections | — | — | 4,442 / 4,322 / 4,314 | **discarded** | — | — | market evidence | Each payload is `"No sufficiently relevant X markets found for: <echo of query>"` — ~95% echo. All three **excluded**; no market section in the compiler input | 1 |

**Audit logging bug:** the per-tool summary reads `Kalshi 1 / Manifold 1 / Polymarket 1` candidates.
All three are the same URL — `https://www.cisa.gov/known-exploited-vulnerabilities-catalog` —
harvested out of the echoed query text by the source ledger. **True market candidate count: 0.**

## Phase 4 — Search, scrape, extract (main pass)

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| — | 015 | **Tavily Search** | query plan (memoized) | — | 34 | *(fell through)* | Tavily API | $0 | first provider in the chain | `"Tavily Search: no usable results"`. Zero results → ranking skipped (`if not results: return []`), so no LLM spend. Chain fell through to Firecrawl. SerpAPI was not in the chain at all | 1 |
| — | — | **Firecrawl Search (main pass)** | 9 queries = title + 8 generated | — | **69 unique candidates** | url-ranking | Firecrawl `/v2/search` | not credit-tracked | **generate the candidate URLs** | 9 POSTs, `sources=("web","news")`, `per_source_limit = ceil(10/2) = 5` → ≤10 results/query, ≤90 raw, deduped to 69. Found the CISA alert pages and the aggregators; never surfaced the CSV feed | 4 |
| 11 | — | firecrawl-url-ranking | 69 candidates (`results[:80]`) | 51,478 | 7,852 | scrape queue | Sonnet 5 | $0.0962 | rank + group 34 URLs | 6 of the 14 pages it picked were never cited in the brief. Input is 94% candidate blocks — see Finding A.1 | 3 |
| — | 004–009 | scrapes cycle 1 ×6 | ranked URLs | — | 72,116 | extract | firecrawl / cache | — | fetch pages | 4 of 6 never cited | 3 |
| 12 | 010 | extract cycle 1 | 6 pages | 83,029 | 9,315 | cumulative report (8,882) | Sonnet 5 | $0.1079 | pull facts per group | base-rate group left **lacking** | 3 |
| — | 011 | scrape cycle 2 ×1 | ranked URLs | — | 4,285 | extract | firecrawl | — | fill base-rate gap | one 4.3K alert page | 2 |
| 13 | 012 | extract cycle 2 | 1 page | 19,579 | 10,185 | report (9,827) | Sonnet 5 | $0.0567 | re-extract | $0.057 to absorb one 4.3K page; **still lacking**; rewrites the whole cumulative report each cycle | 2 |
| — | 013 | scrape cycle 3 ×1 | ranked URLs | — | 4,922 | extract | firecrawl | — | fill base-rate gap | one 4.9K alert page | 2 |
| 14 | 014 | extract cycle 3 | 1 page | 21,184 | 11,215 | report (10,847) | Sonnet 5 | $0.0625 | re-extract | $0.063, **still lacking** after 3 cycles. Diffs show it deleting then re-adding its own "no monthly totals found" caveat — churn, not progress | 2 |
| — | **016** | **Firecrawl Search section assembly** | queries + ranked groups + cycles + extract report + all 69 search results | — | **62,507** | compiler input, artifact check, raw-research forecaster | `format_firecrawl_research` (code) | — | render one provider section | The unit everything downstream consumes. **8,983 scaffolding + 10,876 extract report + 42,648 search-results block.** Descriptions for the 8 scraped URLs are swapped for `SNIPPET_OMITTED_NOTE` here — see Finding H | 3 |
| — | 017 | Resolution Criteria Sources section | page-summary + wayback history | — | 6,489 | compiler input, artifact check | code | — | render resolution section | Smallest section, but priority 1 — the only one kept **whole** by the artifact-check fitter | 4 |
| — | 021 | **AskNews deepnews** | question | — | 21,302 | compiler input **+ query-gen seed** | AskNews API (no LLM) | $0 in OpenRouter | recent news digest | **21,405 chars = 18.6% of the compiler input, 2nd-largest section.** Earned it: produced `[E12]` — NVD 45,207, MS 642, Oracle 1,449, Google 433, *and* "the KEV catalog shows the number actually used in attacks has not risen," the discount that stopped the AI-surge narrative pushing the forecast up. But 14 URLs, **0 ranked, 0 scraped** — digest text only, never verified. Payload contains verbatim duplicate articles (Arista VCO story appears twice) | 4 |
| 15 | 022 | artifact-check v1 | all sections | 69,581 | 2,114 | retry gate + banner | Sonnet 5 | $0.0668 | status + retry queries | **Good** — `partial`, 4 well-targeted retry queries. `forecast_swing: moderate` is computed and never read (gate is status-only) | 4 |

## Phase 5 — Focused artifact retry (~$0.58 = 27% of the run)

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| — | — | **Firecrawl Search (retry) — the search execution** | **4 queries** from artifact-check v1's `retry_queries` | 4 queries (~340 chars) | **≤40 raw → 25 URLs, ≈108,000 chars of search-result text** | url-ranking #2 | Firecrawl `/v2/search` ×4 | not credit-tracked | **generate the retry candidate URLs** | 4 POSTs, `sources=("web","news")`, 5 per source → ≤10/query → ≤40 raw, deduped within this call to **25**. `preset_queries` **skips query generation entirely** — no LLM call, no title prepended ([firecrawl_research.py:148](research/firecrawl_research.py#L148)). Those 25 carry ~4,320 chars of description each — that bulk is what makes row 16 cost $0.138 and what later overflows the compiler budget. Surfaced securityonline, hivepro, senserva, gopher — the pages that finally produced a base rate | 4 |
| 16 | — | firecrawl-url-ranking #2 | **the same 25 retry candidates** — nothing carried over from the main pass | 112,326 | 6,638 | scrape queue | Sonnet 5 | $0.1383 | rank retry URLs | Most expensive ranking call in the run, entirely from **description bulk**: ≈**4,320 chars per candidate vs ~686** on the main pass, because the retry's report/statistics queries returned near-full-page Firecrawl descriptions. The saved section shows only 66,554 because 6 descriptions were later replaced by an 89-char note post-scrape — the ranker read them in full — see Finding A.1 | 3 |
| — | 023–028 | scrapes ×6 | ranked URLs | — | 84,073 | extract | firecrawl | — | fetch base-rate pages | 4 of 6 never cited | 3 |
| 17 | 029 | extract (retry) | 6 pages | 95,263 | 10,341 | report (10,305) | Sonnet 5 | $0.1302 | pull monthly counts | **Worked** — `lacking_groups: []`. Produced Apr 31 / May 21 / Jun 23 | 4 |
| — | **none** | **Focused Artifact Retry section assembly** | retry queries + ranked groups + cycle + extract report + all 25 search results | — | **83,261** | precompress → compiler input | `format_firecrawl_research` (code) | — | render the retry provider section | **1.33× the main-pass section from 36% as many candidates**, with a *smaller* extract report and *less* scaffolding — the whole excess is the search-results block at **80% of the section** (see breakdown below). This is what overflowed the 120K compiler budget and forced the $0.158 precompress. **No trace payload exists** — the 83,261 survives only as `input_chars` in the precompress meta | 2 |
| — | 030 | retry_decision | gate | — | 545 | log | code | — | record decision | fine | 4 |
| 18 | 031 | artifact-check v2 | all sections | 72,478 | 2,119 | banner | Sonnet 5 | $0.0776 | reconcile | Upgraded `closest_available` to real monthly figures; status stayed `partial` | 4 |

The retry is what turned "no base rate" into a base rate. It also paid third-party
aggregators for numbers derivable free from the CSV linked in a page scraped 3 times.

## Phase 6 — Compile & forecast

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 19 | 032 | compiler/precompress | retry section (83,261) | 85,569 | 19,118 | compiler input | Sonnet 5 | $0.1578 | asked for 24,331 | **HARD FAIL.** `max_tokens = 24,331//3 = 8,110` and native-out **hit 8,110 exactly** — output stops mid-URL at entry **[5] of 25**. 20 entries vanished with no truncation marker, under a banner claiming "all distinct claims retained". Also cost-negative: $0.158 spent to save $0.128 of Opus input. See Finding I | 1 |
| — | 033 | fit sections | 5 sections | — | 115,229 | compiler | code | — | assemble input | 178,930 → 114,934 + 295 separators. Budget correctly enforced; 2 of 3 precompress calls unused, `_visible_truncate` never fired | 3 |
| 20 | 034 | **compiler/research-brief** | 115,229 fitted | 134,756 | 15,437 | brief | **Opus 5** | $0.5130 | select + rank evidence | **Strong.** 7.5:1 compression, kept the Wayback series and the two-way July cross-check, correctly rejected VulnCheck's 495 as a basis mismatch | 4 |
| — | 035 | banner injection | artifact-check v2 | — | +2,757 → 18,194 | forecasters | code | — | prepend status | Deterministic override; 1,698 of the 2,757 is v2's text, 1,059 fixed scaffolding. Sound mechanism, but see Finding J on `closest_available` | 4 |
| 21 | — | mc-forecast | brief | 33,207 | **0** | — | Opus 5 | $0.00 | forecast | **HARD FAIL** — `JSONDecodeError` ([runs.md:282](44879_How_many_known_vulnerabilities_will_the_US_Cybersecurity__In/runs.md#L282)). Ensemble silently 3→2 | 0 |
| 22 | — | mc-forecast | brief | 33,207 | 12,587 | aggregator | gpt-5.6-sol | $0.2476 | forecast | ran | 4 |
| 23 | — | mc-forecast | **raw research** | 199,734 | 9,800 | aggregator | Sonnet 5 | $0.3259 | heterogeneous view | 6× the input of the brief runs, ~1.7× the cost of the brief-view run it replaces | 3 |

---

# Q44880 — Parties in the 2026 Kazakh election

`$1.629749 · 300,812 tokens · 18 LLM calls · 10 scrapes · 601.1 s (research 416.0 / forecast 185.1) · Firecrawl 8/25 credits`

**Submitted:** 21.83 / 24.73 / 22.7 / 15.83 / 12.6 / 2.3 across `Two or Fewer` … `Greater than Six`

## Phase 1 — Resolution source

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| — | 001 | scrape Wikipedia 2026 election | resolution URL | — | 99,963 | page-summary | firecrawl | — | fetch resolution source | Kept first 99,800 of 199,193 chars, discarded from the END. Low real loss (no results exist yet) — but the run then speculated about what was in the discarded half | 3 |
| — | 002 | scrape `Kurultai_(Kazakhstan` | description URL | — | 5,170 | page-summary | firecrawl | — | fetch description source | **Bug** — closing paren dropped from the URL; returned a "page does not exist" error page, which entered the brief as a Direct Evidence source | 1 |
| — | 003 | scrape metaculus.com/faq | fine-print link | — | 96,236 | page-summary | firecrawl | — | resolve "credible sources" | Pure boilerplate. Capped to 20,000 before the LLM, so ~20% of a 100K call plus a wasted Firecrawl credit | 1 |
| 2 | — | page-summary | 3 pages, cleaned + tier-capped | 100,552 | 4,085 | Resolution section (4,312) | Sonnet 5 | $0.0907 | clean resolution pages | Input was FAQ boilerplate + a resultless page + an error page. **First place the wrong "only two parties above 5%" verdict enters the pipeline** | 2 |
| — | — | *(wayback-history)* | — | — | — | — | — | — | — | **Never ran, no row anywhere.** `_build_wayback_history_section` returns `""` silently on fetch failure *or* <2 captures. Nothing tried the 2023 election page, where archive history does exist | — |

## Phase 2 — Planning

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 004 | evidence-plan | 4 question fields + AskNews[:12,000] | 16,223 | 6,223 | compiler input only | Sonnet 5 | $0.0386 | name the required artifact | Correct target (CEC results table) and correctly framed the base rate as "4–6 parties seated". Same double severance as 44879 | 4 |
| 6 | — | google-query-generation | AskNews[:14,000] + question fields | 16,621 | 1,870 | Firecrawl Search | Sonnet 5 | $0.0183 | build SERP queries | 54 candidates incl. CEC, ODIHR, 2023 Wikipedia. Evidence plan again contributed 0 bytes | 3 |

## Phase 3 — Prediction markets

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 3 / 4 | — | kalshi + manifold query-gen | question + plan | 4,801 / 4,751 | 87 / 87 | market APIs | Sonnet 5 | $0.0075 | build queries | ran | 1 |
| 9 / 7 | — | kalshi + manifold scoring | candidates | 11,379 / 4,794 | 61 / **3** | — | Sonnet 5 | $0.0131 | score markets | Both sections: `"No sufficiently relevant X markets found"` + echo. **Excluded** | 1 |
| 5 / 8 | 019 | **polymarket** query-gen + scoring | question + plan | 4,739 / 4,835 | 100 / 9 | Market Signals | Sonnet 5 | $0.0072 | find + score markets | **Earned it.** 3 genuine `polymarket.com/event/...` candidates, all kept, section **included** (2,000 chars fitted). Became `[E7]`, cited by Runs 1 and 3, and carries the only liquid market in either question ($40.5K Auyl 2nd-place) | 4 |

## Phase 4 — Search, scrape, extract

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| — | 014 | **Tavily Search** | query plan (memoized) | — | 34 | *(fell through)* | Tavily API | $0 | first provider in the chain | `"Tavily Search: no usable results"` — same as 44879. Zero results → ranking skipped, no LLM spend. **0 for 2 across both runs** | 1 |
| — | — | **Firecrawl Search (main pass)** | 9 queries = title + 8 generated | — | **54 unique candidates** | url-ranking | Firecrawl `/v2/search` | not credit-tracked | **generate the candidate URLs** | 9 POSTs, ≤10 results/query, ≤90 raw, deduped to 54. Strong yield: election.gov.kz, ODIHR, 2023 Wikipedia, the KazISS poll coverage | 4 |
| 10 | — | firecrawl-url-ranking | 54 candidates (`results[:80]`) | 57,294 | 8,175 | scrape queue | Sonnet 5 | $0.1082 | rank + group 20 URLs | Good picks; 3 of 7 scraped never cited | 4 |
| — | 005–010 | scrapes cycle 1 ×6 | ranked URLs | — | 64,709 | extract | firecrawl / wikipedia adapter | — | fetch pages | timesca, gulfobserver, ODIHR never cited | 3 |
| 11 | — | **wikipedia-adapter-extract** | 2023 election page | 84,625 | 8,611 | extract report | Sonnet 5 | $0.0973 | pull the 2023 results table | **Best step in the run** — full 2023 table (6 of 7 parties seated, Baytaq 2.3%), the anchor base rate every forecaster used | 5 |
| 12 | 011 | extract cycle 1 | 6 pages | 75,825 | 15,498 | report (15,028) | Sonnet 5 | $0.1199 | pull facts per group | Retrieved well, but **manufactured** "only Adilet (64.8%) and Auyl (5.4%) above a 5% threshold" from raw poll shares without checking the denominator | 2 |
| — | 012 | scrape cycle 2 ×1 | ranked URLs | — | 17,999 | extract | firecrawl | — | close official-source gap | election.gov.kz CEC release — right call | 4 |
| 13 | 013 | extract cycle 2 | 1 page | 39,819 | 18,411 | report (18,375) | Sonnet 5 | $0.1018 | re-extract | `lacking_groups: []`. Output ≈ half the input — the cumulative-rewrite pattern again | 4 |
| — | **015** | **Firecrawl Search section assembly** | queries + ranked groups + cycles + extract report + all 54 search results | — | **69,365** | compiler input, artifact check, raw-research forecaster | `format_firecrawl_research` (code) | — | render one provider section | 8,798 scaffolding + **18,404 extract report** + 42,163 search-results block. The extract report runs to offset 27,202 — past the artifact check's 20,695 head cut, which is why the 2026 poll table was elided (A.3) | 3 |
| — | 016 | Resolution Criteria Sources section | page-summary (no wayback) | — | 4,312 | compiler input, artifact check | code | — | render resolution section | Kept **whole** by the fitter (priority 1) — including the pre-cooked "only two parties above 5%" verdict | 2 |
| — | 020 | **AskNews deepnews** | question | — | 23,820 | compiler input **+ query-gen seed** | AskNews API (no LLM) | $0 in OpenRouter | recent news digest | **22,528 chars = 21.6% of the compiler input, 2nd-largest section.** Earned none of it: Russian/Turkish/Portuguese campaign-logistics coverage — debates, observer invitations, ballot order. No polls, no seat projections. 13 URLs, **0 ranked, 0 scraped.** With the compiler dead, all 22.5K landed raw in the fallback brief and no forecaster cited it | 2 |
| 14 | 021 | **artifact-check v1** | all sections | 69,510 | 1,361 | banner | Sonnet 5 | $0.0605 | status + retry queries | **Worst step in either run.** `missing` is correct (election 24 days out), but `closest_available` asserts Aq Jol / Respublica / QHP / JSDP / Baytaq "polled below 5%". Shares are of *respondents* (sum 86.9); the bar is of *valid votes*. Normalised, Respublica 4.8→5.5 and Aq Jol 4.7→5.4 both **clear**. The prompt explicitly bans this ("do not perform arithmetic… do not characterize it as high/low") and it did it anyway | 1 |
| — | 022 | retry_decision | gate | — | 153 | log | code | — | decide retry | No retry, no queries. Correct for a future event — saved the ~$0.58 that 44879 spent | 4 |

## Phase 5 — Compile & forecast

| Audit # | Trace | Task | From | In | Out | To | Processor | Cost | Purpose | Verdict | ★ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| — | 023 | fit sections | 5 sections | — | 104,170 | compiler | code | — | assemble input | Polymarket cut 4,715 → 2,000 | 3 |
| 15 | — | **compiler/research-brief** | 104,170 fitted | 123,704 | **0** | — | **Opus 5** | $0.00 | select + rank evidence | **HARD FAIL**, 0.0 s → `except Exception: return None` ([compiler.py:712](compiler.py#L712)) | 0 |
| — | 024 | heuristic fallback report | **unfitted** sections | — | 36,957 | brief | code | — | salvage a brief | 2.8:1 vs 44879's 7.5:1. Nested duplicate headers, `# Resolution Criteria Sources` twice, three `[Truncated.]` cuts at the 5,000-char joins, placeholder text under Near Proxy / Weak Proxy / Background. Run 1 caught it: *"verbatim duplicates — one signal, not two"*. Credit: it read **unfitted** sections, so Polymarket market [3] survived where the fitted input would have cut it. But nothing told the forecasters they were reading a fallback | 2 |
| — | 025 | banner injection | artifact-check v1 | — | +2,729 → 39,686 | forecasters | code | — | prepend status | **The delivery vector for the defect.** Injects `closest_available` verbatim *after* the compiler returns — no compiler pass could have caught or edited it | 1 |
| 16 | — | mc-forecast | fallback brief | 55,342 | 16,569 | aggregator | Opus 5 | $0.4708 | forecast | mode = **Six** (0.23) | 3 |
| 17 | — | mc-forecast | fallback brief | 55,342 | 12,307 | aggregator | gpt-5.6-sol | $0.2340 | forecast | mode = **Two or Fewer** (0.32) | 3 |
| 18 | — | mc-forecast | raw research | 127,807 | 11,124 | aggregator | Sonnet 5 | $0.2616 | heterogeneous view | mode = **Four** (0.281). Misread the $54.6K *event* total as one market's liquidity despite the inline "NOT any single market's volume" caveat beside it | 3 |

---

# Structural findings

## A. Wayback threw away the monthly captures that would have closed 44879's main gap

`fetch_snapshot_history` does two HTTP calls — no scraper stack, plain `httpx`:

1. **List** — Wayback CDX API, `months_back=18`, `collapse=timestamp:6` → **at most one capture
   per calendar month**, so up to 18 timestamps were returned.
2. **Fetch** — `max_snapshots=4` evenly spread, each truncated to `_MAX_SNAPSHOT_CHARS = 15_000`.

Result: captures **2025-01-17, 2025-07-01, 2026-01-01, 2026-07-01** — six months apart, with
~14 monthly captures discarded before a single fetch. For a question asking how many entries
appear in *one month*, those monthlies are the artifact. They would have given the true
month-by-month series including **August 2025** — the exact seasonality gap the brief named
as its top gap, and the reason Phase 1 widened sigma from ~4.3 to ~6, moving ~8 points from
the centre into the tails.

So 44879 had two free paths to its own base rate and took neither: the linked
`known_exploited_vulnerabilities.csv` (never fetched), and these captures (listed, discarded
by a constant). The second is the cheaper fix — [Adapters/Wayback.py:65](Adapters/Wayback.py#L65),
same source, no new retrieval logic. It wants a total-char budget, though: 18 × 15,000 blows
the input cap, so the shape is "fetch all monthly captures, extract only the total line from
each," not "raise `max_snapshots` to 18."

## A.1 Where the 51,478-char ranking input comes from

Measured against `_build_ranking_prompt` ([firecrawl_research.py:465](research/firecrawl_research.py#L465))
with 44879's real question fields and zero results:

| Component | Chars |
|---|---|
| Fixed template (date discipline, grouping rules, JSON shape) | 2,392 |
| `title` | 126 |
| `resolution_criteria` | 211 |
| `background` (`question_details.description`) | 417 |
| `fine_print` (empty → `"Not provided."`) | 13 |
| **Overhead** | **3,159** |
| JSON-serialization overhead in the audit figure (see preamble) | ~970 |
| **Candidate blocks** | **~47,350** (94%) |

Each candidate is a 7-line block — `Title / URL / Source / Category / Date / Query / Description`.
~47,350 ÷ 69 logged candidates = **~686 chars each** (÷ 80, the `_MAX_RANKING_INPUT_RESULTS` cap, = 592).
Two fields drive it: Firecrawl's `Description`, and `Query`, which **restates the full search-query
string on every result** — 9 distinct queries across ~70–80 blocks, ~6–7K chars (13% of the payload)
of pure repetition. `Source` and `Category` are frequently `"Not provided."`, another ~56 chars of
nothing per block.

**Why the retry ranking is 112,326 for only 25 candidates** — measured against the two rendered
search-result blocks in `research.md`:

| | Main pass | Artifact retry |
|---|---:|---:|
| Entries | 69 | **25** |
| Block chars (as rendered) | 42,577 | **66,554** |
| Mean per entry | 610 | **2,654** |
| Description mean | 290 | **2,315** |
| Largest single entry | 2,332 | **14,799** |
| Entries > 2,000 chars | 2 | **8** |

**How many URLs the ranker read: 25 — nothing from the main pass.** The same `search_results` list is
passed to `rank_firecrawl_urls(results=search_results)` *and* stored in
`FirecrawlResearchResult(search_results=search_results)`, which `format_firecrawl_research` renders in
full ([firecrawl_research.py:165–207](research/firecrawl_research.py#L165)). The rendered block has
25 entries, so the ranker's input was those same 25:

| Stage | Count |
|---|---:|
| Raw results, 4 queries × (5 web + 5 news) | ≤40 |
| After `_dedupe_results` (within this call) | **25** |
| `exclude_social_results(...)[:80]` → **read by the ranker** | **≤25** — cap never binds |
| Rendered into the section | **25** |

**Why the saved section can never reconcile with the audit figure.** `SNIPPET_OMITTED_NOTE` deletes a
description *after* the ranking call, so the block on disk is a doctored copy of what the LLM read.
Running the same arithmetic on both passes:

| | Main pass | Retry |
|---|---:|---:|
| Audit "In" | 51,478 | 112,326 |
| − est. JSON-serialization overhead | 931 | 1,009 |
| − ranking template + question fields | 3,159 | 3,159 |
| **= candidate text the LLM read** | **47,388** (69 entries) | **108,158** (25 entries) |
| Entries as saved in `research.md` | 42,132 (8 stubbed) | 66,373 (6 stubbed) |
| **Gap over the stubbed descriptions** | 5,968 | 42,319 |
| **→ per deleted description** | **746** | **7,053** |

The main pass is the control: the identical method yields an unremarkable **746** chars against a
visible description mean of 290. The retry's **7,053** is equally consistent with *its* distribution —
visible mean 2,315, eight entries over 2,000, largest surviving 14,799. If the accounting were wrong
(e.g. the ranker reading entries beyond the 25), the main pass would come out absurd too. It does not.

The stubbed entries are precisely the URLs the ranker chose to scrape — senserva, tech-insider,
hipaajournal, proofpoint, thehackernews, a CISA alert — pages that scraped to 4,975–18,000 chars. The
ranker picked them **because** their descriptions were the meatiest. So the saved record deletes the
largest descriptions, and under-records the ranking input hardest for the entries that decided the run.

≈**4,320 chars per candidate vs ~686 on the main pass.** Only the per-entry description size differs;
the candidate count does not. These per-description figures are **inferred, not measured** — the raw
Firecrawl response is never persisted.

The cause is the queries. The main pass asked news-shaped questions and got snippets; the retry asked
*"…monthly breakdown chart"* and *"…statistics report"*, which hit long-form aggregator and report
pages, for which Firecrawl returns near-full-page `description` fields. The artifact-check's queries
worked **because** they targeted data-dense pages — and that same property made the ranking call 2.3×
more expensive per candidate.

Still true regardless: **the ranking prompt is never written to `trace/`.** 51,478 + 112,326 = 164K
chars and $0.234 in 44879 alone, reconstructible only by inference, for the step that decides what
gets scraped at all.

## A.2 Where the 83,029-char extract input comes from

`_build_extract_prompt` ([serp_research.py:1425](research/serp_research.py#L1425)), 44879 cycle 1:

| Component | Chars | % |
|---|---|---|
| Fixed template (grounding rules, date discipline, output format) | 3,126 | 3.8% |
| Question fields (126 / 211 / 417 / 13) | 767 | 0.9% |
| `group_lines` — the ranked category list | ~1,620 | 2.0% |
| 6 × packet headers | ~5,400 (≈900 each) | 6.5% |
| **Scraped page content** | **72,116** | **86.9%** |
| `previous_report` → `"No previous report yet."` | 23 | — |

The group/header split comes from solving across cycles, since cycles 2–3 carry one packet each:
`G + 6H ≈ 7,020` and `G + 1H ≈ 2,519` → **H ≈ 900, G ≈ 1,620**. Both residuals below carry ~700–900
of JSON-serialization overhead (see preamble), so the true figures are nearer **H ≈ 750–800,
G ≈ 1,400**. All four extract calls reconcile within ~30 chars:

| Call | Overhead | + content | + prev report | + groups/headers | = total | audit |
|---|---|---|---|---|---|---|
| cycle 1 | 3,893 | 72,116 | 0 | 7,020 | 83,029 | ✓ |
| cycle 2 | 3,893 | 4,285 | 8,882 | 2,519 | 19,579 | ✓ |
| cycle 3 | 3,893 | 4,922 | 9,827 | 2,542 | 21,184 | ✓ |
| retry | 3,893 | 84,073 | 0 | 7,297 | 95,263 | ✓ |

This also confirms the cycle mechanic — `prompt_cycles = cycles[-1:] if previous_report` — so
cycles 2 and 3 resend only the new page plus the previous report, not the accumulated packets.

**The retry's `prev_report` of 0 is not an omission in this table — it is the code.**
`run_scrape_cycles` takes no `previous_report` argument and initialises `report = ""` locally on every
invocation ([serp_research.py:501](research/serp_research.py#L501)). The retry is a fresh invocation
with `max_cycles=1`, so its extract call literally received `"No previous report yet."` The two
extract reports are therefore **siblings, not cumulative**: the retry never saw the main pass's
alert-day counts, so no single extract call ever reconciled "June 2026 = 23" (hivepro) against the
main pass's separately-logged June 5 = 1 and June 9 = 3. That reconciliation happened a layer later,
at artifact-check v2 and the compiler. The upside is that the retry escaped the cumulative-rewrite
cost that made cycles 2 and 3 charge $0.057 and $0.063 to absorb one 4–5K page each.

Two notes. The ~900-char packet header is mostly **repeated instruction text**: every packet
restates `Group`, `Group purpose`, `URL`, `Source publish date`, `URL purpose`, `Scrape status`,
and two of those carry long fixed warning labels (*"(pre-scrape guess only — NOT evidence; do not
extract any fact, value, or date from this line)"*). Across 12 packets that is ~5–6K chars of the
same caveats re-sent — the same shape as `Query` repeating on every ranking candidate.

And the retry extract landed **within a few hundred chars of `_MAX_EXTRACT_INPUT_CHARS = 90_000`**
(~89,500 by this arithmetic). Whether it tipped over is not determinable, because
`_compact_scrape_content_for_prompt` shrinks content before the cap applies and the compacted size
is never recorded — which also makes ≈900/packet an upper bound. Given this pipeline's history with
`[:N]` truncation, a call that close to a cap with no log line is worth instrumenting.

## A.3 What `research_excerpt` is, and why it is always exactly 60,000

The artifact check's prompt ([pipeline.py:1046](research/pipeline.py#L1046)) has four parts:

| Component | 44879 v1 | Source |
|---|---|---|
| Fixed template | 4,524 | `_ARTIFACT_CHECK_PROMPT` |
| `title` | 126 | |
| `evidence_plan_excerpt` | 4,000 | `_truncate_text(evidence_plan, 4_000)`, from 5,481 |
| `research_excerpt` | ~60,000 | `fit_artifact_check_sections(included_results)` |
| `prior_check_section` | 0 (v1) / 2,690 (v2) | the previous verdict, re-injected |

`research_excerpt` is **every included provider section** — for 44879 that is Evidence Plan,
Firecrawl Search, Resolution Criteria Sources and AskNews (the three market providers were
excluded) — rendered as `## Name\n<content>` and fitted into one budget.

**Why 60,000:** `_MAX_ARTIFACT_CHECK_INPUT_CHARS = 60_000`, hardcoded. It *binds* in both runs —
raw sections total ~95,800 (44879) and ~108,100 (44880), so **36% and 44% of the research is cut
before the check reads it**. Simulating the real section sizes through the actual function
reproduces 59,996–60,000: the budget is saturated exactly, every time.

Allocation is **weighted water-filling**, not a flat per-section cap:

- sections sorted by priority — retry (0) → resolution (1) → evidence plan (2) → everything else (3)
- demand multiplied by `_ARTIFACT_CHECK_PRIORITY_WEIGHTS = (3.0, 2.0, 1.5, 1.0)`
- a floor of `_MIN_ARTIFACT_CHECK_SECTION_CHARS = 4_000` so nothing is starved
- sections that fit whole are settled and their remainder redistributed
- whatever is still over gets `_head_tail_truncate` — 60% head + 40% tail, **middle elided** with a
  visible `[... middle of this section elided to fit the artifact-check budget ...]` marker

Allocation, measured by running the **real section contents** through the actual function:

| Section | 44879 v1 | 44880 v1 |
|---|---|---|
| Resolution Criteria Sources | 6,489 (**whole**) | 4,312 (**whole**) |
| Evidence Plan | 4,772 of 5,481 | 4,618 of 5,915 |
| Firecrawl Search | **36,285 of 62,507 (58%)** | **34,569 of 69,365 (50%)** |
| Polymarket | — | 4,000 (**cut to the floor** from 4,715) |
| AskNews | 12,365 of 21,302 (58%) | 12,400 of 23,820 (52%) |
| **rendered** | **59,996** | **60,000** |

### What actually gets cut inside Firecrawl Search

The section is not homogeneous. Its composition (offsets measured against the literal block
labels emitted by `format_firecrawl_research`):

| Block | 44879 chars |
|---|---:|
| Header, generated queries, ranked URL groups, cycle list | 8,983 |
| `Compiled scraped research:` — the extract report | 10,876 (= the 10,847 recorded at trace seq 14, + 29 for its label line) |
| `Raw Firecrawl search results considered:` — the 69 candidates | 42,648 |
| **Section total** | **62,507** |

The **retry** section has the same shape, measured the same way from `research.md`:

| Block | Main pass | Retry |
|---|---:|---:|
| Banner + `Firecrawl sources:` | 226 | 155 |
| Generated queries | 629 | 397 (**4** retry queries) |
| Ranked URL groups | 6,399 | 5,005 |
| Scrape cycles listing | 1,729 | 887 |
| `Compiled scraped research:` (extract report) | 10,876 | 10,334 |
| `Raw Firecrawl search results considered:` | **42,648** (69 entries) | **66,626** (25 entries) |
| **Section total** | **62,507** | **83,404** |
| Search-results share | 68% | **80%** |

Cross-checks: the retry's extract-report block is 10,334 vs the **10,305** recorded at trace seq 29 —
a 29-char difference, exactly its label line plus blank line, identical to the main pass. Its
search-results block holds 25 entries with 6 `SNIPPET_OMITTED_NOTE` substitutions, matching the 6
retry scrapes. (The precompress metadata records the section as 83,261; measuring `research.md`'s copy
gives 83,404 — a 143-char difference attributable to trailing separators in the raw-research view.
Proportions unaffected.)

So the retry section is 1.33× the main pass's while carrying a *smaller* extract report and *less*
scaffolding: 25 candidate descriptions outweigh 69 of them.

Because the search-results block sits **last**, the 60/40 head-tail cut lands almost entirely on it:

| Block | 44879 kept |
|---|---|
| Scaffolding (8,983) | **whole** |
| Extract report (10,876) | **whole** |
| Search-results block (42,648) | 16,426 kept / **26,222 elided** |

`8,983 + 10,876 + 16,426 = 36,285` ✓

**This corrects an earlier claim in this document.** I previously wrote that Firecrawl "surrenders
42–50% of its middle — that is where the scraped extract reports live." For **44879 that is wrong**:
the extract report is entirely inside the head and survives whole; 100% of the elision falls on the
search-results block. The fitter did the right thing.

For **44880 it is right, and it matters.** Allowance 34,569 → head keeps 0…20,695, but the extract
report spans 8,798…27,202, so **its last 6,507 chars are elided** — and that is exactly where the
2026 poll table sits (offsets ~23,100–23,800):

> Adilet 64.8% · Auyl 5.4% · **Respublica 4.8%** · **Ak Zhol 4.7%** · JSDP 3.3% · QHP 3.1% ·
> Baytaq 0.8% · **"Against all": 2.2%** · **Undecided on party: 10.9%**

All of it in the elided window. What survived in the head was the *2023* results table and a ranker
purpose line mentioning "Adilet leading with 64.8%". Meanwhile **Resolution Criteria Sources was kept
whole** (priority 1) — the section already carrying the pre-cooked verdict *"only two parties clearly
above the 5% threshold in every poll."*

So the sharper diagnosis of 44880's ★1 artifact-check row: it did not botch arithmetic on raw
numbers — **it inherited a verdict from a summary while the table that would have refuted it, including
the 2.2% + 10.9% residual that exposes the denominator, was elided from its view.** Same shape as this
folder's `postmortem-Q44880.md` wrongly blaming the compiler, one layer further up again.

### Two further notes

1. **The artifact check never sees the resolution criteria.** `_ARTIFACT_CHECK_PROMPT` interpolates
   only `{today}`, `{title}`, `{evidence_plan_excerpt}`, `{research_excerpt}`, `{prior_check_section}`.
   It judges "was the required artifact found?" from the question **title** plus the evidence plan's
   description of the artifact — never the resolution text or the fine print.
2. **The evidence plan is sent twice.** Once as `evidence_plan_excerpt` (4,000) and again as a
   `## Evidence Plan` section inside `research_excerpt` (4,772) — confirmed by
   [pipeline.py:326](research/pipeline.py#L326), which skips "Evidence Plan" when emitting provider
   traces precisely because it *is* one of the entries in `results`. ~8,800 chars on a 5,481-char
   document, 4,772 of it out of the research budget Firecrawl Search is being cut to fit.

Set against the rest of the pipeline, this fitter is the **best-engineered truncation in the
codebase** — priorities, a floor, and middle-elision instead of a bare head cut. Its docstring says
it exists because 44512 lost a retry section to exactly the naive combination still used elsewhere.
It is the pattern the compiler-input fit, `_join_research_context`, and `DEFAULT_ASKNEWS_CHAR_LIMIT`
should copy — the last of which is what severs the evidence plan in Finding B.

## A.4 The retry section evicted the main pass from artifact-check v2

`83,261 → 72,478` is not a shrink — they are different quantities. 83,261 is one input **section**;
72,478 is the whole **prompt**, and its `research_excerpt` is still exactly 60,000, same as v1:

```
template 4,524 + title 126 + evidence plan 4,000 + research_excerpt 60,000 + prior verdict 2,690
= 71,340  + ~1,100 JSON overhead  ≈ 72,440          (audit row 18: 72,478 ✓)
```

What changed is **who owns the 60,000**. Raw sections went 95,779 → 179,040 against a fixed ceiling,
and the retry section is priority **0** with weight **3.0** — the highest in
`_ARTIFACT_CHECK_PRIORITY_WEIGHTS` — so it absorbed the entire increase at everyone else's expense:

| Section | v1 (pre-retry) | v2 (post-retry) | |
|---|---:|---:|---|
| **Focused Artifact Retry** (p0) | — | **37,323 of 83,261 (45%)** | **62% of the budget** |
| Resolution Criteria Sources (p1) | 6,491 — **WHOLE** | 4,002 (62%) | no longer whole |
| Evidence Plan (p2) | 4,774 (87%) | 4,002 (73%) | |
| **Firecrawl Search** (p3) | **36,287 (58%)** | **10,549 (17%)** | **loses 25,738** |
| AskNews (p3) | 12,365 (58%) | 4,000 (19%) | floored |
| **rendered** | 59,996 | 60,000 | unchanged |

**Firecrawl Search at 10,549 cannot reach its own extract report.** Head/tail gives 6,283 + 4,190,
but the extract report does not start until offset 8,983 — so the head stops inside the ranked-URL
listing and the tail picks up only the last 4,190 chars of the search-results block.
**Artifact-check v2 never saw the main pass's extract report.**

That is visible in the v1→v2 diff in `evolution.md`. v1's `what_was_found` was built from the main
pass — *"March 20 = 5 CVEs; June 5 = 1; June 9 = 3; April 21 = 8; May 22 = 2; July 27 = 2"* — and v2
**deleted every one of those**, rewriting the field purely from retry sources (*"Q1 2026 = 71; April =
31; May = 21; June = 23"*).

The step whose job is to **reconcile** the retry against what was already found had the "already
found" evicted from its context by the retry itself. The monthly totals are the better evidence, so
the outcome was fine — but by luck, not design. This is the [`commonwealth-44512`] shape inverted:
there a truncation destroyed what the retry retrieved; here the retry's bulk destroyed what the main
pass retrieved. A reconciliation step should pin the prior pass's extract report, not let a
weight-3.0 newcomer crowd it out.

## A.5 What the artifact checks output, and who reads it

Both checks emit the same fixed 6-field JSON — ~1,960 chars of text out of a 72,478-char prompt.
The object is then **split between two consumers**:

| Field | v1 (44879) | v2 (44879) | Consumer |
|---|---:|---:|---|
| `status` | 7 | 7 | banner + retry gate |
| `what_was_found` | 398 | 701 | banner only |
| `what_is_missing` | 693 | 389 | banner only |
| `closest_available` | 477 | 608 | banner only |
| `forecast_swing` | 8 (`moderate`) | 3 (`low`) | banner only |
| `retry_queries` | 372 (4 queries) | 252 (4 queries) | **v1 only — v2's are never read** |

- **`retry_queries`** is read at [pipeline.py:376-405](research/pipeline.py#L376-L405), from v1 only. There is no
  second retry loop, so v2's four queries are traced and discarded.
- **The other five fields of the final check** reach the forecasters *verbatim*. Line 501 does
  `artifact_check = refreshed_check` (v2 overwrites v1), then
  [`_apply_artifact_status_banner`](research/pipeline.py#L750) prepends them as
  `## Required Artifact Status` at line 529 — **after** the compiler has run. See Finding J for
  what that block contains. The compiler *does* separately receive the same five fields via
  `_format_artifact_check` ([compiler.py:717](compiler.py#L717)); what it is forbidden is emitting
  its own verdict ([compiler.py:819](compiler.py#L819)). So the verdict reaches the forecaster by
  two routes: as compiler guidance, and verbatim as the banner.

**The second cause of the v1→v2 loss.** A.4 shows v2's *research excerpt* had no main-pass extract
report in it. But v1's full JSON **was** in v2's prompt — that is the `prior 2,690` term, from
`_PRIOR_CHECK_SECTION` at [pipeline.py:882](research/pipeline.py#L882). The model could read
"Mar 20 = 5; Jun 9 = 3…" and deleted them anyway, because the prompt instructs it to:

> Your job now is to **RECONCILE, not restate** … Do not carry any earlier claim forward unexamined.

The only carry-forward licence is conditional on the retry *contradicting* the earlier claim. The
daily counts were not contradicted — they were complementary — and there is no "keep what still
stands" branch. So the truncation removed the evidence and the prompt removed the permission to
restate it from memory. Fixing either one alone would not have saved it.

**44880 had only one check.** `retry_queries: []` ⇒ `022_retry_decision` records
`wants_retry: false, ran: false`; no v2 exists. Its single verdict (1,675 chars, `missing`,
swing `decisive`) went straight to the banner — including the `closest_available` field that
presents the **2023** election result as the reference point.

## B. The evidence plan is severed at both ends

```
resolution scrapes ──X──► evidence plan ──X──► query generation
   (parallel task,          (5,481 / 5,915)     (truncated out by
    never awaited)                │              a 14,000-char cap)
                                  │
                                  └──► compiler input ✓  (only surviving consumer)
```

**Upstream:** the resolution scraper is fired as `asyncio.create_task` and explicitly kept
"off the AskNews → evidence-plan critical path… saves its head start (~1 minute of wall clock
per question)" ([pipeline.py:204](research/pipeline.py#L204)). The plan is awaited at Stage 2
while the scraper is still running, so it writes `ideal_sources` blind to what the resolution
URL actually contains.

**Downstream:** `search_asknews_research = _join_research_context(("AskNews research", …),
("Evidence plan", …))` is *supposed* to carry the plan to query-gen. Two head-truncations kill it,
and either alone is fatal:

1. `_join_research_context(max_chars=18_000)` — AskNews is concatenated **first** and already
   overruns the cap alone (21,302 / 23,820). The cut lands inside AskNews; the `## Evidence plan`
   section that follows is discarded whole.
2. `_truncate_text(…, DEFAULT_ASKNEWS_CHAR_LIMIT = 14_000)` in `build_query_generation_prompt`.

Since AskNews alone exceeds 14,000 in both runs, **the first 14,000 chars are pure AskNews
regardless.** The evidence plan contributes exactly zero bytes to query generation in both
questions.

**Who does read it** (three consumers, none of which can act on it in time):

| Consumer | How much | When |
|---|---|---|
| Market providers (`market_question`) | full | before search — but they found 0 markets in 44879 |
| `artifact-check` | `_truncate_text(plan, 4_000)` **+ again** as a `## Evidence Plan` section inside `research_excerpt` | *after* all retrieval is done |
| Compiler input | full | ~9 min downstream |

So the plan reaches every consumer that cannot change what gets scraped, and is cut from the
one that can. The queries that *did* find monthly totals came from `artifact-check`'s
`retry_queries` and worked immediately — the same information arriving one stage too late and
$0.58 more expensive.

**The direct proof.** The plan's last section is 646 chars of **10 queries written for the next
stage**. Two of them are:

```
- site:cisa.gov known exploited vulnerabilities catalog csv
- CISA KEV catalog CSV JSON feed download
```

The 9 queries `google-query-generation` actually emitted contain **no `csv`, no `json`, no `feed`,
no `download`** — not one of the plan's 10 survives in any form. And the plan's
`## Required Evidence Artifact` had already named the right target
(*"Monthly historical counts of CISA KEV Catalog additions (Date Added) by month, trailing 12–24
months"*) with ideal sources *"official page/**CSV/JSON feed**… machine-readable data (JSON/CSV
export)"*.

The planner got it right and wrote the queries to find it. Nothing that could act on them ever read
them. That is the real reason 44879's queries never targeted the CSV — not a planning failure, a
wiring failure.

Worth noting what the planner had to work with: title + resolution criteria + background + fine
print + **AskNews capped at 12,000** ([evidence_plan.py:12](research/evidence_plan.py#L12)), i.e. 56%
of the 21,325-char AskNews section and **no web page at all** — the resolution scrape is a parallel
task that is not awaited. It named the correct artifact from news snippets alone.

## C. Two silent Opus failures in two runs

| Run | Call | Signature | Consequence |
|---|---|---|---|
| 44879 | `mc-forecast[claude-opus-5]` | 33,207 in → 0 out, $0.00, 0.0 s | ensemble 3 → 2 |
| 44880 | `compiler/research-brief` | 123,704 in → 0 out, $0.00, 0.0 s | brief replaced by heuristic fallback |

Both exhausted `OPENROUTER_MAX_ATTEMPTS` and degraded quietly. `degraded_search_providers` is
`[]` in both `forecast.json` files. Only forecaster failures get a runs.md line; the compiler
failure has no log entry anywhere — it's detectable only by the all-zeros audit row and the
fallback's structural fingerprint.

## D. AskNews is invisible in every cost figure you have

`research/asknews_research.py` makes no OpenRouter call — no `MonetaryCostManager`, no
`AsyncOpenAI`. It's a separate paid subscription. So it has no audit row, and the
research / compiler / forecaster cost splits assign it **$0.00** while it consumes ~20% of the
most expensive call in each run. It also runs **first** (Stage 1) and is passed *into* the
search providers, so it seeds query generation before the evidence plan exists.

Trimming AskNews would cut real Opus input tokens that the cost table attributes entirely
to "compiler."

## E. 44880's three runs disagreed and the average hid it

Six (0.23) / Two-or-Fewer (0.32) / Four (0.281) averaged to a near-uniform
21.8 / 24.7 / 22.7 / 15.8 / 12.6 / 2.3. The ensemble reported "no idea" while three runs each
had a confident, different idea. Widest inter-run disagreement logged to date.

## F. Correction to the in-folder `postmortem-Q44880.md`, §5.1

It calls the $40.5K and $54.6K Polymarket volumes "irreconcilable, at least one confabulated."
They aren't: [research.md:1249](44880_How_many_parties_will_obtain_representation_in_the_2026_Kaza/research.md#L1249)
shows $54.6K as the *event* total across 7 sub-markets and
[:1265](44880_How_many_parties_will_obtain_representation_in_the_2026_Kaza/research.md#L1265)
shows $40.5K as the Auyl sub-market. Run 1 quoted it correctly; Run 3 misattributed the event
total to a single market. A misread, not a confabulation.

## G. The search layer is the pipeline's biggest instrumentation hole

The step that *generates every candidate URL* has no audit row (no LLM) and no trace stage. It is
reconstructible only from code plus the candidate counts:

On the **main pass**, `_queries_with_title(title, query_plan)` → **[question title] + 8 generated
queries, deduped = 9**. On the **artifact retry**, `preset_queries` short-circuits that branch
entirely ([firecrawl_research.py:148](research/firecrawl_research.py#L148)): the queries are exactly
artifact-check v1's `retry_queries`, normalised — **no title prepended, and no query-generation LLM
call**, which is why one `google-query-generation` row covers the whole run.

One `POST /v2/search` per query, `sources=("web","news")`,
`per_source_limit = ceil(DEFAULT_FIRECRAWL_TOTAL_RESULTS_PER_QUERY / 2) = 5` → ≤10 results per query,
deduped to the logged candidates:

| Run | Phase | Search calls | Query source | Unique candidates |
|---|---|---|---|---|
| 44879 | main pass | 9 | title + query-gen | 69 |
| 44879 | artifact retry | **4** | artifact-check v1 `retry_queries` | 25 |
| 44880 | main pass | 9 | title + query-gen | 54 |

**The Firecrawl credit budget does not count these.** `audit.md` reports 15/25 and 8/25 credits.
Both equal the **firecrawl-scrape** count exactly:

- 44879: 2 resolution + 5 cycle-1 + 1 cycle-2 + 1 cycle-3 + 6 retry = **15** ✓
- 44880: 3 resolution + 4 cycle-1 + 1 cycle-2 = **8** ✓

So 13 search calls in 44879 (9 + 4) and 9 in 44880 sit **entirely outside the tracked budget** — even though
`_firecrawl_search` computes an `estimated_credits` for exactly this and only writes it to a log line.
If you ever tune against "credits spent", you are tuning against scrapes only.

**Tavily ran first in both questions and returned "no usable results" both times.** It cost nothing in
LLM terms — the query plan is memoized in `_QUERY_PLAN_CACHE` (which is why one
`google-query-generation` row serves two providers), and zero results short-circuits ranking via
`if not results: return []`. But it is 0-for-2, its failure surfaces only as a 34-char trace payload,
and nothing escalates a chain provider that never works. SerpAPI was not in the chain at all in either
run — `fell_through` lists Tavily alone.

## H. The 42,648-char search-results block is not redundant — it carried the run's only August evidence

The obvious cost-cut is "we already compiled a 10,876-char extract report, why still ship 42,648
chars of search results?" The answer is that **they cover disjoint URLs**, by design:

- the extract report covers the **8 pages actually scraped** in the main pass;
- the search-results block covers the **69 candidates**, 61 of which were never scraped — it is the
  only record of them;
- and the dedup is explicit: `format_firecrawl_research` swaps the description for
  `SNIPPET_OMITTED_NOTE` on any URL that *was* scraped
  ([firecrawl_research.py:317](research/firecrawl_research.py#L317)). It fires exactly **8 times**
  in 44879 — matching the 8 main-pass scrapes.

**It earned its keep here.** `recordedfuture.com/blog/august-2025-cve-landscape` appears in the audit
as candidate **and ranked-for-scrape**, and was never scraped. Yet it produced `[E9]` in the brief,
quoted verbatim — *"On August 12, 2025, CISA added CVE-2025-8088… On August 13, 2025, CISA added
CVE-2025-8875 and CVE-2025-8876."* That text exists nowhere but its search-result description. And
`[E9]` is the run's **only August precedent of any year** — the very gap that drove the sigma widening
from ~4.3 to ~6.

Composition of the block (69 entries, 42,648 chars incl. its label and `[N] ` prefixes; 42,203 across
the entries themselves):

| | Chars |
|---|---:|
| Metadata lines (`title / URL / Source / Category / Date / Query`) | 20,951 |
| — of which the `Query:` string restated on all 69 entries (only **9 distinct**) | 4,527 |
| Description text | 20,079 |

Median entry is 472 chars; only 10 exceed 800; largest 2,332 — these are snippets, not scraped pages.

**So the trim is the scaffolding, not the descriptions:** ~4,500 chars of repeated query strings plus
`Category:`/`Date:` lines that are mostly `"Not provided."` — roughly 6K of the 42K, removable with no
information loss. Identical pattern to `Query` repeating on every ranking candidate (A.1).

The fair criticism is not that the block exists but that **nothing re-reads it with intent**. It rides
into the compiler, the artifact check and the raw-research forecaster as bulk context, and `[E9]` was
picked up by the compiler noticing a snippet. Feeding the *unscraped* candidates' snippets to the
extract step explicitly — "here are 61 pages we did not open, mine them for the artifact" — would make
that deliberate rather than lucky.

## I. The precompress pass was cut off by `max_tokens` and then declared itself lossless

`_fit_sections_to_budget` fires because the five compiler sections sum to **178,930 > 120,000**:

| Section | raw | after fit |
|---|---:|---:|
| Evidence Plan | 5,481 | 5,481 |
| Firecrawl Search | 62,294 | 62,294 |
| Resolution Criteria Sources | 6,489 | 6,489 |
| AskNews | 21,405 | 21,405 |
| **Focused Artifact Retry** | **83,261** | **19,265** |
| | **178,930** | **114,934** |

It picks the largest **non-resolution** section and sets a target ([compiler.py:559](compiler.py#L559)):

```
overflow = 178,930 − 120,000 = 58,930
target   = max(83,261 − 58,930, 83,261//4, 10,000) = 24,331
```

`trace.jsonl` seq 32 confirms: `{"target_chars": 24331, "input_chars": 83261}`.

**Then [compiler.py:435](compiler.py#L435) caps the reply:**

```python
max_tokens = max(2_000, min(16_000, target_chars // 3))   # = 8,110
```

Audit row 19's **native out is 8,110 — the cap, hit exactly**. The `//3` assumes ~3 chars/token; this
content (URLs, CVE IDs, ISO dates) ran **2.36 chars/token**, so the cap binds at 19,118 chars, 21%
short of the target the same function computed. The output file ends mid-URL:

```
[5] CISA Adds One Known Exploited Vulnerability to Catalog — https://
```

The retry section held **25** search-result entries. The compiler received **5**. Entries [6]–[25] —
~59,000 chars of the block Finding H shows to be load-bearing — were never emitted, and because
`_compress_section_text` returned a string rather than `None`, `_visible_truncate` never ran and no
marker was written. Instead [compiler.py:484](compiler.py#L484) prepended:

> `[Section condensed by a lossless-compression pass from 83,261 to 19,118 chars — duplicates and boilerplate removed; all distinct claims retained.]`

The final clause is false, and it is *in the compiler's prompt*. This is the [`44382`]/[`44512`]/[`44619`]
silent-truncation family with an added false assurance to the downstream model.

**It is also cost-negative.** Solving the rates from the audit's native-token columns gives
**Sonnet 5 = $2/M in, $10/M out** and **Opus 5 = $5/M in, $25/M out** (row 20 checks exactly:
53,893×$5/M + 9,742×$25/M = $0.513015). Precompress removed 63,996 chars from Opus's input; at row
20's measured 0.39993 native tokens/char that is 25,594 tokens = **$0.128**. The call cost
**$0.158**. Net **−$0.030**, for the privilege of losing 20 of 25 entries.

Three fixes, in order of cheapness: (1) derive `max_tokens` from a measured chars/token ratio or just
`target_chars // 2`; (2) after the call, compare `len(compressed)` against `max_tokens` — if the reply
hit the cap, treat it as a failure and fall back to `_visible_truncate`; (3) note that 120,000 is
self-imposed — 178,930 chars is ~71,600 tokens, well inside Opus 5's context, so raising the budget
removes the whole stage.

## J. What the artifact-status banner is, and the anchor it carries

`_apply_artifact_status_banner` ([pipeline.py:750](research/pipeline.py#L750)) prepends a fixed block
straight after `# Compiled Research Brief`. 44879's is **2,757 chars — 15% of the 18,194-char brief,
in the highest-attention position**:

| Line | chars | source |
|---|---:|---|
| `## Required Artifact Status (starting point — reconstruct and reconcile)` | 72 | fixed, per status |
| `The resolution-target artifact is **PARTIAL — …NOT confirmed**.` | 92 | fixed, per status |
| `- What was found:` | 719 | 18 prefix + **701 from v2** |
| `- Still missing:` | 406 | 17 prefix + **389 from v2** |
| `- Closest available adjacent metric (a STARTING reference only — not the answer…)` | 803 | **195 prefix** + 608 from v2 |
| `- Forecast swing if resolved: low` | 33 | 30 prefix + 3 from v2 |
| `- Forecasting rule: build your own outside view…` | 624 | fixed, per status |
| newlines | 8 | |
| | **2,757** | 1,698 model text / 1,059 scaffolding |

**Its job is to be un-overridable.** The compiler is banned from writing a verdict
([compiler.py:819](compiler.py#L819)) so the status reaches the forecaster as concatenated code
output, immune to an LLM talking itself into "found". `complete` vs `partial`/`missing` changes the
framing far more than the label: `closest_available` goes from *"use it, do not ignore it"* to
*"a STARTING reference only — not the answer"*, and the rule goes from *"weight this confirmed value
heavily"* to the 624-char flatten-under-uncertainty guard (build your own base rate, or say there is
no reference class; **widen your interval**). That guard is the [`44410`] fix, and it is the only
place in the entire brief that addresses interval width directly.

### J.1 Banner vs `## Extracted Artifact Rows` — and what the head/tail cut really destroys

The two blocks sit adjacent at the top of the brief and are constantly confused. They are different
things by design:

| | **Required Artifact Status** | **Extracted Artifact Rows** |
|---|---|---|
| Author | code (`_apply_artifact_status_banner`) | **Opus 5** |
| Input | artifact-check v2's JSON, verbatim | the 115,229-char fitted sections |
| Which model read the evidence | **Sonnet**, from a 60,000-char excerpt | **Opus**, from 115,229 chars |
| Answers | *was the value found?* — the **verdict** | *what are the numbers?* — the **data** |
| Table | never, prose only | yes — the series lives here |

[compiler.py:819](compiler.py#L819) bans Opus from writing a verdict ("This section is only for the
data itself"); [compiler.py:820](compiler.py#L820) calls Rows "the single most important section".

**In 44879 the Rows section is a strict superset.** It adds October 2025 = 32 (Loginsoft), the
explicit "August 2026 — NOT YET RELEASED" row with the live scrape evidence, and — crucially — the
**Wayback catalog-total series** (`2025-01-17: 1,250 · 2025-07-01: 1,373 · 2026-01-01: 1,484 ·
2026-07-01: 1,631 · 2026-07-29 live: 1,656`), the run's **only same-source time series** and the only
valid basis for a flow rate. The banner mentions none of it.

**Why, exactly.** The Wayback block spans offsets **4,531–6,532** of the 6,532-char Resolution
Criteria Sources section. Artifact-check v2 allowed that section 4,002 chars ⇒ head 2,355, tail from
4,961. The elided 4,531–4,961 is precisely:

```
Resolution Source History (Wayback Machine)

Historical captures of https://www.cisa.gov/known-exploited-vulnerabilities-catalog
(2025-01-17, 2025-07-01, 2026-01-01, 2026-07-01) — use for the value's flow rate and
the page's real update cadence; the live scrape above remains the current value.

**1. VALUE TIME SERIES:**
- 2025-01-17 (capture): Catalog total "Showing 1 - 20 of 1250"; ... (Date
```

and v2's tail resumes mid-word at `" Added). No August 2026 data present."`

**The cut took the heading and the label and kept the numbers.** Sonnet was left with three orphaned
bullets of catalog totals and nothing identifying them as archive captures of the resolution page.
This is the sharpest example in either run of what `_head_tail_truncate` actually costs: not raw
information, but the *frame* that makes information citable. A truncator that preserved section
headings across the elision boundary would have cost ~120 chars and saved the run's best evidence
from invisibility at the verdict stage.

**The unresolved tension:** `closest_available` puts a concrete number in the brief's most-read
position and then tells the reader not to anchor on it. In 44880 that field carried the 2023 result
(6 of 7 parties seated) and **6 sat at the mode of the submitted distribution**. And in 44879 the
banner asserts the monthly totals were retrieved while the body underneath was built from a retry
section that had lost 20 of its 25 entries (Finding I) — banner and body are two different
truncations of the same evidence, and nothing reconciles them.

## K. What each forecaster actually reads — and the uncommitted change that would couple them

All three members get the same **13,566-char `MULTIPLE_CHOICE_PROMPT_TEMPLATE`** plus question
fields ≈ **15,013 chars** of scaffolding. Only `{summary_report}` differs.

**Brief members (rows 21–22, 33,207 in).** 18,194 chars, and `18,194 + 15,013 = 33,207` exactly:

| Brief section | chars | author |
|---|---:|---|
| `# Compiled Research Brief` | 27 | code |
| `## Required Artifact Status` | 2,757 | code (Finding J) |
| `## Extracted Artifact Rows` | 1,932 | Opus |
| `## Resolution Mechanics` | 1,478 | Opus |
| **`## Key Evidence`** | **7,359** | Opus — 15 `[E n]` items, 19 URLs |
| `## Balance Check` | 1,513 | Opus |
| `## Derived Implications` | 1,268 | Opus |
| `## Market Signals` | **114** | Opus — one line saying there are none |
| `## Gaps And Cautions` | 1,746 | Opus |
| | **18,194** | |

The brief is only **55%** of what those two models read; the rest is the reasoning template.

**Raw member (row 23, 199,734 in).** Same banner, then the five provider sections verbatim:

| | chars |
|---|---:|
| `## Required Artifact Status` (identical banner) | 2,757 |
| Evidence Plan | 5,510 |
| Firecrawl Search | 62,539 |
| Resolution Criteria Sources | 6,532 |
| AskNews | 21,325 |
| **Focused Artifact Retry** | **83,530** |
| | **182,193** |

`182,193 + 15,013 + 2,528 = 199,734`, and the overhead term is confirmable: the raw view carries
2,184 newlines + 342 quotes = 2,526 extra serialized chars. **Sonnet read 10× the brief's evidence
text** — the same material before Opus's selection.

**The load-bearing detail:** the retry section Sonnet read was **83,530**, not the 19,265 the compiler
got. On this run the raw member was genuinely immune to the Finding-I cut-off — it was the only
forecaster that saw entries [6]–[25]. That is exactly the diversity the member exists for.

**That immunity is being removed in the uncommitted working tree** ([pipeline.py:543](research/pipeline.py#L543)):

```diff
-        _build_raw_research_view(included_results), artifact_check
+        _build_raw_research_view(fitted_sections or included_results), artifact_check
```

`fitted_sections` is filled *after* `_fit_sections_to_budget`, so it is the post-precompress copy.
The docstring justifies it as *"the fit step only removes duplicates and boilerplate (a
lossless-as-possible pass), never the compiler's selection"* and cites 44879's own 181K→115K as
evidence. Finding I shows that on this very run the fit step was **not** lossless — it was cut off
at `max_tokens` and lost 20 of 25 entries. Shipped as-is, the same failure would hit all three
members simultaneously, and the member designed to survive compile-stage loss would be the one
carrying it.

The ~35% token saving (~$0.11 here) is real. To keep both, gate it: fall back to `included_results`
whenever any section was compressed *and* the compressed reply hit its `max_tokens` cap.

---

# Scorecard

| | Q44879 | Q44880 |
|---|---|---|
| **Best** | wayback-history ★4 — the only same-source time series | wikipedia-adapter-extract ★5 — the 2023 base rate |
| **Worst** | Opus forecaster hard-fail ★0; **precompress cut off at `max_tokens`, 20 of 25 entries lost under a "lossless" label ★1** (Finding I); 3 extract cycles that never closed the gap ★2 | Opus compiler hard-fail ★0; artifact-check's un-normalised threshold verdict ★1 |
| **Money wasted** | ~$0.58 retry buying from aggregators what the linked CSV gives free; $0.158 cost-negative precompress; $0.029 on 6 market calls that found 0 markets | 96K-char Metaculus FAQ scrape + a malformed-URL error page; $0.021 on 4 Kalshi/Manifold calls that found 0 markets |
| **Silent failure** | forecaster (logged) | compiler (not logged anywhere) |

**Across both runs: 10 of 12 market LLM calls produced nothing that reached a forecaster (~$0.05).**
The two that paid off were Polymarket's pair in 44880, at $0.0072.
