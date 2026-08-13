# Chain Round — Research Addendum Header

**Name in code:** `ADDENDUM_HEADER_TEMPLATE` (feedback_loop/research_rounds.py)  
**Source:** [feedback_loop/research_rounds.py:35](../../feedback_loop/research_rounds.py#L35)  
**Tier:** n/a — parallel architecture, not wired in

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Prepended to the round-2 research material inside the brief the forecaster reads.

> **Not in the pipeline.** Part of the unwired `feedback_loop/` package.

## Intended use

**Pipeline stage:** Would be: research round 2 of the feedback loop.

Labels gap-round material so the forecaster knows it was retrieved specifically for the previous round's open questions, and states the precedence rule: where the addendum corrects or redates an earlier claim, the newer scrape wins.

## Inserts

- `{round_no}`
- `{date}`
- `{provider}`
- `{gap_lines}`
- `{content}`

`{round_no}`, `{date}`, `{provider}`, `{gap_lines}`, `{content}`.

## Length

- **Scaffold (this file's template text):** 448 chars, 5 slot(s)
- **Filled prompt as sent:** ~0.5K chars (plus the retrieved content)

## Template

```text


---

# Gap-Round {round_no} Addendum (retrieved {date} for the round-{round_no} forecaster's open questions; via {provider})

The round-{round_no} forecaster identified the gaps below as missing but plausibly published, and this material was retrieved specifically for them. Weigh it together with the original brief above; where it corrects or redates an earlier claim, this newer scrape wins.

Gaps this addendum targets:
{gap_lines}

{content}
```
