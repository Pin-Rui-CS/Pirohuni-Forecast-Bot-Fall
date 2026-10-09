"""Forecast runs: base-rate denominators, internal consistency, arithmetic."""
from __future__ import annotations

import re
import statistics

from diagnostics.loader import RunArtifacts
from diagnostics.result import FAIL, INFO, PASS, WARN, Check, skipped

CAT = "forecast"
_BASE_RATE = re.compile(r"Base rate cases:?\**\s*\**\s*(\d+)\s*(?:of|/|out of)\s*(\d+)", re.I)
_FINAL_PROB = re.compile(r"^\s*Probability:\s*(\d+(?:\.\d+)?)\s*%", re.M)
_OPERAND = r"\d+(?:[.,]\d+)*%?"
_ARITH = re.compile(rf"((?:{_OPERAND}\s*[×\*÷/+\-−]\s*)+{_OPERAND})\s*(?:≈|=)\s*(-?\d+(?:[.,]\d+)*)\s*(%?)")


def base_rate(a: RunArtifacts) -> Check:
    title = "Base rate denominator stated and adequate"
    runs = a.forecast_runs
    if not runs:
        return skipped(f"{CAT}.base_rate", CAT, title, "no run transcripts")
    stated = {}
    for n, text in runs:
        match = _BASE_RATE.search(text)
        if match:
            stated[n] = (int(match.group(1)), int(match.group(2)))
    if a.forecast.get("question_type") != "binary" and not stated:
        return Check(f"{CAT}.base_rate", CAT, title, INFO,
                     "only the binary prompt asks for 'N of D'; none stated here")
    small = {n: nd for n, nd in stated.items() if nd[1] < 5}
    missing = [n for n, _ in runs if n not in stated and len(_) > 500]
    status = WARN if small or missing else PASS
    return Check(f"{CAT}.base_rate", CAT, title, status,
                 ", ".join(f"run {n}: {k} of {d}" for n, (k, d) in stated.items()) or "none stated",
                 value={"stated": {str(n): list(v) for n, v in stated.items()}, "not_stated": missing},
                 evidence=[f"run {n}: denominator {d} (fewer than 5 cases)" for n, (_, d) in small.items()] +
                          [f"run {n}: no 'Base rate cases: N of D' line" for n in missing])


def consistency(a: RunArtifacts) -> Check:
    title = "Stated, extracted and submitted forecasts agree"
    qtype = a.forecast.get("question_type")
    members = (a.forecast.get("extra") or {}).get("ensemble") or []
    issues = []
    if qtype == "binary":
        texts = dict(a.forecast_runs)
        for i, member in enumerate(members, 1):
            if member.get("dropped") or "probability" not in member:
                continue
            stated = _FINAL_PROB.findall(texts.get(i, ""))
            if stated and abs(float(stated[-1]) - float(member["probability"])) > 0.5:
                issues.append(f"run {i}: wrote {stated[-1]}% but {member['probability']}% was extracted")
        values = [float(v) for v in a.forecast.get("run_values") or []]
        final = a.forecast.get("final_forecast")
        tiebreak = (a.forecast.get("extra") or {}).get("tiebreaker_used")
        if values and final is not None and not tiebreak and abs(statistics.median(values) - float(final)) > 0.005:
            issues.append(f"submitted {final} is not the median of {values}")
    elif qtype == "multiple_choice":
        for i, value in enumerate(a.forecast.get("run_values") or [], 1):
            total = sum(float(v) for v in (value or {}).values())
            if abs(total - 1) > 0.02:
                issues.append(f"run {i}: option probabilities sum to {total:.3f}")
    else:
        return Check(f"{CAT}.consistency", CAT, title, INFO, "numeric: checked by the answer-space witness at run time")
    return Check(f"{CAT}.consistency", CAT, title, FAIL if issues else PASS,
                 f"{len(issues)} inconsistency(ies)", evidence=issues)


def _evaluate(expression: str) -> float | None:
    expr = (expression.replace("×", "*").replace("÷", "/").replace("−", "-")
            .replace("%", "").replace(",", ""))
    if not re.fullmatch(r"[\d.\s*/+\-()]+", expr):
        return None
    try:
        return float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - digits and operators only
    except (SyntaxError, ZeroDivisionError, ValueError, TypeError):
        return None


def arithmetic(a: RunArtifacts) -> Check:
    """Re-evaluate every inline 'a op b ≈ c' the forecasters wrote (item 11, code part)."""
    title = "Inline arithmetic in the forecast runs is correct"
    runs = a.forecast_runs
    if not runs:
        return skipped(f"{CAT}.arithmetic", CAT, title, "no run transcripts")
    checked, wrong = 0, []
    for n, text in runs:
        for expression, claimed, _pct in _ARITH.findall(text.replace("**", "")):
            result = _evaluate(expression)
            if result is None:
                continue
            claim = float(claimed.replace(",", ""))
            checked += 1
            tolerance = max(0.51, 0.02 * abs(claim))
            # Percent and fraction forms mix freely ("0.98 × 0.265 ≈ 26%").
            if not any(abs(candidate - claim) <= tolerance for candidate in (result, result * 100, result / 100)):
                wrong.append(f"run {n}: {expression.strip()} = {result:.4g}, written as {claimed}{_pct}")
    status = FAIL if wrong else PASS if checked else INFO
    return Check(f"{CAT}.arithmetic", CAT, title, status,
                 f"{checked} inline calculation(s) re-evaluated, {len(wrong)} wrong",
                 value={"checked": checked, "wrong": len(wrong)}, evidence=wrong[:10])


def spread(a: RunArtifacts) -> Check:
    title = "Disagreement between forecast runs"
    extra = a.forecast.get("extra") or {}
    if a.forecast.get("question_type") == "binary":
        values = [float(v) * 100 for v in a.forecast.get("run_values") or []]
        if len(values) < 2:
            return skipped(f"{CAT}.spread", CAT, title, "fewer than two runs")
        gap = max(values) - min(values)
        return Check(f"{CAT}.spread", CAT, title, WARN if gap >= 30 else INFO,
                     f"runs span {gap:.0f} points ({', '.join(f'{v:.0f}%' for v in values)}); "
                     f"tiebreaker {'used' if extra.get('tiebreaker_used') else 'not used'}",
                     value={"spread_pp": round(gap, 1)})
    return Check(f"{CAT}.spread", CAT, title, INFO, "see the per-run values in forecast.json",
                 value={"run_values": a.forecast.get("run_values")})


def posted_comment(a: RunArtifacts) -> Check:
    title = "Posted comment is short"
    comment = a.forecast.get("posted_comment")
    if not comment:
        return Check(f"{CAT}.comment", CAT, title, INFO, "no comment posted (not submitted, or older run)")
    return Check(f"{CAT}.comment", CAT, title, WARN if len(comment) > 1300 else PASS,
                 f"{len(comment):,} chars", value={"chars": len(comment)})


CHECKS = (base_rate, consistency, arithmetic, spread, posted_comment)
