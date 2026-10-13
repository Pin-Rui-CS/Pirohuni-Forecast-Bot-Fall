"""Translate trace events written before the lineage contract into it.

Since lineage contract v1, ``research_trace.emit`` records what each step read
and wrote, and ``diagnostics.lineage`` builds the map from that alone. Older
runs only recorded outputs, so this module states, once, what each of their
stages read and wrote. It is the only place that knows stage names, it only
ever applies to events without a ``lineage`` block, and it is frozen: a new
or changed stage declares its own lineage and never needs an entry here.
Every node it produces is marked ``inferred``.
"""
from __future__ import annotations

import json
import os
import re

from diagnostics.loader import RunArtifacts, _read

_URL = re.compile(r"https?://[^\s\)\]\}'\"<>`|]+", re.I)
_PART = re.compile(r"\s*\(part \d+/\d+\)\s*$")
_SCRAPE_CAP = 40_000


def canon(url: str) -> str:
    """Same canonical form as research_trace.ref_url."""
    cleaned = str(url or "").strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = cleaned.removeprefix("www.").split("#", 1)[0]
    return cleaned.rstrip("/").rstrip(".,;:!?)")


def _urls(text: str) -> list[str]:
    return sorted({canon(u) for u in _URL.findall(text or "")})


def _tool_phase(event: dict) -> tuple[str | None, str | None]:
    phase = str((event.get("meta") or {}).get("phase") or "")
    if phase == "resolution":
        return "Resolution Criteria Sources", "main pass"
    tool, _, ps = phase.partition(" / ")
    return (tool or None), (ps or None)


def _payload(art: RunArtifacts, event: dict) -> str:
    name = event.get("payload_file") or ""
    return _read(os.path.join(art.question_dir, "trace", name)) if name else ""


def translate(art: RunArtifacts) -> list[dict]:
    """Events (copies) each with a ``lineage`` block; prompt-only events are
    folded into the step that used them; forecast runs are appended when the
    run did not trace them."""
    out: list[dict] = []
    in_retry, done_retry = False, False
    last_search: str | None = None
    pending_prompt: dict | None = None
    included: list[str] = []
    retry_section: str | None = None
    chains: dict[str, str] = {}
    chosen_provider: str | None = None
    brief_versions = 0
    compressed: set[str] = set()

    for raw in art.trace_events:
        e = dict(raw)
        meta = e.get("meta") or {}
        if "lineage" in e:  # declared by the bot: never second-guess it
            out.append(e)
            continue
        stage, label, seq = e.get("stage"), str(e.get("label") or ""), e.get("seq")
        tool, phase = _tool_phase(e)
        pas = "artifact retry" if in_retry else (phase or "main pass")
        lin: dict = {"legacy": True, "lane": tool, "pass": pas}

        if stage == "scrape":
            url = canon(label)
            if meta.get("phase") == "resolution":
                lin.update(reads=["input:question"], writes=[f"page:{url}", f"resolution-page:{url}"])
            else:
                lin.update(reads=[f"url:{url}"], writes=[f"page:{url}"])
            lin["kind"] = "fetch"
            raw_chars = int(meta.get("raw_chars") or 0)
            if meta.get("truncated") or int(e.get("chars") or 0) >= _SCRAPE_CAP:
                lin["cuts"] = [{"kept": int(e.get("chars") or 0), "of": raw_chars or None,
                                "reason": "scrape cap (40,000 chars)"}]
        elif stage == "apiagent":
            step = int(meta.get("step") or 0)
            try:
                e["_payload_json"] = json.loads(_payload(art, e))
            except ValueError:
                pass
            lin.update(lane="API Agent", kind="model",
                       reads=["input:question"] if step == 0 else [f"doc:apiagent:{step - 1}"],
                       writes=[f"doc:apiagent:{step}"])
        elif stage == "asknews_filter":
            lin.update(lane="AskNews", kind="model", reads=["input:question"], writes=["doc:asknews_filtered"])
        elif stage == "evidence_plan":
            lin.update(lane="*", kind="model", reads=["input:question"], writes=["doc:plan"])
        elif stage == "search_results":
            last_search = f"doc:search:{seq}"
            lin.update(kind="search", reads=["doc:artifact_check:v1"] if in_retry else ["doc:plan"],
                       writes=[last_search] + [f"url:{u}" for u in _urls(_payload(art, e))])
        elif stage == "rank" and "prompt" in label.lower():
            pending_prompt = e  # folded into the ranked-groups step as its input
            continue
        elif stage == "rank":
            lin.update(kind="model", reads=[last_search] if last_search else [],
                       writes=[f"url:{u}" for u in _urls(_payload(art, e))])
            if pending_prompt:
                lin["input_file"] = pending_prompt.get("payload_file")
                lin["input_chars"] = pending_prompt.get("chars")
                e["meta"] = {**meta, **(pending_prompt.get("meta") or {})}
                pending_prompt = None
        elif stage == "extract_report":
            chain = str(meta.get("chain") or "extract")
            _, ctool, cphase = (chain.split(":") + ["", ""])[:3]
            ref = f"doc:extract:{ctool}:{cphase}:{meta.get('cycle')}"
            reads = [f"page:{canon(u)}" for u in meta.get("scraped_urls") or []]
            if chain in chains:
                reads.append(chains[chain])
            chains[chain] = ref
            lin.update(kind="model", lane=ctool or tool, reads=reads, writes=[ref],
                       **{"pass": cphase or pas})
        elif stage == "search_chain":
            chosen_provider = meta.get("chosen")
            lin.update(kind="note", reads=["input:question"], writes=["doc:search_chain"], lane=chosen_provider)
        elif stage == "provider":
            reads = {"AskNews": ["doc:asknews_filtered"], "Public Data APIs": ["doc:apiagent:*"],
                     "Resolution Criteria Sources": ["resolution-page:*"]}.get(label)
            if reads is None:
                prefix = f"doc:extract:{label}:main pass:"
                cycles = [r for x in out for r in x["lineage"].get("writes", []) if r.startswith(prefix)]
                if cycles:  # each cycle report is a delta; the section joins them all
                    reads = cycles
                else:  # market providers fetch on their own; nothing upstream was traced
                    reads = ["input:question"]
                    lin["lane"] = label
            if label == chosen_provider:
                reads = reads + ["doc:search_chain"]
            lin.update(kind="code", reads=reads, writes=[f"section:{label}"])
            if e.get("status") == "included":
                included.append(f"section:{label}")
        elif stage == "artifact_check":
            version = int(meta.get("version") or 1)
            if version == 1:
                lin.update(reads=["doc:plan"] + included, writes=["doc:artifact_check:v1"])
                in_retry = True
            else:
                lin.update(reads=["doc:artifact_check:v1"] + ([retry_section] if retry_section else []),
                           writes=["doc:artifact_check:v2"])
                in_retry, done_retry = False, True
            lin.update(kind="model", lane="*", **{"pass": "artifact check"})
        elif stage == "retry_decision":
            payload = _payload(art, e)
            try:
                provider = json.loads(payload).get("retry_provider") or ""
            except ValueError:
                provider = ""
            retry_section = f"section:Focused Artifact Retry ({provider})" if provider else "section:Focused Artifact Retry"
            retry_cycles = [r for x in out for r in x["lineage"].get("writes", [])
                            if r.startswith(f"doc:extract:{provider}:artifact retry:")]
            lin.update(kind="code", lane=provider or None, reads=retry_cycles,
                       writes=[retry_section], **{"pass": "artifact retry"})
            included.append(retry_section)
        elif stage == "precompress":
            section = _PART.sub("", label)
            compressed.add(section)
            lin.update(kind="code" if e.get("status") == "truncated" else "model",
                       reads=[f"section:{section}"], writes=[f"doc:precompress:{label}"], lane=None,
                       **{"pass": "fit"})
            of = int(meta.get("input_chars") or 0)
            if e.get("status") == "truncated":
                lin["cuts"] = [{"kept": int(meta.get("target_chars") or 0), "of": of,
                                "reason": "visible truncation to fit the budget"}]
            elif e.get("status") not in ("failed",):
                lin["cuts"] = [{"kept": int(e.get("chars") or 0), "of": of, "reason": "compressed by a model"}]
        elif stage == "compiler_input":
            reads, cuts = [], []
            for sec in meta.get("sections") or []:
                name = str(sec.get("name") or "")
                if name == "Evidence Plan":
                    reads.append("doc:plan")
                    continue
                reads.append(f"doc:precompress:{name}*" if name in compressed else f"section:{name}")
                before, after = int(sec.get("chars_before_fit") or 0), int(sec.get("chars") or 0)
                if before > after:
                    cuts.append({"ref": f"section:{name}", "kept": after, "of": before, "reason": "fit to budget"})
            lin.update(kind="code", lane="*", reads=reads, writes=["doc:compiler_input"], cuts=cuts,
                       **{"pass": "fit"})
        elif stage == "brief":
            brief_versions += 1
            reads = ["doc:compiler_input"] if brief_versions == 1 else [f"doc:brief:{brief_versions - 1}"]
            if brief_versions > 1 and done_retry:
                reads.append("doc:artifact_check:v2")
            lin.update(kind="model" if brief_versions == 1 else "code", lane="*",
                       reads=reads, writes=[f"doc:brief:{brief_versions}", "doc:brief"], **{"pass": "brief"})
            if e.get("status") == "truncated":
                lin["cuts"] = [{"kept": int(e.get("chars") or 0), "of": None, "reason": "output token cap"}]
        elif stage == "raw_view":
            uncapped, chars = int(meta.get("uncapped_chars") or 0), int(meta.get("chars") or e.get("chars") or 0)
            lin.update(kind="code", lane="*", reads=list(included), writes=["doc:raw_view"], **{"pass": "brief"})
            if uncapped > chars:
                lin["cuts"] = [{"kept": chars, "of": uncapped, "reason": "raw view cap"}]
        else:  # an unknown legacy stage: keep it visible, unlinked
            lin.update(kind="code", reads=[], writes=[f"doc:{stage}:{seq}"])
        e["lineage"] = lin
        out.append(e)

    if not any(e.get("stage") == "forecast" for e in out):
        out += _forecast_events(art, out, included)
    return out


def _forecast_events(art: RunArtifacts, events: list[dict], included: list[str]) -> list[dict]:
    """One node per ensemble run, from forecast.json (runs were not traced)."""
    ensemble = (art.forecast.get("extra") or {}).get("ensemble") or []
    if not ensemble:
        return []
    has_raw_view = any(e.get("stage") == "raw_view" for e in events)
    seq = max((int(e.get("seq") or 0) for e in events), default=0)
    # The raw-research member gets a different prompt: tell it apart by the size
    # of its forecast call's input (the brief runs all share one prompt).
    forecast_calls = [c for c in art.llm_calls if "-forecast[" in str(c.get("name_of_task") or "")
                      and "/repair" not in str(c.get("name_of_task") or "")]
    sizes = [int(c.get("input_characters") or 0) for c in forecast_calls]
    differs = (len(sizes) == len(ensemble) > 1
               and abs(sizes[-1] - sizes[0]) > 0.2 * max(sizes[0], 1))
    out = []
    for i, run in enumerate(ensemble, 1):
        model = str(run.get("model") or "")
        raw = differs and i == len(ensemble)
        call = forecast_calls[i - 1] if len(forecast_calls) == len(ensemble) else {}
        reads = (["doc:raw_view"] if has_raw_view else list(included)) if raw else ["doc:brief"]
        out.append({
            "seq": seq + i, "ts": None, "stage": "forecast",
            "label": f"Run {i} · {model.split('/')[-1]}" + (" (raw research)" if raw else ""),
            "status": "ok" if run.get("valid", True) else "failed",
            "error": run.get("error") or call.get("error") or "",
            "chars": 0, "payload_file": None, "meta": {"model": model},
            "lineage": {"legacy": True, "kind": "model", "lane": "*", "pass": "forecast",
                        "reads": reads, "writes": [f"doc:forecast:{i}"],
                        "note": "repaired once" if run.get("repaired") else ""},
        })
    return out


def calls_from_events(events: list[dict], by_seq: dict, *, has_call_log: bool) -> list[dict]:
    """External calls recoverable from the trace when no call log was written:
    every fetch, and every data-API call the API agent made."""
    calls: list[dict] = []
    for e in events:
        lin = e.get("lineage") or {}
        node = by_seq.get(e.get("seq"))
        if lin.get("kind") == "fetch" and not has_call_log:
            meta = e.get("meta") or {}
            calls.append({"service": meta.get("engine") or "scrape", "kind": "scrape", "request": e.get("label"),
                          "status_code": None, "started_at": e.get("ts"), "duration_s": None,
                          "returned": f"{int(e.get('chars') or 0):,} chars",
                          "outcome": e.get("error") or ("" if e.get("status") == "ok" else e.get("status")),
                          "node_id": node})
        if e.get("stage") == "apiagent" and lin.get("legacy"):  # new runs log these in calls.jsonl
            calls += _apiagent_calls(e, node)
    return calls


def _apiagent_calls(event: dict, node: str | None) -> list[dict]:
    # The step payload is {"calls": [...], "outputs": {id: output}, ...}; read
    # from the event's in-memory copy when present, else nothing to add.
    payload = event.get("_payload_json")
    if not isinstance(payload, dict):
        return []
    out = []
    for call in payload.get("calls") or []:
        if call.get("name") != "call_api":
            continue
        args = call.get("args") or call.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        result = (payload.get("outputs") or {}).get(call.get("id")) or {}
        error = str(result.get("error") or "")
        code = result.get("httpStatus")
        if code is None:
            match = re.search(r"\b([45]\d\d)\b", error)
            code = int(match.group(1)) if match else (None if error else 200)
        out.append({"service": result.get("api") or args.get("api") or "api", "kind": "data",
                    "request": json.dumps(args.get("params") or {k: v for k, v in args.items() if k != "api"},
                                          ensure_ascii=False)[:300],
                    "status_code": code, "started_at": event.get("ts"),
                    "duration_s": round(result["ms"] / 1000, 2) if isinstance(result.get("ms"), (int, float)) else None,
                    "returned": f"{result.get('rowCount')} rows" if "rowCount" in result else "",
                    "outcome": error[:200], "node_id": node})
    return out


def node_for_llm_call(record: dict, nodes: list[dict]) -> str | None:
    """Legacy only: tie a forecast model call to its run node by model tag."""
    task = str(record.get("name_of_task") or "")
    match = re.search(r"-forecast\[([^\]]+)\]", task)
    if not match:
        return None
    tag = match.group(1)
    for n in nodes:
        if n.get("stage") == "forecast" and f"· {tag}" in str(n.get("label")):
            return n["id"]
    return None
