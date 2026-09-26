# apiagent-kit

A research agent that answers factual questions from **24 public data APIs**, with
tiered provenance on every claim. Extracted from a Next.js app into a folder you
can drop into any repo.

It exists to stop one specific failure: a model that answers a factual question
from memory and dresses it up as sourced. Every layer below is shaped by that.

**One runtime dependency: `zod`.** No LLM SDK, no framework, no build step.

---

## Requirements

- **Node 22.6+** (24+ recommended). The package runs TypeScript directly via
  Node's type stripping — there is no compile step and no `dist/`.
- An OpenAI-compatible LLM endpoint, *or* twenty lines implementing `ChatModel`
  against whatever you use. Only needed for the full agent; the toolkit and
  registry work with no model at all.

```bash
cp -r apiagent-kit /path/to/your/repo/
cd /path/to/your/repo/apiagent-kit
npm install
cp .env.example .env      # then edit — see Configuration
```

Verify it works without spending a cent on tokens:

```bash
node examples/tools-only.ts
```

That runs the deterministic matcher and makes a real call to a keyless Tier A
endpoint. If you see rows and a source URL, the package is working.

---

## Three ways to use it

Each layer works without the ones above it. Pick the lowest one that does what
you need.

### 1. The whole agent

```ts
import { openAICompatible, runAgent } from "./apiagent-kit/src/index.ts";

const result = await runAgent({
  model: openAICompatible({
    baseUrl: process.env.LLM_BASE_URL!,
    apiKey: process.env.LLM_API_KEY!,
    model: process.env.LLM_MODEL!,
  }),
  question: "How many US banks failed in August 2026?",
  onEvent: (event) => console.error(event.type, event),
});

console.log(result.text);
console.log(result.usage); // { lookups, calls, failures, tiersUsed }
```

### 2. Just the tools, in an agent you already have

This is the common case for bolting the APIs onto an existing bot. You get two
tool definitions as plain JSON Schema; you run your own loop.

```ts
import { createToolkit } from "./apiagent-kit/src/index.ts";

const toolkit = createToolkit();          // per turn — it holds a data budget

toolkit.definitions;                       // [{ name, description, parameters }]
await toolkit.dispatch(name, args);        // route tool calls back here
toolkit.usage();                           // what the turn spent
```

`dispatch` **never throws.** Failures come back as an object with an `error` key
and an `advice` field telling the model what to do instead — retry with different
params, try another API, or admit it could not be checked. That is deliberate: a
thrown error kills a turn, whereas a readable failure is something the model
recovers from on the next step.

### 3. Just the APIs, no LLM anywhere

```ts
import { find, getAdapter } from "./apiagent-kit/src/index.ts";

find("US bank failures in 2026");          // deterministic shortlist, no model
const adapter = getAdapter("fdic_failures");
const result = await adapter!.run({ from: "2026-01-01", to: "2026-12-31" }, signal);
// { tier: "A", url, retrievedAt, rows, truncated?, note? }
```

---

## Wiring your own model

`ChatModel` is one method. Implement it and `runAgent` works unchanged.

```ts
export interface ChatModel {
  readonly id: string;
  complete(request: ChatRequest): Promise<ChatResponse>;
}
```

`ChatRequest` gives you `{ system, messages, tools, toolChoice, signal }`;
you return `{ text, toolCalls, finishReason?, usage? }`. See
[`src/agent/model.ts`](src/agent/model.ts) for the full types and
[`src/models/openai-compatible.ts`](src/models/openai-compatible.ts) for a
complete reference implementation in ~180 lines.

### Example: Claude via the official SDK

The kit ships no Anthropic dependency, so this lives in your code, not the
package. `npm install @anthropic-ai/sdk`:

```ts
import Anthropic from "@anthropic-ai/sdk";
import type { ChatModel } from "./apiagent-kit/src/index.ts";

export function claude(model = "claude-opus-5"): ChatModel {
  const client = new Anthropic();               // reads ANTHROPIC_API_KEY

  return {
    id: model,
    async complete({ system, messages, tools, toolChoice, signal }) {
      const response = await client.messages.create(
        {
          model,
          max_tokens: 16000,
          system,
          tools: tools.map((t) => ({
            name: t.name,
            description: t.description,
            input_schema: t.parameters as Anthropic.Tool.InputSchema,
          })),
          // "required" -> "any". See the note below before using Fable-tier models.
          tool_choice:
            toolChoice === "required" ? { type: "any" }
            : toolChoice === "none" ? { type: "none" }
            : { type: "auto" },
          messages: messages.map((message) => {
            if (message.role === "user") {
              return { role: "user" as const, content: message.content };
            }
            if (message.role === "assistant") {
              return {
                role: "assistant" as const,
                content: [
                  // Anthropic rejects empty text blocks, so only include it if there is text.
                  ...(message.content ? [{ type: "text" as const, text: message.content }] : []),
                  ...message.toolCalls.map((call) => ({
                    type: "tool_use" as const,
                    id: call.id,
                    name: call.name,
                    input: call.arguments as Record<string, unknown>,
                  })),
                ],
              };
            }
            // ALL results for a step go in ONE user message. Splitting them across
            // messages silently trains Claude to stop making parallel calls.
            return {
              role: "user" as const,
              content: message.results.map((result) => ({
                type: "tool_result" as const,
                tool_use_id: result.callId,
                content: JSON.stringify(result.output),
                is_error: result.isError,
              })),
            };
          }),
        },
        { signal },
      );

      return {
        text: response.content
          .filter((block) => block.type === "text")
          .map((block) => block.text)
          .join(""),
        toolCalls: response.content
          .filter((block) => block.type === "tool_use")
          .map((block) => ({ id: block.id, name: block.name, arguments: block.input })),
        finishReason: response.stop_reason ?? undefined,
        usage: {
          inputTokens: response.usage.input_tokens,
          outputTokens: response.usage.output_tokens,
        },
      };
    },
  };
}
```

> **Forced tool use is not universal.** The loop's first two steps ask for
> `toolChoice: "required"`. Claude spells that `{type: "any"}`, but the
> Fable/Mythos-tier models **reject `any` and `tool` with a 400** — on those, map
> `required` to `{type: "auto"}` and add an instruction naming the tool. Say so
> in your implementation rather than downgrading silently, because the first two
> steps are what stop the model answering from memory.

---

## Configuration

Everything is optional except your LLM credentials. **22 of the 24 APIs need no
credential at all.** See [`.env.example`](.env.example) for the annotated list.

| Variable | Effect if unset |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | The examples refuse to run. The library takes these as arguments, so name them anything in your own code. |
| `APIAGENT_UA_PRODUCT` / `APIAGENT_UA_URL` | **Change these.** They currently name the project this came from. |
| `FRED_API_KEY` | The `fred` adapter switches itself off. |
| `METACULUS_API_TOKEN` | The `metaculus` adapter switches itself off. |
| `GITHUB_TOKEN` | `github` still works, at 60 requests/hour instead of 5000. |
| `SEC_EDGAR_CONTACT` | SEC EDGAR calls omit a contact address and may be throttled. |

An API with no key is treated as **absent, not broken**: it is filtered out of
the shortlist, so the model is never offered something that cannot work.

> **The User-Agent is load-bearing, not cosmetic.** Akamai fronts several of
> these endpoints (FRED among them) and scores requests on User-Agent together
> with source-IP reputation. An uncategorised string from a datacenter range gets
> *tarpitted into 30-second timeouts* rather than refused — which surfaces as an
> unexplained timeout, not a 403. Keep the declared-crawler form
> `Name/version (+https://url)`. The measurement is written up in
> [`src/core/http.ts`](src/core/http.ts).

---

## How the agent works

Six steps, with tools **forced at the start and denied at the end**:

| Step | Tool choice | Why |
|---|---|---|
| 0–1 | `required` | Small models answer from memory unless forced. Step 0 forces the lookup; step 1 forces a call using what the lookup returned — the step models most often skip. |
| 2–4 | `auto` | Free to look further, correct a bad parameter, or answer. |
| 5 | `none` + a different prompt | Otherwise a turn can end on a tool result with no answer after it. The prompt says *why* the budget is gone, or the model signs off mid-thought with "let me check". |

The floor for a real answer is `find_apis → call_api → call_api → answer` — four
steps with no room to recover from a bad parameter. Six leaves one recovery and
one follow-up. Tool calls within a step run **in parallel**; adapters that must
not overlap serialise themselves (GDELT publishes a hard one-request-per-five-
seconds limit and uses the `spaced` gate in `core/http.ts`).

**Two tools, not twenty-four.** Every API is reachable through `call_api`, so the
schema the model sees stays the same size whether there are twelve adapters or a
hundred. `find_apis` narrows twenty-four to six before the model has to choose,
using a deterministic keyword/stem scorer — no model, no network, so API
selection is testable without spending anything.

### Provenance tiers

Set by the adapter, never by the model, so it cannot be talked out of one.

- **A — resolution-grade.** Documented, stable, citable as the answer.
- **B — forecasting input.** Useful evidence, never the source you cite.
- **C — fragile input.** Undocumented internal endpoint. Logged with its
  retrieval URL and timestamp, never cited as resolution, and fails loudly rather
  than silently when its shape changes.

`usage().tiersUsed` lets you flag an answer built only from Tier B sources.

---

## Layout

```
src/
  index.ts                 public surface — start here
  toolkit.ts               find_apis + call_api, provider-neutral
  agent/
    loop.ts                the six-step loop and its phase gates
    model.ts               the ChatModel seam
    prompts.ts             standard of evidence, and today's date
  models/
    openai-compatible.ts   reference ChatModel over plain fetch
  core/
    registry.ts            the 24 adapters + the deterministic matcher
    types.ts               ApiAdapter, defineAdapter, Tier, Domain
    http.ts                shape-checked fetch, caps, rate gates, CSV
    adapters/*.ts          one file per API
examples/
  ask.ts                   full agent, needs an LLM
  tools-only.ts            no LLM, no credentials — the smoke test
```

### Adding an API

Write one file in `core/adapters/`, then add it to the array in
`core/registry.ts`. Nothing else changes — the tool schema is fixed, so the model
never sees a bigger choice.

```ts
import { z } from "zod";
import { defineAdapter } from "../types.ts";
import { getJson, capRows, now, isObject } from "../http.ts";

export const example = defineAdapter({
  id: "example",                    // stable — it travels in transcripts
  name: "Example API",
  tier: "A",
  domain: "statistics",
  answers: "What this can settle, and what it is NOT for.",  // goes to the model verbatim
  keywords: ["example", "demo"],    // matcher fuel, never shown to the model
  paramsSchema: z.object({ from: z.string(), to: z.string() }),
  paramsHelp: "from and to are ISO dates, both inclusive.",  // also verbatim
  async run(params, signal) {
    const data = await getJson<{ items: unknown[] }>(url, {
      signal,
      context: "Example API",
      expect: (v) => isObject(v) && Array.isArray(v.items),
      expected: "an object with an items array",            // prose, for humans
    });
    const { rows, truncated } = capRows(data.items);
    return { tier: "A", url, retrievedAt: now(), rows, truncated };
  },
});
```

Two rules the `http.ts` helpers enforce, both from a real incident:

1. **A 200 is not a success.** Check the *shape*, not just the status — a scrape
   once returned `ok` with a 21-character body reading "Status: 403 Forbidden"
   and the audit logged zero failures. `getJson`'s `expect` is not optional.
2. **Fail loudly.** A shape mismatch throws `ShapeError` carrying the first 200
   characters of what actually arrived, so the tool layer can hand the model a
   real explanation instead of an empty result set.

---

## Constraints to preserve

- **Pure-erasure TypeScript only** — no enums, no constructor parameter
  properties, no namespaces, no decorators. `erasableSyntaxOnly` in
  `tsconfig.json` enforces it. This is what lets Node run the package with no
  build step; breaking it means adding a toolchain.
- **Explicit `.ts` import extensions, no path aliases.** Same reason.
- Run `npx tsc --noEmit` after any change. It checks both rules.

## Relationship to the original

Extracted from `web/src/projects/apiagent/` in the SOCLAAS-Experiments repo.
Everything under `src/core/` is a **verbatim copy** except one deliberate change
— the User-Agent strings in `http.ts` became configurable — so re-syncing an
upstream adapter fix stays a `diff`, not a merge.

What was removed: the Next.js route handler, the `server-only` imports, the
`@/lib/soclaas` gateway binding, the React UI, and a model allowlist specific to
that gateway. What replaced them: the `ChatModel` seam and the provider-neutral
loop.

**Local changes in this repo (not upstream):**
- `core/adapters/treasury-fiscal.ts` — U.S. Treasury Fiscal Data (keyless, Tier A),
  a 25th adapter. `catalog` maps any fiscaldata.treasury.gov dataset page to its API.
- `core/adapters/fred.ts` — optional `limit` param, so a program can take a whole
  series. Agent callers omit it and keep the 25-row cap.

Both exist for the bot's measured-series path (`known_series.py`). Re-syncing
upstream means re-applying these two.

Not carried over (available upstream if you want them): the smoke-test harness
that live-checks all 24 adapters with a worker pool, and the React tool-call
rendering components.
