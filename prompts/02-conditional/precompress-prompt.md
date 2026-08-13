# Section Pre-Compression

**Name in code:** `_PRECOMPRESS_PROMPT` (compiler.py)  
**Source:** [compiler.py:392](../../compiler.py#L392)  
**Tier:** Tier 2 — `anthropic/claude-sonnet-5` (OpenRouter) / `gpt-5.6-terra` (OpenAI)
  
**Call site:** `_PRECOMPRESS_MODEL`, at most `_MAX_PRECOMPRESS_CALLS = 3` calls

## Intended use

**Pipeline stage:** Immediately before the compiler call.

**Trigger:** Fires only when the assembled research exceeds `_COMPILER_INPUT_BUDGET_CHARS = 120,000` and a section must be shrunk to fit.

Condenses one over-budget research section so the compiler can read all of it. Framed as a lossless-as-possible compressor rather than a summariser: every distinct claim, statistic, date, quote, price, source and URL must survive, and directional balance is mandatory.

## Inserts

- `{target_chars}`
- `{name}`
- `{content}`

Chunks are `_PRECOMPRESS_CHUNK_CHARS = 90,000` with a `_PRECOMPRESS_MIN_TARGET_CHARS = 10,000` floor.

## Length

- **Scaffold (this file's template text):** 1,147 chars, 3 slot(s)
- **Filled prompt as sent:** ~1K – 91K chars

## Notes

Known failure mode: the call's `max_tokens` cap has truncated output mid-URL at 5 of 25 entries while the response labelled itself 'all claims retained'. The instruction text is not the problem — the output cap is.

## Template

```text
You are condensing one section of raw research so a downstream evidence compiler
can read all of it within its input budget. You are a lossless-as-possible
compressor, NOT a summarizer.

Rules:
- Keep EVERY distinct factual claim, statistic, date, quoted statement, market
  price, source name, and URL. Exact values and dates must survive verbatim.
- Remove only: navigation/boilerplate text, repeated headers, and content that
  is an exact or near-exact duplicate of content earlier in this same section
  (syndicated reposts of one story collapse to one entry listing the duplicate
  sources).
- DIRECTIONAL BALANCE IS MANDATORY: never drop a claim because it cuts against
  the apparent majority narrative of the section. If claims point both toward
  and against the event in question, both sides must survive compression.
- Preserve the section's heading structure and the original order of items.
- Target length: about {target_chars} characters. Completeness beats the
  target — if honoring the target would force dropping a distinct claim, run
  longer instead and say nothing about it.

Section name: {name}

Section content:
{content}
```
