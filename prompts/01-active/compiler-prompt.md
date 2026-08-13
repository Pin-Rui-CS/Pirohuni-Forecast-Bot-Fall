# Research Compiler (evidence brief)

**Name in code:** `_build_compiler_prompt` (compiler.py)  
**Source:** [compiler.py:762](../../compiler.py#L762)  
**Tier:** Tier 1 — `anthropic/claude-opus-5` (OpenRouter) / `gpt-5.6-sol` (OpenAI)
  
**Call site:** `_DEFAULT_MODEL`, temperature 0.1, `_COMPILER_MAX_OUTPUT_TOKENS = 10,000`

## Intended use

**Pipeline stage:** The last research step — produces the '# Compiled Research Brief' the forecaster reads.

Distils every provider's output into one evidence brief with numbered `[E#]` items, tiered `direct` / `adjacent-metric` / `near-proxy` / `market`, plus source-document `[D#]` tags that the forecaster prompts use for double-counting control. Carries the consistency check (temporally impossible evidence, corrections outrank earlier inferences) and the rule that the resolution source outranks secondary material. Two of the three forecaster runs see ONLY this brief, so anything dropped here is invisible to them.

## Inserts

- `{today}`
- `{title}`
- `{resolution_criteria or 'Not provided.'}`
- `{background or 'Not provided.'}`
- `{fine_print or 'Not provided.'}`
- `{_format_artifact_check(artifact_check)}`
- `{resolution_text or 'No resolution-source scrape was available for this question.'}`
- `{research_text}`

Sections arrive pre-fitted to `_COMPILER_INPUT_BUDGET_CHARS = 120,000` by `_fit_sections_to_budget` (LLM compression with visible markers) — no `[:N]` truncation happens in this function. Resolution-source sections are split out and placed above the secondary research.

## Length

- **Scaffold (this file's template text):** 14,973 chars, 8 slot(s)
- **Filled prompt as sent:** ~25K – 135K chars

## Template

```text
Today's date is {today}.

Forecast question:
{title}

Resolution criteria:
{resolution_criteria or 'Not provided.'}

Background:
{background or 'Not provided.'}

Fine print:
{fine_print or 'Not provided.'}

Automated check of whether the required evidence artifact was found:
{_format_artifact_check(artifact_check)}

RESOLUTION SOURCE material (AUTHORITATIVE — this is the page/feed the question
resolves from; it outranks every secondary source below for the resolution value.
It was scraped DURING THIS RESEARCH RUN, i.e. on {today}; if the page displays a
data cutoff or "as of" date older than that, the gap between the two dates is
direct evidence of how often the source actually updates):
{resolution_text or 'No resolution-source scrape was available for this question.'}

Other research provider outputs, already partially cleaned (secondary; use to inform
the forecast, never to stand in for the resolution value):
{research_text}

Task:
Distill the raw research into a detailed evidence brief for a forecaster. Select
only what could plausibly change the forecast. Drop filler, vivid color, and
broad commentary that does not bear on the resolution criteria.

Consistency check (do this before selecting evidence). Cross-check the retrieved
items against each other and against the resolution source. Flag every failure in
"Gaps And Cautions" and never label a failing item `direct`:
- Temporally impossible evidence: today is {today}. A report or observation whose claimed event or publication date is AFTER today cannot exist — the date is wrong (almost always a prior-year event mislabeled with the current year). Reclassify it as misdated historical data, flag it, and NEVER present its value as a candidate for the resolution window. This rule outranks the automated artifact check above: if that check carries a future-dated claim, correct it here rather than repeating it.
- Corrections outrank earlier inferences: if any scrape-cycle extract explicitly corrects, redates, or retracts a claim made elsewhere in the research (e.g. "Important note: this event is dated 8 August 2025, not 2026"), the correction wins. Carry the corrected fact into the brief, drop the superseded claim, and flag the contamination in Gaps And Cautions.
- Year-less dates: never assume a date without a year ("Aug 7") falls in the current year or the resolution window; keep the item marked "(year not stated in source)" and do not label it `direct`.
- Same value, two dates: if an identical figure is attributed to two different periods (e.g. the same number reported for both 2025 and 2026), at least one date is wrong or it is one stale item double-counted — flag it and treat neither as confirmed current data.
- Contradicts the resolution series: if a figure conflicts with the resolution source's own table (a "latest" reading the resolution source does not show, or one out of order with its trajectory), trust the resolution source and flag the outlier.
- Impossible superlative: if a claim like "N-month high/low" is inconsistent with the values in the extracted series, flag it.
- Wrong-era drivers: if the reasons given for a supposedly current datapoint describe events from a different period, treat that datapoint's date as suspect.
- ALREADY IN THE BASELINE? When the resolution source shows a confirmed current value (a count, total, list, or standing "as of" some date), every evidence item that implies movement toward or away from that value must be reconciled against it: does the item describe something that happened BEFORE the baseline's "as of" date (its effect is already inside the current value) or AFTER it (a genuine pending change)? Say which on the item. Watch especially for follow-up coverage of an old event (implementing decrees, anniversary pieces, secondary rollouts of an already-enacted law) dressed as new movement. If the research does not let you place an entity inside or outside the current value (e.g. the source's row-level breakdown was not retrieved), the item must say "(position vs. baseline unverified)" and must NOT be presented as the strongest candidate to change the value — and flag the unretrieved breakdown in Gaps And Cautions as the blocking gap.

Output exactly these Markdown sections:

# Compiled Research Brief

## Extracted Artifact Rows
- Name the artifact the Evidence Plan says is most important.
- Do NOT write a found / partial / not-found verdict here — the authoritative artifact status is shown to the forecaster in a separate fixed banner above this brief. This section is only for the data itself.
- If it is a table or time series and any rows were extracted, reproduce those rows here verbatim, and mark the resolution-target row as "not yet released" when the resolution source does not show it. This is the single most important section.
- Never present a secondary or year-ago figure as if it were the confirmed resolution value.
- If the automated check lists a "Closest available adjacent metric", reproduce it here and carry it into Key Evidence as an `adjacent-metric` item. Never omit a value that was actually retrieved just because it is not the exact metric.

## Resolution Mechanics
Only when the question resolves off a published source (a curated page, tracker, leaderboard, or scheduled data release) rather than by direct observation of an event; if it resolves by direct observation, write the bullet "Not applicable — resolves by direct observation of the event" — and when that event is one step inside a longer causal sequence (steps that must precede it, steps that normally follow it), add one bullet writing out the chain in order with the resolution event marked (e.g. "confidential draft → review → PUBLIC FILING (resolution event) → roadshow → listing"), so the forecaster can classify each report and market as upstream or downstream of the resolution event. State the chain as the sources describe it; do not attach timing conclusions to it here. Otherwise, at most 4 bullets, each citing its evidence:
- Whether the resolution source will or may update again before the resolution deadline: stated cadence (e.g. "updated periodically"), scheduled releases, and the observed freshness gap. If the resolution-source scrape (fetched {today}) displays a data cutoff or "as of" date older than the fetch date, state both dates explicitly — that gap is direct update-cadence evidence. If the resolution material includes a "Resolution Source History" section (dated archive captures), carry its value time series and observed update cadence here — a same-source historical series is the ONLY valid basis for a flow rate; never let a cross-source coincidence stand in for one.
- What new information CAN appear in the source before the deadline, and what CANNOT arrive in time (reporting calendars, disclosure deadlines, publication or data-pipeline lags). Distinguish activity that will be observable by the deadline from activity that happens before the deadline but is disclosed only after it.
- Any scheduled data event between today and the deadline (filing deadline, release date) that would change what the source shows.
- If the research contains nothing on these mechanics, write one bullet saying exactly that — do not invent a cadence or calendar.

## Key Evidence
A list of at most 15 items. Do NOT sort by relevance — order does not matter, and the [E#] labels are just citation handles, not a priority ranking. Format each item as:
[E1] (tier) Claim with exact numbers and dates. — Source name, publish date, URL
- SELECT BY DECISION-RELEVANCE (this governs which items make the list, not their order). The items that must appear whenever the research supports them are: (1) the RULE or MECHANISM that governs how the resolution value changes over time — eligibility criteria, recovery/transition conditions, the clock or event that triggers a change, a reaction function, a scheduled decision; and (2) the CURRENT VALUE of each input that rule depends on (including the date that starts the clock, not just the date a change was announced). A precise figure that does not feed this mechanism is background color, however exact. When a number's relevance hinges on a condition (a confounder that speeds or slows the mechanism), keep the condition with the number.
- OBSERVED BEHAVIOR OUTRANKS FORMAL PROCESS: when the question resolves on an observed event or action, a reported instance of that same behavior actually happening (a precedent, a completed prior step, a dry run) is top-tier evidence even if it carries no number and sits outside the formal process the documents describe. Do not drop a behavioral precedent in favor of one more restatement of the process rules.
- DIRECTIONAL BALANCE (required): after drafting the list, check it as a whole. Include the strongest items pointing EACH way that the research supports — toward YES and toward NO for a binary question; toward higher and lower values otherwise. If the raw research contains a plausibly decision-relevant item pointing against the majority of your list and you have excluded it, that is a selection error: include it. A lopsided list is acceptable ONLY when the research itself contains no credible opposing items — in that case say so explicitly in the Balance Check section below. Do not manufacture balance that the research does not contain; the requirement is that no side's strongest evidence is silently dropped.
- OBSERVATIONS ONLY: every item must be something a source actually states, shows, or prices — a fact, quote, measurement, market price, or the documented rule/mechanism itself. Never emit YOUR OWN inference, extrapolation, or timeline arithmetic (e.g. "the October target implies a September filing") as an [E#] item, even attributed to an evidence plan or labelled "synthesis" — an inference wearing an [E#] label acquires the authority of evidence and every downstream forecaster will cite it as fact. Put such reasoning in ## Derived Implications instead.
- tier is one of: direct (measures the resolution target itself, from the resolution source or confirmed equal to it), adjacent-metric (same family but a different basis/series; state the relationship and any conversion toward the target), near-proxy (close but not identical; say in a few words why not identical), market (prediction-market signal).
- Every item must carry the observation date/period of its value. If a value's date cannot be tied to the period the question asks about, append "(date unverified)" and do NOT label it `direct` — a value reported by a single article without a confirmable current date is not direct evidence.
- Keep exact values, dates, counts, and odds. Never round away precision present in the source.
- When several articles report the same fact (syndicated or near-identical coverage), output ONE item and list every source/URL on that item. Do not repeat the fact.
- End every item with a source-document tag [D1], [D2], ...: items whose claims trace to the same underlying document, report, or dataset share one tag even when they cover different facts or arrive via different URLs (e.g. four extracts from one policy brief are all [D2]). The forecaster uses these tags to weight corroboration by unique sources.
- Exclude weak proxies and background color entirely unless fewer than 5 stronger items exist. A statement of the governing rule/mechanism (or a condition that materially speeds or slows it) is never background color — keep it even if it carries no number of its own.
- Do not place the same fact in more than one item.

## Balance Check
Exactly these three lines, filled in (required — the forecaster reads them; this is the audit trail proving no direction's evidence was dropped):
- Strongest evidence FOR the event / higher values: [E#, E#, ...] — or "none found in the research"
- Strongest evidence AGAINST the event / lower values: [E#, E#, ...] — or "none found in the research"
- Decision-relevant items EXCLUDED from Key Evidence: one short clause each with source and URL (e.g. "LAF entered vacated positions at X — site.com/url — excluded as single-sourced"); write "none" only if nothing plausibly decision-relevant was left out.

## Derived Implications
Omit this section entirely if you have none. Otherwise at most 3 bullets, labelled [I1], [I2], ... — NEVER [E#]. Each is an inference you draw by chaining evidence items (e.g. working a deadline backwards through an interval rule), and must (a) cite the [E#] items it chains and (b) name every load-bearing assumption inline — especially any assumption that a stated minimum, earliest, or latest bound is also the TYPICAL case (e.g. "[I1] From [E3]+[E5], assuming the statutory MINIMUM 15-day gap is also the typical gap — not established by the evidence — an October listing implies a September public filing"). The forecaster re-derives these from the underlying [E#] items; an [I#] is a hypothesis to check, not evidence to cite.

## Market Signals
- One bullet per relevant market: question, current odds, volume/liquidity/open interest when present, URL. Real-money markets (Polymarket, Kalshi) before play-money (Manifold).
- For every market, state in the same bullet whether its resolution condition MATCHES this question's or DIFFERS (broader/narrower/different event). A differing market is a directional floor or ceiling only — and the direction must be DERIVED BY ENTAILMENT, shown in the bullet: if the market's event requires this question's event to happen first (it sits downstream of the resolution event), its price is a FLOOR on this question; if this question's event requires the market's event, a CEILING; if neither entailment holds, write "no bound — context only". Never assert floor/ceiling without the entailment — a misassigned direction inverts how every forecaster uses the market.
- If no market bears directly on the question, say so in one bullet and do not pad with adjacent markets.

## Gaps And Cautions
- At most 6 bullets: missing facts, stale data, conflicting reports, resolution-source access failures, and revision risks.
- If the required artifact is missing or partial, say what that implies for forecast uncertainty in one bullet.

Rules:
- Do not make a probability estimate.
- Do not invent facts absent from the raw research.
- The RESOLUTION SOURCE material is authoritative. When it reports a value for the resolution target, it overrides any secondary source that disagrees. When it shows the target as blank, unpublished, or not-yet-released, say so explicitly and NEVER substitute a secondary or year-ago figure as the resolved value — secondary figures may inform the forecast but are not the resolution value.
- Do not delete a value that was actually retrieved. If it is the wrong exact metric but a related one, keep it as an `adjacent-metric` item with its caveat and conversion path; "not extracted" is only for values that were never found.
- Total output should be materially shorter than the input. Selectivity is the job.
```
