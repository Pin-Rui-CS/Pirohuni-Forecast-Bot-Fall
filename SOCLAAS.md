# SoCLaaS API Reference (condensed)

Portable reference for the NUS SoC LLM-as-a-Service gateway. Distilled from
DocHub `cf/guides/soclaas/*` (pages last modified Aug 2026). Source pages are
NUS-network-only: <https://dochub.comp.nus.edu.sg/cf/guides/soclaas/start>

Drop this file into a repo root, or rename it `AGENTS.md` / `CLAUDE.md` /
`.cursorrules` so any coding agent picks it up as context.

---

## What it is

An OpenAI-compatible LLM API gateway run by NUS School of Computing, serving
open-weight models. Free — no billing, no real money charged. Intended for
teaching, learning, experiments, and prototypes. Works with any OpenAI-compatible
client or SDK.

Also provides Whisper audio transcription.

## Setup

```bash
export SOCLAAS_API_KEY="clsk_<prefix>_<secret>"
export SOCLAAS_BASE_URL="https://soclaas-api.comp.nus.edu.sg/v1"
```

Auth header on every `/v1/*` request:

```
Authorization: Bearer <soclaas-api-key>
```

Key format is `clsk_<prefix>_<secret>`.

Python:

```python
from openai import OpenAI
import os

client = OpenAI(
    api_key=os.environ["SOCLAAS_API_KEY"],
    base_url=os.environ["SOCLAAS_BASE_URL"],
)
```

## Endpoints

Only `/v1/*` is for application users.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/models` | Model catalogue visible to this key |
| POST | `/v1/chat/completions` | Primary inference endpoint |
| POST | `/v1/responses` | OpenAI Responses API shim |
| POST | `/v1/embeddings` | Vector embeddings |
| POST | `/v1/audio/transcriptions` | Whisper transcription (multipart) |

Portal API (separate host) for key/budget info:
`https://soclaas-portal.comp.nus.edu.sg/api-key/budget`

---

### GET /v1/models

```bash
curl -sS "$SOCLAAS_BASE_URL/models" \
  -H "Authorization: Bearer $SOCLAAS_API_KEY" | jq
```

Returns standard OpenAI model fields plus a `soclaas` namespace
(`display_name`, `description`, which may be blank) and context-length fields
(`context_length`, `context_window`, `max_context_tokens`, `max_model_len`).

**Always use the `id` values returned here.** Public model names are
operator-managed aliases and can change. If a key can't use a model, that model
does not appear in the list.

### POST /v1/chat/completions

The low-transformation path — best choice for plain chat clients.

```bash
curl "$SOCLAAS_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $SOCLAAS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [
      {"role": "system", "content": "Be concise."},
      {"role": "user", "content": "Say hello in one sentence."}
    ],
    "reasoning_effort": "none"
  }'
```

- `model` is required; omitting it returns 400.
- Streaming via standard SSE with `"stream": true`.
- On streaming requests the gateway forces upstream
  `stream_options.include_usage=true`; on non-streaming requests it strips
  `stream_options` before proxying.
- The gateway does **not** execute tools on this path.

### POST /v1/responses

Compatibility shim: translated into chat-completions traffic internally, then
translated back into Responses-shaped JSON or SSE.

```bash
curl "$SOCLAAS_BASE_URL/responses" \
  -H "Authorization: Bearer $SOCLAAS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.1:8b", "input": "Summarize SoCLaaS in one sentence."}'
```

Supported fields: `model`, `input`, `instructions`, `stream`,
`max_output_tokens`, `temperature`, `top_p`, `tools`, `tool_choice`,
`parallel_tool_calls`, `previous_response_id`.

Input rules: a string `input` becomes one user message; `instructions` becomes a
system message; array `input` is supported for message-style items and tool
outputs.

**Limitations — read before building agent loops on this:**

- `background=true` → 400, not supported.
- Response state is **not durable**. `previous_response_id` lives in one gateway
  process only, in memory, with a 15-minute TTL, and does not survive restarts.
  Unknown or expired ID → 400.
- Persisted response retrieval, response cancellation, and hosted OpenAI
  built-in tools are not implemented.

**Tools.** Client-executed (returned to you, gateway does not run them): OpenAI
function tools, custom tools, `shell` / `local_shell`, `apply_patch`.
Gateway-executed web tools: `web_fetch`, `web_search`, `web_search_preview` —
documented as working only if enabled globally *and* allowed by your key's
policy.

> **Tested 2026-09-01: they are NOT implemented on this deployment.** Sending
> `tools: [{"type": "web_search"}]` returns 200 with no `web_search_call` in the
> output — the declaration is silently dropped and the model narrates a search
> it never ran, which is worse than an error. Forcing one via `tool_choice`
> returns 400, and the error leaks the reason: `/v1/responses` is translated
> straight into chat/completions, and only `{"type": "function", "function":
> {"name": ...}}` is understood. Treat the gateway as executing **no** tools.

**Function calling does work**, on both endpoints. Tool definitions are forwarded
and tool calls come back for you to execute. Verified on `llama3.1:8b`,
`qwen3.6:35b`, `gemma4:26b`, and `qwen3.8:27b` — all four emit well-formed calls
when `tool_choice` forces one. Unforced, the larger models often answer from
memory instead, so a system prompt that says when to reach for a tool matters.
Anything agentic here means running the tools yourself.

> Emitting one well-formed call is **not** the same as being able to drive a
> multi-step loop, and the two come apart on this gateway — `llama3.1:8b` has
> the first ability and not the second. See *Driving a multi-step tool loop*
> under Models before choosing a model for anything agentic.

### POST /v1/embeddings

```bash
curl --fail-with-body -X POST "$SOCLAAS_BASE_URL/embeddings" \
  -H "Authorization: Bearer $SOCLAAS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<embedding-model-id>",
    "input": "SoCLaaS provides controlled access to LLM backends.",
    "encoding_format": "float"
  }'
```

- `input` takes one string or an array of strings (batching). Token-ID input is
  forwarded when the backend supports it.
- Optional forwarded fields: `encoding_format`, `dimensions`, `user`.
- The model must have the embeddings capability — a chat-only model will not
  work.
- The `model` in the *response* is the upstream provider ID, not the SoCLaaS
  public ID. Don't infer access from it; use `/v1/models`.

### POST /v1/audio/transcriptions

`multipart/form-data`, not JSON.

```bash
curl --fail-with-body -X POST "$SOCLAAS_BASE_URL/audio/transcriptions" \
  -H "Authorization: Bearer $SOCLAAS_API_KEY" \
  -F "model=<whisper-model-id>" \
  -F "file=@./recording.mp3" \
  -F "language=en" \
  -F "response_format=json"
```

Required: `file` (one file), `model`. Optional forwarded: `language`, `prompt`,
`response_format` (`json`, `text`, `verbose_json`, `srt`, `vtt`), `temperature`,
`timestamp_granularities[]`, `stream`.

Streaming: add `-F "stream=true"` with `-H "Accept: text/event-stream"` and
`--no-buffer`; SSE passes through untransformed.

Upload limit 50 MiB by default → 413 if exceeded. Errors: 400 for missing model,
missing/multiple file parts, malformed multipart, or a model without the
transcription capability; 403 for a policy-denied model.

---

## Models

Costs are **microdollars per million tokens**, used only for rate control — no
real money is charged.

| Model | Context | Input | Output | Recommended for |
| --- | --- | --- | --- | --- |
| `llama3.1:8b` | 65536 | 20000 | 30000 | Capable small-model experimentation |
| `qwen3.5:9b` | 65536 | 100000 | 150000 | Higher-quality general-purpose and multimodal |
| `gemma4:26b` | 131072 | 60000 | 300000 | Higher-quality general-purpose and multimodal |
| `qwen3-coder-next` | 196608 | 110000 | 800000 | Coding agents, software-engineering workflows |
| `qwen3.6:35b` | 262144 | 140000 | 900000 | Efficient agentic workflows, advanced coding |
| `qwen3.8:27b` | 262144 | 195000 | 900000 | High-quality general reasoning and multimodal |
| `qwen3-vl:32b` | 80000 | 104000 | 416000 | Vision-capable |
| `ornith:35b` | 262144 | 260000 | 900000 | Agentic software engineering and coding |

Picking one:

- **Learning the API** → `llama3.1:8b`. Small, fast, cheap; fine for prompts,
  streaming, structured output.
- **Interactive / Open WebUI** → best all-round conversational model; or
  `gemma4:26b` to emphasise document understanding and vision.
- **Coding agent (Codex, OpenCode)** → `qwen3-coder-next`; the `ornith` 35B for
  more advanced multi-step workflows.
- **General agent (OpenClaw, Hermes)** → `qwen3.6:35b`, MoE, good
  capability/efficiency balance for planning and tool calling.

> ⚠️ The DocHub models page contradicts itself: its table lists `qwen3.8:27b` and
> `ornith:35b`, while the recommendations below it say `qwen3.6:27b` and
> `ornith1.0:35b`. Resolve against `GET /v1/models` — that's authoritative.

### Driving a multi-step tool loop

A stricter bar than "can it emit a tool call". That question is whether the model
produces well-formed JSON; this one is whether it can run a dispatcher — call a
lookup tool, read a shortlist back, then construct valid parameters for something
it was told about only in that reply. Those are different abilities, and the
smaller models have the first without the second.

Measured 2026-09-06. Same question to each ("How many US banks failed between
January and August 2026?", true answer four), two tools, six steps:

| Model | | Result |
| --- | --- | --- |
| `gemma4:26b` | PASS | Found the source, called it, 4 rows, cited it correctly. |
| `qwen3.6:35b` | PASS | One call rejected on parameters; it read the error, corrected it, then answered. The recovery path working. |
| `qwen3.8:27b` | PASS | Clean on the first attempt. |
| `llama3.1:8b` | **FAIL** | Called the lookup, was handed the right source, then never called it — and answered "0 bank failures" with an invented URL, an invented query date, and the sentence "This answer rests on Tier A evidence". Forcing `tool_choice` did not fix it: it then emitted text *shaped* like a tool call, with parameter names the schema does not have, as its answer. |

A model missing from this list is unverified, not known-broken. Add one once you
have watched it complete a turn with a real tool result behind the answer.

**A loop shape that works.** Tools forced at the start, denied at the end:

| Step | `tool_choice` | Why |
| --- | --- | --- |
| 0–1 | `required` | Step 0 forces the lookup. Step 1 forces a call *using what the lookup returned* — the step models most often skip. |
| middle | `auto` | Free to look further, fix a bad parameter, or answer. |
| last | `none` | Otherwise a turn can end on a tool result with no answer after it. |

Two details that are load-bearing:

- **On the final step, swap the system prompt as well as denying tools.** Denial
  alone is not enough — measured on `qwen3.8:27b`, a model denied tools without
  being *told why* signs off mid-thought with "let me check" and the reader gets
  nothing. Say the budget is gone and to answer with what it already has.
- **Budget about six steps.** The floor for a researched answer is
  lookup → call → call → answer, which is already four with no room to correct a
  bad parameter. Six leaves one recovery and one follow-up.

**Every step is a request against the 30/minute limit below**, and all of them
share one wall clock with the final answer. A six-step loop with parallel tool
calls inside each step is a realistic way to hit the rate limit from a single
user action.

## Usage limits

Documented defaults:

- 90 requests per minute
- 80,000,000 microdollars ($80) per day
- 800,000,000 microdollars ($800) per month

> **Do not trust those numbers.** Measured against a real key on 2026-09-02, the
> `default` policy actually returns **30 requests/minute, $50/day, $500/month** —
> well under what the docs advertise. Read the budget endpoint below rather than
> assuming; the rate limit in particular matters as soon as one user action costs
> several requests, as any tool-calling loop does.

Also constrained by lifetime credits and model-level access policy. Exceeding any
limit rejects the request (429).

Windows reset on **UTC** boundaries: daily at 00:00 UTC = 08:00 SGT; monthly on
the 1st at 08:00 SGT. Usage is accounted against the authenticated API key.

Check actual limits:

```bash
curl -sS "https://soclaas-portal.comp.nus.edu.sg/api-key/budget" \
  -H "Authorization: Bearer $SOCLAAS_API_KEY" \
  -H "Content-Type: application/json"
```

Limits exist for fair use, not cost recovery, and are adjusted over time.

## Errors

Shape: `{"error": "message"}`

| Status | Causes |
| --- | --- |
| 400 | Invalid JSON, missing `model`, unsupported Responses feature, expired `previous_response_id` |
| 401 | `missing bearer token`, or `invalid API key` (invalid/revoked) |
| 403 | Model not allowed, username mapping failed, forced web tool not allowed |
| 429 | Rate limit or quota exceeded |
| 503 | Auth backend unavailable, policy enforcement unavailable, web search provider not configured |
| 5xx | Transient gateway/upstream issue — retry if the request is safe to retry |

## Good practice

- Use model IDs from `GET /v1/models`, not hardcoded names from docs.
- Retry 429 and 5xx with backoff.
- Set client-side request timeouts.
- Test with small prompts before wiring into a larger workflow.
- Keep the key in an env var. Never commit it, and don't ship it in a deployed
  service others can call — usage is billed to your key's quota.

## Third-party client configs

### OpenCode

`~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "soclaas/qwen3-coder-next",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "SoCLaaS",
      "options": {
        "baseURL": "https://soclaas-api.comp.nus.edu.sg/v1",
        "apiKey": "<your SoCLaaS API key>"
      },
      "models": {
        "qwen3-coder-next": {
          "name": "qwen3-coder-next",
          "limit": { "context": 262144, "output": 32768 }
        }
      }
    }
  }
}
```

If it doesn't default to SoCLaaS, run `/models` and pick from the menu.

### Hermes Agent

`~/.hermes/config.yaml`:

```yaml
model:
  default: qwen3.6:35b
  provider: custom:soclaas
custom_providers:
  - name: SoCLaaS
    base_url: https://soclaas-api.comp.nus.edu.sg/v1
    api_key: <your SoCLaaS API key>
    model: qwen3.6:35b
    api_mode: chat_completions
    models:
      - qwen3.6:35b
```

Codex and Open WebUI are documented on DocHub separately; any OpenAI-compatible
client works with the same base URL and key.

## Access notes

DocHub documentation is reachable only from inside the NUS network. Whether the
gateway itself is similarly restricted is not stated in the docs — test
`GET /v1/models` from off-campus before depending on it.
