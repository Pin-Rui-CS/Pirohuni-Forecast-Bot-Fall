"""Narrow Qwen tasks whose every answer code verifies.

Qwen never grades quality. It only extracts (rows, calculations, quotes) or
classifies (relevant: yes/no) into strict JSON, and code checks each answer
against the run's own text: a quote must appear verbatim, a calculation is
re-evaluated, a year/value row must appear in its source. An answer that fails
verification counts against the claim it was supposed to support.

SoCLaaS Qwen only. The route is asserted before any call; the CLI blanks the
paid keys before importing anything, so a stray paid call fails rather than
spends.
"""
from __future__ import annotations

import json
import logging
import re

import llm_provider
from diagnostics.checks_condensation import _numbers
from diagnostics.checks_evidence import history_coverage
from diagnostics.checks_forecast import _BASE_RATE, _evaluate
from diagnostics.loader import RunArtifacts
from diagnostics.result import FAIL, INFO, PASS, WARN, Check, skipped
from llm_client import call_llm

logger = logging.getLogger(__name__)
_MAX_SOURCE_CHARS = 60_000


def qwen_available() -> bool:
    route = llm_provider.route_for_model(llm_provider.QWEN_RESEARCH_MODEL)
    return route.endpoint.label == "SoCLaaS" and bool(llm_provider.api_key_for(route.endpoint))


async def _ask(task: str, prompt: str, *, max_tokens: int = 4000):
    """One Qwen call returning parsed JSON (list or dict), or None."""
    route = llm_provider.route_for(llm_provider.QWEN_RESEARCH_MODEL, label=f"diagnostics/{task}")
    if route.endpoint.label != "SoCLaaS":  # belt and braces: never a paid endpoint
        raise RuntimeError(f"diagnostics/{task} resolved to {route.endpoint.label}, not SoCLaaS")
    text = await call_llm(prompt, model=llm_provider.QWEN_RESEARCH_MODEL, temperature=0.1,
                          _label=f"diagnostics/{task}", max_tokens=max_tokens)
    match = re.search(r"(\[.*\]|\{.*\})", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*`_|]", " ", str(text or ""))).strip().lower()


def _quoted(quote: str, source: str) -> bool:
    q = _norm(quote)
    return len(q) >= 8 and q in _norm(source)


async def plan_items(a: RunArtifacts) -> Check:
    """Which evidence-plan requests the brief actually contains (item 12)."""
    title = "Evidence the plan asked for is in the brief"
    artifact = next((b[5:].strip() for b in a.plan_list("Required Evidence Artifact")
                     if b.lower().startswith("name:")), "")
    items = ([("artifact", artifact)] if artifact else []) + \
            [(f"direct {i}", b) for i, b in enumerate(a.plan_list("Direct Evidence To Find"), 1)]
    items = items[:8]
    if not items or not a.brief:
        return skipped("evidence.plan_items", "evidence", title, "no plan items or no brief", "qwen")
    listing = "\n".join(f"{i}. {text}" for i, (_, text) in enumerate(items))
    answer = await _ask("plan-items", f"""For each requested item, say whether the BRIEF contains it.
If it does, copy one short sentence or table row from the BRIEF verbatim as proof.

REQUESTED ITEMS:
{listing}

BRIEF:
<<<
{a.brief[:_MAX_SOURCE_CHARS]}
>>>

Reply with JSON only: [{{"item": <number>, "found": true|false, "quote": "<verbatim text from the brief, or empty>"}}]""")
    if not isinstance(answer, list):
        return skipped("evidence.plan_items", "evidence", title, "Qwen gave no usable JSON", "qwen")
    verdict = {}
    for row in answer:
        try:
            idx = int(row.get("item"))
        except (TypeError, ValueError, AttributeError):
            continue
        claimed = bool(row.get("found"))
        verified = claimed and _quoted(row.get("quote", ""), a.brief)
        verdict[idx] = (claimed, verified, str(row.get("quote") or "")[:160])
    lines, missing = [], []
    for i, (kind, text) in enumerate(items):
        claimed, verified, quote = verdict.get(i, (False, False, ""))
        state = "found" if verified else "NOT found" + (" (quote did not verify)" if claimed else "")
        lines.append(f"[{kind}] {state}: {text[:120]}" + (f' -- "{quote}"' if verified else ""))
        if not verified:
            missing.append(kind)
    status = FAIL if "artifact" in missing else WARN if missing else PASS
    return Check("evidence.plan_items", "evidence", title, status,
                 f"{len(items) - len(missing)}/{len(items)} requested items verified in the brief",
                 value={"missing": missing, "items": len(items)}, evidence=lines, method="qwen")


def _row_verified(year: str, value: str, quote: str, source: str, *, lookback: int = 2000) -> bool:
    """A year/value row counts only if the quote is real, holds the value, and the
    year is in the quote or shortly before it (results pages put the year in a
    heading, e.g. '### TERMINATOR' under a 2021 results page)."""
    number = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    if not number or not _quoted(quote, source):
        return False
    if number.group(0).replace(",", "") not in quote.replace(",", ""):
        return False
    if year in quote:
        return True
    flat_source, flat_quote = _norm(source), _norm(quote)
    at = flat_source.find(flat_quote)
    return at >= 0 and year in flat_source[max(0, at - lookback): at]


async def history_rows(a: RunArtifacts) -> Check:
    """Year -> value rows for the required artifact: in research vs in the brief vs used (item 10)."""
    title = "Historical rows obtained vs used"
    artifact = next((b[5:].strip() for b in a.plan_list("Required Evidence Artifact")
                     if b.lower().startswith("name:")), "")
    if not artifact:
        return skipped("evidence.history_rows", "evidence", title, "no required artifact named", "qwen")
    # Retry and resolution sections first: that is where history tends to land.
    sections = sorted((p for p in a.providers if p[0] != "Evidence Plan"),
                      key=lambda p: (("retry" not in p[0].lower()) + ("resolution" not in p[0].lower())))
    # Up to 3 chunks of the research, so a long section cannot hide the rest.
    chunks, current = [], ""
    for name, content in sections:
        block = f"### {name}\n{content}\n\n"
        while block:
            room = _MAX_SOURCE_CHARS - len(current)
            current, block = current + block[:room], block[room:]
            if len(current) >= _MAX_SOURCE_CHARS:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    research_years, answered = set(), False
    for source in chunks[:3]:
        answer = await _ask("history-rows", f"""List every dated data point in the RESEARCH for this series:
"{artifact}"

One entry per year. Copy the value exactly as written, and quote the sentence or table row it appears in.
Do not include years whose value is missing, unknown or not yet published.

RESEARCH:
<<<
{source}
>>>

Reply with JSON only: [{{"year": <yyyy>, "value": "<as written>", "quote": "<verbatim>"}}]""", max_tokens=5000)
        if not isinstance(answer, list):
            continue
        answered = True
        for row in answer:
            try:
                year, value, quote = str(int(row.get("year"))), str(row.get("value") or ""), str(row.get("quote") or "")
            except (TypeError, ValueError, AttributeError):
                continue
            if _row_verified(year, value, quote, source):
                research_years.add(year)
    if not answered:
        return skipped("evidence.history_rows", "evidence", title, "Qwen gave no usable JSON", "qwen")
    brief_years = set((history_coverage(a).value or {}).get("years") or [])
    denominators = [int(m.group(2)) for _, text in a.forecast_runs for m in [_BASE_RATE.search(text)] if m]
    lost = sorted(research_years - brief_years)
    status = WARN if lost or (denominators and min(denominators) < len(research_years)) else PASS
    if not research_years:
        status = INFO
    return Check("evidence.history_rows", "evidence", title, status,
                 f"research held {len(research_years)} verified year(s), the brief {len(brief_years)}, "
                 f"forecasters used D={min(denominators) if denominators else 'n/a'}",
                 value={"research_years": sorted(research_years), "brief_years": sorted(brief_years),
                        "denominators": denominators},
                 evidence=[f"in research but not the brief: {', '.join(lost)}"] if lost else [], method="qwen")


async def calculations(a: RunArtifacts) -> Check:
    """Qwen finds the calculations; code re-evaluates them (item 11)."""
    title = "Calculations in the forecast runs are correct"
    runs = [(n, t) for n, t in a.forecast_runs if len(t) > 500][:4]
    if not runs:
        return skipped("forecast.calculations", "forecast", title, "no run transcripts", "qwen")
    checked, wrong, unverified = 0, [], 0
    for n, text in runs:
        answer = await _ask("calculations", f"""Extract every arithmetic calculation the author wrote in this text,
where all inputs and the result are numbers. Rewrite each as a plain expression using only
numbers and + - * / ( ). Use decimals for percentages (26% -> 0.26). Quote the original line.

TEXT:
<<<
{text[:30_000]}
>>>

Reply with JSON only: [{{"expression": "<e.g. 0.98*0.265+0.02*0>", "claimed": <number as the author wrote it>, "quote": "<verbatim line>"}}]""")
        for row in answer if isinstance(answer, list) else []:
            if not isinstance(row, dict) or not _quoted(str(row.get("quote") or ""), text):
                unverified += 1
                continue
            result = _evaluate(str(row.get("expression") or ""))
            try:
                claim = float(str(row.get("claimed")).replace(",", "").rstrip("%"))
            except ValueError:
                continue
            if result is None:
                continue
            checked += 1
            tolerance = max(0.51, 0.02 * abs(claim))
            if not any(abs(c - claim) <= tolerance for c in (result, result * 100, result / 100)):
                wrong.append(f"run {n}: {row.get('quote', '')[:120]} -> {row.get('expression')} = {result:.4g}, "
                             f"written {row.get('claimed')}")
    status = FAIL if wrong else PASS if checked else INFO
    return Check("forecast.calculations", "forecast", title, status,
                 f"{checked} calculation(s) re-evaluated, {len(wrong)} wrong ({unverified} extraction(s) "
                 "discarded: quote not found)",
                 value={"checked": checked, "wrong": len(wrong)}, evidence=wrong[:10], method="qwen")


async def dropped_numbers(a: RunArtifacts) -> Check:
    """Of numbers the brief writer saw but the brief dropped, which mattered (item 7)."""
    title = "Decision-relevant numbers dropped by the brief"
    compiler = a.events("compiler_input")
    if not compiler or not a.brief:
        return skipped("condensation.dropped_numbers", "condensation", title, "no compiler input or brief", "qwen")
    source = a.payload(compiler[-1])
    dropped = _numbers(source) - _numbers(a.brief)
    samples = []
    for number in sorted(dropped, key=lambda n: source.find(n)):
        pos = source.replace(",", "").find(number)
        flat = source.replace(",", "")
        if pos < 0:
            continue
        samples.append((number, re.sub(r"\s+", " ", flat[max(0, pos - 120): pos + 120])))
        if len(samples) >= 20:
            break
    if not samples:
        return Check("condensation.dropped_numbers", "condensation", title, PASS, "no dropped numbers", method="qwen")
    listing = "\n".join(f"{i}. [{n}] ...{ctx}..." for i, (n, ctx) in enumerate(samples))
    answer = await _ask("dropped-numbers", f"""Question being forecast: {a.question.get('title', '')}

Each line shows a number (in brackets) with its surrounding text. For each, answer whether that
number would directly help forecast the question (a relevant count, value, date-bound figure,
price, threshold or rate) -- not page furniture, IDs or unrelated facts.

{listing}

Reply with JSON only: [{{"id": <number>, "relevant": true|false}}]""", max_tokens=2000)
    if not isinstance(answer, list):
        return skipped("condensation.dropped_numbers", "condensation", title, "Qwen gave no usable JSON", "qwen")
    relevant = []
    for row in answer:
        try:
            if row.get("relevant"):
                relevant.append(samples[int(row.get("id"))])
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
    status = WARN if len(relevant) >= 3 else PASS
    return Check("condensation.dropped_numbers", "condensation", title, status,
                 f"{len(relevant)} of {len(samples)} sampled dropped numbers judged relevant "
                 f"({len(dropped)} numbers dropped in total)",
                 value={"sampled": len(samples), "relevant": len(relevant), "dropped_total": len(dropped)},
                 evidence=[f"[{n}] ...{ctx[60:200]}..." for n, ctx in relevant[:10]], method="qwen")


TASKS = (plan_items, history_rows, calculations, dropped_numbers)
