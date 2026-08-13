# Chain Round — Prior Round Appendix

**Name in code:** `PRIOR_ROUND_APPENDIX_TEMPLATE` (feedback_loop/gap_analysis.py)  
**Source:** [feedback_loop/gap_analysis.py:233](../../feedback_loop/gap_analysis.py#L233)  
**Tier:** n/a — parallel architecture, not wired in

> **This is a fragment, not a standalone prompt.** It is appended to (or formatted
> into) another prompt at runtime and is never sent on its own.
>  
> Appended by `_build_chain_prompt` to the round-2 forecaster prompt.

> **Not in the pipeline.** Part of the unwired `feedback_loop/` package.

## Intended use

**Pipeline stage:** Would be: forecast round 2 of the feedback loop.

Hands round 2 the prior analyst's reasoning with **its numeric estimates stripped out** so they cannot anchor, and requires an independent analysis first, then a 'PRIOR ROUND AUDIT' section. The anchoring-control design here has no equivalent anywhere in the live pipeline.

## Inserts

- `{round_no}`
- `{masked_rationale}`

`{round_no}` and the estimate-stripped prior analysis.

## Length

- **Scaffold (this file's template text):** 1,059 chars, 2 slot(s)
- **Filled prompt as sent:** ~1.1K chars (plus the base prompt and the prior analysis)

## Template

```text


---

## APPENDIX — PRIOR ANALYST ROUND {round_no} (estimates removed)

A prior analyst produced the analysis below on an EARLIER version of the
research brief. Its numeric estimates have been removed on purpose so they
cannot anchor you. The research brief above now contains a "Gap-Round
Addendum" section holding material retrieved specifically to answer this
analyst's open questions — weigh that addendum explicitly.

Instructions:
1. Complete your own full phased analysis FIRST, deriving your own estimate
   from the evidence alone.
2. Then audit the appendix for errors: misread or misdated evidence,
   double-counted signals, missed resolution-mechanics branches, base rates
   asserted without a numerator and denominator.
3. Add a short section titled "PRIOR ROUND AUDIT" stating which of the prior
   analyst's claims you accept and which you reject, with reasons. Adjust your
   estimate only where the appendix's REASONING (never a number — there are
   none) reveals something you genuinely missed.

Prior round rationale:
{masked_rationale}
```
