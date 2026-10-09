"""Run every check over one run and render the result."""
from __future__ import annotations

import traceback

from diagnostics import (checks_condensation, checks_evidence, checks_forecast, checks_pipeline,
                         checks_sources)
from diagnostics.loader import RunArtifacts
from diagnostics.result import FAIL, INFO, PASS, SKIPPED, WARN, Check, skipped, sort_key

CODE_CHECKS = (checks_pipeline.CHECKS + checks_sources.CHECKS + checks_condensation.CHECKS
               + checks_evidence.CHECKS + checks_forecast.CHECKS)
_ICON = {PASS: "✅", WARN: "⚠️", FAIL: "❌", INFO: "ℹ️", SKIPPED: "⏭️"}


def _crash(fn, exc: Exception, method: str) -> Check:
    name = getattr(fn, "__name__", "check")
    detail = f"check crashed: {type(exc).__name__}: {exc}"
    return skipped(f"error.{name}", "error", name, detail + " | " + traceback.format_exc(limit=1)[-300:], method)


async def run_all(a: RunArtifacts, *, use_qwen: bool = True) -> list[Check]:
    results: list[Check] = []
    for fn in CODE_CHECKS:
        try:
            results.append(fn(a))
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
            results.append(_crash(fn, exc, "code"))
    if use_qwen:
        from diagnostics import qwen_tasks
        if not qwen_tasks.qwen_available():
            results.append(skipped("qwen.unavailable", "qwen", "Qwen checks",
                                   "SoCLaaS key not set; Qwen checks skipped", "qwen"))
        else:
            for task in qwen_tasks.TASKS:
                try:
                    results.append(await task(a))
                except Exception as exc:  # noqa: BLE001
                    results.append(_crash(task, exc, "qwen"))
    return sorted(results, key=sort_key)


def render_markdown(a: RunArtifacts, results: list[Check]) -> str:
    counts = {s: sum(1 for r in results if r.status == s) for s in (FAIL, WARN, PASS, INFO, SKIPPED)}
    lines = [
        f"# Diagnostics — {a.forecast.get('title') or a.question.get('title', '')}",
        f"Question {a.forecast.get('question_id')} · run `{a.run_id}` · {a.forecast.get('run_timestamp', '')}",
        "",
        " · ".join(f"{_ICON[s]} {s} {n}" for s, n in counts.items() if n),
        "",
        "| | check | result | method |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {_ICON.get(r.status, '')} | {r.title} | {r.detail.replace('|', '/')} | {r.method} |")
    lines.append("")
    for r in results:
        if r.evidence and r.status in (FAIL, WARN):
            lines += [f"### {_ICON[r.status]} {r.title}", ""]
            lines += [f"- {e}" for e in r.evidence]
            lines.append("")
    return "\n".join(lines)
