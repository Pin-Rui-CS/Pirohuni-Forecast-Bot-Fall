# Run lineage: data spec for the display

This is everything needed to build the **run trace** view. Its four parts are:
- an API calls table;
- a top-down flow map;
- a step page showing one task's input and output;
- an evidence lifecycle table.

You don't need to know how the bot works. The diagnostic writes one JSON document per diagnosed forecast, and this file describes it.

**Rule for the display:** never hard-code stage names, lane names, or how many of anything there are. The bot's pipeline changes over time. The data already says what fed what, which row each step sits in, and which column it belongs to. Render whatever comes. A stage name you have never seen is normal; show it as text.

Machine-readable schema: [`lineage.schema.json`](lineage.schema.json). Example document: [`examples/lineage-46185.json`](examples/lineage-46185.json).

---

## 1. Where the data lives

| What | Where |
|---|---|
| The lineage document | Supabase table `run_lineage`, column `lineage` (jsonb). One row per `(run_id, question_id)`. Join to `forecasts` on both columns. |
| Step input and output files | Storage bucket `forecast-runs`, at `{run.files_prefix}/steps/{file}.gz`. Each file is gzip-compressed UTF-8 text (markdown or JSON). |
| The checklist (existing) | Table `run_diagnostics` and view `diagnostics_summary`. Lineage checks have `category = 'lineage'`. |

`run_lineage` columns:
- `run_id`
- `question_id`
- `schema_version`, an int (currently `1`)
- `lineage_source`: `declared`, or `legacy-translation`
- `lineage`, the document
- `diagnosed_at`

**Access:** signed-in users can read, as with the other tables. Anonymous users can't. Files are fetched with the same Supabase client used for the existing run files.

## 2. Top level

```jsonc
{
  "schema_version": 1,
  "lineage_source": "declared",            // or "legacy-translation": see §8
  "run":    { ... },                        // question and run metadata
  "lanes":  ["Tavily Search", "AskNews", ...],  // flow-map columns, in display order
  "nodes":  [ ... ],                        // every step (boxes on the map, rows on the step page)
  "edges":  [ ... ],                        // what fed what (arrows)
  "calls":  [ ... ],                        // every external call (the API table)
  "facts":  [ ... ],                        // evidence lifecycle rows
  "losses": { "<stage>": { "<state>": count } }  // where facts were lost, by stage
}
```

`run` fields:

| Field | Meaning |
|---|---|
| `question_id`, `title`, `question_type`, `run_id`, `run_timestamp` | Identity of the forecast. |
| `timings` | `{research_seconds, forecast_seconds, total_seconds}`. |
| `files_prefix` | Storage prefix for this forecast's files. |
| `files_layout` | Always `"{files_prefix}/steps/{file}.gz"`. |
| `layers` | Number of rows in the flow map. |

## 3. Nodes: one per step

```jsonc
{
  "id": "n17",                       // stable within the document; use it in URLs (#step-n17)
  "seq": 17,                         // order the step finished in (null for input nodes)
  "stage": "scrape",                 // free text: what kind of step. Show as-is.
  "label": "https://www.tse.jus.br/…",   // free text: which one (a URL, a section name, "Run 2 · gpt-6.1-sol")
  "kind": "fetch",                   // input | search | fetch | model | code | note  (icon/colour hint)
  "lane": "Tavily Search",           // column; "*" = spans every column; null only for input nodes
  "pass": "main pass",               // free text sub-grouping, e.g. "artifact retry"
  "layer": 4,                        // row, 0 = top
  "group": "Tavily Search|4|scrape", // nodes sharing a group collapse into one box (§6.1)
  "status": "pass",                  // pass | warn | fail | unused
  "status_raw": "ok",                // the bot's own word (ok, thin, truncated, excluded, …)
  "error": "",                       // why it failed or degraded, if anything
  "ts": "2026-10-08T14:10:57",       // when it finished (may be null)
  "duration_s": null,                // may be null on older runs
  "model": null,                     // model name for model steps, when known
  "cuts": [{"kept": 40000, "of": 73513, "reason": "scrape cap (40,000 chars)"}],
  "note": "",
  "output": {"file": "017_scrape_….md", "chars": 5794},        // null when nothing was saved
  "input":  {"file": null, "chars": 8155, "from": ["n16"]},     // see §6.3
  "parents": ["n16"], "children": ["n24"],   // same information as edges, for convenience
  "call_ids": ["c16"],               // external calls this step made
  "dead_end": false,                 // produced output that no later step read
  "gaps": [],                        // inputs it declared that nothing produced (a pipeline bug)
  "inferred": true,                  // true on older runs: links reconstructed, see §8
  "reads": ["url:…"], "writes": ["page:…"]   // raw references; debugging only, no need to show
}
```

**Status colours:** use the existing Diagnostics palette.
- `pass`: green.
- `warn`: amber. It worked, but with a cut, a fallback or thin content.
- `fail`: red.
- `unused`: grey, dashed. The step ran, but its output was deliberately left out.

## 4. Edges: what fed what

```jsonc
{"from": "n16", "to": "n17", "refs": ["url:…"], "ref_count": 1,
 "chars": 8155,      // size of the parent's output that flowed down (null if unknown)
 "cut": false,       // true when the parent cut or compressed text: draw red dashed
 "inferred": true}
```

Edges always point from an earlier step to a later one, so the graph has no cycles. Every edge goes from a smaller `layer` to a larger one, but not always the next row. For example, a section feeds the brief-writer input several rows down.

## 5. Calls: every external call

```jsonc
{"id": "c16", "node_id": "n17",      // the step that made it (null when unknown, mostly older model calls)
 "service": "firecrawl-scrape",      // host or provider name
 "kind": "scrape",                   // http | search | scrape | data | model
 "request": "https://…",             // what was asked (URL, query, API parameters, task label)
 "status_code": 200,                 // HTTP status; null when not recorded
 "started_at": "2026-10-08T14:10:57",
 "duration_s": 3.1,
 "returned": "5,794 chars",          // short human summary
 "outcome": "",                      // error text when it went wrong
 "failed": false,
 "retry_of": null}                   // id of the failed call this one retried
```

A `200` can still be a failure. Show `outcome` next to the code.

## 6. Drawing each view

### 6.1 Flow map

The data is laid out as a grid of `lanes` (columns) by `layer` (rows).
1. **Columns:** one per entry in `lanes`, left to right in the given order.
   - Nodes with `lane: "*"` span all columns. These are merge steps: the check, the brief-writer input, the brief and the forecasts.
   - The input node (`kind: "input"`, `layer: 0`) can be a thin bar across the top, or hidden.
2. **Rows:** `layer`, top to bottom. Every edge points down. Steps are already pulled down to just above the step that uses them, so parallel sections line up in one row before their merge.
3. **Boxes:** collapse all nodes that share a `group` into one box.
   - Give the box a header such as "6 × scrape" or the single label, plus a worst-status dot.
   - List the members as item rows: label (shortened), status dot, `output.chars`, and a ✂ when `cuts` is non-empty.
   - This is what keeps the map readable: seven scrapes become one box.
4. **Arrows:** draw one arrow per pair of boxes that has at least one edge between members.
   - Label it with the summed `chars`, e.g. "32.5k".
   - Draw it red and dashed if any underlying edge has `cut: true`, and add the cut reason on hover.
   - Arrows that skip rows are fine. Route them down the gap between columns, or as straight lines behind boxes.
5. **Click a box or item row:** open a panel showing:
   - status and error;
   - `cuts`;
   - Fed by (`parents`) and Feeds (`children`), each a link that selects that node;
   - an **Open input & output** button linking to the step page (§6.3).

   Highlighting `parents` and `children` on the map is optional.
6. **Optional:** a separator, or a lighter band, where `pass` changes inside a column (e.g. "artifact retry"). It shows the second pass, which sits below the check that triggered it.

### 6.2 API calls table

- **Rows:** `calls`, sorted by `started_at`, nulls last and then in array order.
- **Columns:** service, kind, request, status code, duration (plus a timeline bar from `started_at` and `duration_s` against `run.timings.total_seconds`), returned, outcome.
- **Next →**
  - If `retry_of` points at this call (another call has `retry_of == this.id`), the next chip is that retry: "↻ retry".
  - Otherwise it is the step's children: for each `child` in `nodes[node_id].children`, link to the first call whose `node_id == child`. If the child made no call (a merge or code step), link to the child's step page instead.
- **Open ↗:** the step page of `node_id`.
- **Filters:** by `kind`, and "problems" (`failed`, or `status_code >= 400`, or non-empty `outcome`).

### 6.3 Step page (`#step-<node id>`)

- **Header:** `stage`, `label`, status pill, `kind`, `model`, `duration_s`, `error`, `note`, `cuts`.
- **Navigation:** Fed by = `parents`, Feeds = `children`. Each is a link to that node's step page, so a person can walk the whole pipeline.
- **Calls:** the rows of `calls` whose `node_id` is this node.
- **Output pane:** fetch `{files_prefix}/steps/{output.file}.gz`, gunzip, and show as text, rendering markdown if you like. If `output` is null, show "this step saved no output".
- **Input pane:**
  - If `input.file` is set, fetch that file the same way. That is the exact prompt the step was given.
  - Otherwise, the input was the outputs of `input.from`: show each parent's output file in turn, with the parent's label as a heading.
  - Show `input.chars` → `output.chars`, and the percentage kept.
- **Cuts:** when `cuts` is non-empty, show each as "kept {kept} of {of} chars ({kept/of}) — {reason}" above the panes.
- Files can be large, up to about 200k chars. Lazy-load them, and show the first ~50k with a "show all" toggle.

### 6.4 Evidence lifecycle

- **Rows:** `facts`. Facts with `plan_item: true` are things the evidence plan asked for; show them first, under a "Plan items" heading.
- **Row content:** `claim`, `source`, then the `steps` as a horizontal chain.
  - Each step is `{node_id, state, note?, quote?}`.
  - Show it as a chip with the node's `stage` and short label, coloured by state.
  - Clicking a chip opens that node's step page.
- **Filters:** `final_state`; "lost" (`lost_at != null`); `plan_item`.
- **Summary:** chart `losses` as a bar per stage, stacked by state.
  - This answers "which step loses the most".
  - Across many runs, sum `losses` from every `run_lineage` row.

| state | meaning | colour |
|---|---|---|
| `present` | Every value of the fact is in this step's output. | green |
| `modified` | Some values survived (`note` says how many). | amber |
| `dropped` | A step left it out by choice. Fix that step's prompt. | red |
| `gone` | Lost at a step that cut text or failed. Fix the limit or the failure. | grey, strikethrough |
| `never_fetched` | Plan items only: the plan asked for it and no step retrieved it. Fix retrieval. | red, dashed outline |
| `unknown` | This step's output wasn't saved, so it can't be checked. | faint |

`final_state` is the last state on the chain. `lost_at` is the node where it was dropped, gone or never fetched, or null.

## 7. How facts are found

This section is for interpreting the table; nothing here needs implementing.

- **Facts:** a fact is a line, written by a model or code step, that states at least one value: a number with three or more digits, a decimal, or a percentage. Its source is the nearest URL or host above it. It is identified by its set of values.
- **The chain:** starts at the first step whose output holds all the values, plus the fetched page it came from when that is known. It then follows the route that carries the fact furthest down the map.
- **The loss:** where no next step holds it, that step is where it was lost.
- **Plan items:** come from a Qwen check. Qwen must quote the brief, or the research, word for word, and code verifies the quote.
- **Limits:**
  - The claim text is the original line (up to 240 chars).
  - A reworded fact counts as kept only if its values survive.
  - The 200 facts per run are taken in pipeline order.

## 8. Older runs (`lineage_source: "legacy-translation"`)

Runs made before the bot recorded what each step read have their links reconstructed from a fixed per-stage table. Every node and edge is marked `inferred: true`. Expect some fields to be missing:
- `duration_s`;
- status codes and timings on calls;
- `node_id` on most model calls;
- forecast-run outputs, so facts end in `unknown` at the forecasters.

Show a small "links inferred" note when `lineage_source` isn't `declared`. Nothing else changes.

## 9. Versioning

- `schema_version` only goes up for a breaking change. New optional fields can appear at any time, so ignore fields you don't know.
- New values for `kind`, `stage`, `pass`, `status_raw` and lane names can appear at any time. Render unknown values generically.
