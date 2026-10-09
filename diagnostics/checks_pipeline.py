"""Did each step run, what errored, what it cost, and where calls were routed."""
from __future__ import annotations

import json
import re
from collections import Counter

from diagnostics.loader import RunArtifacts
from diagnostics.result import FAIL, INFO, PASS, WARN, Check, skipped

CAT = "pipeline"
_REQUIRED_STAGES = ("evidence_plan", "provider", "artifact_check", "compiler_input", "brief")
_BAD_STATUSES = {"failed", "fallback", "rejected", "truncated"}
_LADDER_LINE = re.compile(r"\[qwen-ladder\] (?P<label>\S[^|]*?) \| (?P<effort>\w+) \| "
                          r"(?P<outcome>\w+) \| (?P<secs>[\d.]+)s(?: \|.*\| (?P<error>.+))?")
_LOG_ERROR = re.compile(r"\b(WARNING|ERROR)\b")
_ERROR_KINDS = (
    ("rate limited (429)", re.compile(r"\b429\b|Too Many Requests|rate.?limit", re.I)),
    ("timeout", re.compile(r"Timeout|timed out", re.I)),
    ("server error (5xx)", re.compile(r"\b50[0-4]\b")),
    ("client error (4xx)", re.compile(r"\b40[0-4]\b")),
    ("provider unavailable", re.compile(r"unavailable", re.I)),
)


def stages(a: RunArtifacts) -> Check:
    title = "Every pipeline stage ran"
    if not a.trace_events:
        return skipped(f"{CAT}.stages", CAT, title, "no trace.jsonl in this run")
    counts = Counter(e.get("stage") for e in a.trace_events)
    missing = [s for s in _REQUIRED_STAGES if not counts.get(s)]
    bad = [f"{e.get('stage')} '{str(e.get('label'))[:60]}': {e.get('status')}"
           + (f" ({e.get('error')[:120]})" if e.get("error") else "")
           for e in a.trace_events if e.get("status") in _BAD_STATUSES]
    status = FAIL if missing else WARN if bad else PASS
    detail = (f"missing stages: {', '.join(missing)}. " if missing else "") + \
             (f"{len(bad)} stage event(s) failed, fell back or were cut." if bad else "all required stages present")
    return Check(f"{CAT}.stages", CAT, title, status, detail,
                 value={"stage_counts": dict(counts), "missing": missing}, evidence=bad[:15])


def search_chain(a: RunArtifacts) -> Check:
    title = "Web search provider served the research"
    events = a.events("search_chain")
    degraded = a.forecast.get("degraded_search_providers") or []
    if not events:
        status = FAIL if degraded else INFO
        return Check(f"{CAT}.search_chain", CAT, title, status,
                     "no search fallback recorded" + (f"; degraded: {degraded}" if degraded else ""),
                     value={"degraded": degraded})
    meta = events[-1].get("meta") or {}
    fell = meta.get("fell_through") or meta.get("failed") or []
    evidence = list(fell)
    # A provider "out of credits" is often really a rate limit: say which.
    for line in a.run_log_all:  # the cause is often in an earlier question's window
        if "SerpAPI" in line and ("429" in line or "Too Many Requests" in line):
            evidence.append("run.log: SerpAPI returned HTTP 429 (rate limit), which the pipeline "
                            "records as 'out of credits': " + line[:160])
            break
    status = FAIL if degraded else WARN if fell else PASS
    detail = f"served by {meta.get('chosen') or 'none'}" + (f"; fell through: {len(fell)}" if fell else "")
    return Check(f"{CAT}.search_chain", CAT, title, status, detail,
                 value={"chosen": meta.get("chosen"), "fell_through": fell, "degraded": degraded},
                 evidence=evidence)


def artifact_retry(a: RunArtifacts) -> Check:
    title = "Required artifact found (artifact check and retry)"
    checks = a.events("artifact_check")
    versions = []
    for event in checks:
        try:
            versions.append(json.loads(a.payload(event)).get("status"))
        except (ValueError, AttributeError):
            versions.append("unreadable")
    final = (a.forecast.get("artifact_check") or {}).get("status")
    decision = {}
    for event in a.events("retry_decision"):
        try:
            decision = json.loads(a.payload(event))
        except ValueError:
            pass
    status = PASS if final == "complete" else WARN if final == "partial" else FAIL if final == "missing" else INFO
    detail = (f"status by version: {' -> '.join(v or '?' for v in versions) or 'none'}; final {final}; "
              f"retry {'ran' if decision.get('ran') else 'did not run'}"
              + (f" ({decision.get('reason')})" if decision.get("reason") else "")
              + (", result included" if decision.get("included") else ""))
    missing = (a.forecast.get("artifact_check") or {}).get("what_is_missing") or ""
    return Check(f"{CAT}.artifact_retry", CAT, title, status, detail,
                 value={"versions": versions, "final": final, "retry": decision},
                 evidence=[f"still missing: {missing[:400]}"] if missing else [])


def ensemble(a: RunArtifacts) -> Check:
    title = "All forecast runs completed"
    members = (a.forecast.get("extra") or {}).get("ensemble") or []
    if not members:
        return skipped(f"{CAT}.ensemble", CAT, title, "no ensemble record")
    dropped = [m for m in members if m.get("dropped") or not m.get("valid", True)]
    evidence = []
    run_texts = dict(a.forecast_runs)
    for i, member in enumerate(members, 1):
        if member in dropped:
            reason = member.get("error") or ""
            if not reason and i in run_texts:
                reason = re.sub(r"\s+", " ", run_texts[i])[:300]
            evidence.append(f"run {i} ({member.get('model')}) dropped: {reason or 'reason not recorded'}")
    status = FAIL if len(dropped) == len(members) else WARN if dropped else PASS
    return Check(f"{CAT}.ensemble", CAT, title, status,
                 f"{len(members) - len(dropped)}/{len(members)} runs used",
                 value={"members": members, "dropped": len(dropped)}, evidence=evidence)


def llm_call_errors(a: RunArtifacts) -> Check:
    title = "LLM calls without errors"
    failed = [c for c in a.llm_calls if c.get("error")]
    status = WARN if failed else PASS
    return Check(f"{CAT}.llm_errors", CAT, title, status,
                 f"{len(failed)} of {len(a.llm_calls)} calls recorded an error",
                 value={"failed": [c.get("name_of_task") for c in failed]},
                 evidence=[f"{c.get('name_of_task')}: {str(c.get('error'))[:200]}" for c in failed])


def qwen_attempts(a: RunArtifacts) -> Check:
    title = "Qwen ladder attempts (failed or timed-out rungs)"
    if a.qwen_attempts:
        attempts, source = a.qwen_attempts, "trace/qwen_attempts.jsonl"
    elif a.run_log_lines:
        own = {str(c.get("name_of_task") or "").split(" ")[0] for c in a.llm_calls}
        attempts, source = [], "run.log (approximate: concurrent questions share labels)"
        for line in a.run_log_lines:
            match = _LADDER_LINE.search(line)
            if match and match.group("label").strip() in own:
                attempts.append({"label": match.group("label").strip(), "effort": match.group("effort"),
                                 "outcome": match.group("outcome"), "seconds": float(match.group("secs")),
                                 "error": match.group("error") or ""})
    else:
        return skipped(f"{CAT}.qwen_attempts", CAT, title, "no attempt log and no run.log")
    bad = [x for x in attempts if x.get("outcome") != "ok"]
    wasted = sum(float(x.get("seconds") or 0) for x in bad)
    status = WARN if bad else PASS
    return Check(f"{CAT}.qwen_attempts", CAT, title, status,
                 f"{len(bad)} of {len(attempts)} attempts failed or timed out ({wasted:.0f}s spent on them); "
                 f"source: {source}",
                 value={"attempts": len(attempts), "bad": len(bad), "wasted_seconds": round(wasted, 1),
                        "by_label": dict(Counter(x["label"] for x in bad))},
                 evidence=[f"{x['label']} @ {x['effort']}: {x['outcome']} after {x.get('seconds', 0):.0f}s "
                           f"{str(x.get('error') or '')[:120]}" for x in bad[:12]])


def log_errors(a: RunArtifacts) -> Check:
    title = "API errors in the run log"
    if not a.run_log_lines:
        return skipped(f"{CAT}.log_errors", CAT, title, "no run.log")
    kinds: Counter = Counter()
    examples: dict[str, str] = {}
    for line in a.run_log_lines:
        if not _LOG_ERROR.search(line[:60]):
            continue
        for kind, pattern in _ERROR_KINDS:
            if pattern.search(line):
                kinds[kind] += 1
                examples.setdefault(kind, line[:220])
                break
    status = WARN if kinds else PASS
    return Check(f"{CAT}.log_errors", CAT, title, status,
                 (", ".join(f"{k}: {n}" for k, n in kinds.most_common()) or "none") +
                 " (run.log window; may include a concurrent question)",
                 value=dict(kinds), evidence=list(examples.values()))


def cost_time(a: RunArtifacts) -> Check:
    title = "Cost and time"
    calls = a.llm_calls
    paid = sum(float(c.get("cost_usd") or 0) for c in calls)
    quota = sum(float(c.get("quota_microdollars") or 0) for c in calls)
    timings = a.forecast.get("timings") or {}
    slowest = sorted(calls, key=lambda c: -float(c.get("duration_seconds") or 0))[:5]
    by_stage: Counter = Counter()
    for c in calls:
        label = str(c.get("name_of_task") or "")
        by_stage[label.split("[")[0].split("/")[0] or "?"] += float(c.get("cost_usd") or 0)
    return Check(f"{CAT}.cost_time", CAT, title, INFO,
                 f"${paid:.3f} paid, {quota:,.0f} quota µ$; research {timings.get('research_seconds', '?')}s, "
                 f"forecast {timings.get('forecast_seconds', '?')}s",
                 value={"paid_usd": round(paid, 4), "quota_microdollars": round(quota, 1),
                        "timings": timings, "paid_by_stage": {k: round(v, 4) for k, v in by_stage.items() if v}},
                 evidence=[f"{c.get('name_of_task')}: {float(c.get('duration_seconds') or 0):.0f}s, "
                           f"${float(c.get('cost_usd') or 0):.3f}" for c in slowest])


def routing_leak(a: RunArtifacts) -> Check:
    """Paid calls on research labels the Qwen setup is meant to run for free."""
    title = "Research calls stayed on Qwen (no paid leak)"
    try:
        import llm_provider
        qwen_labels = llm_provider.QWEN_RECOMMENDED_RESEARCH_LABELS
    except Exception:  # noqa: BLE001
        return skipped(f"{CAT}.routing_leak", CAT, title, "llm_provider not importable")
    calls = a.llm_calls
    qwen_mode = any(c.get("cost_source") == "quota" for c in calls)
    if not qwen_mode:
        return Check(f"{CAT}.routing_leak", CAT, title, INFO, "run used the paid research setting")

    def research(label: str) -> bool:
        return any(label == p or (p.endswith("/") and label.startswith(p)) for p in qwen_labels)

    leaks = [c for c in calls if c.get("cost_source") != "quota" and research(str(c.get("name_of_task") or ""))]
    return Check(f"{CAT}.routing_leak", CAT, title, WARN if leaks else PASS,
                 f"{len(leaks)} research call(s) billed on a paid model",
                 value={"leaks": [c.get("name_of_task") for c in leaks]},
                 evidence=[f"{c.get('name_of_task')} -> {c.get('model_used')} ${float(c.get('cost_usd') or 0):.3f}"
                           for c in leaks])


CHECKS = (stages, search_chain, artifact_retry, ensemble, llm_call_errors, qwen_attempts,
          log_errors, cost_time, routing_leak)
