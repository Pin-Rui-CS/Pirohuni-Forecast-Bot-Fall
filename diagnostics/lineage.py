"""Run lineage: the flow map of one run, built from what each step declared.

Every trace event carries a ``lineage`` block (written by ``research_trace.emit``):

    {"reads": [...], "writes": [...], "cuts": [...], "lane": ..., "pass": ...,
     "kind": ..., "input_file": ..., "duration_s": ...}

``reads``/``writes`` are plain reference strings (``url:...``, ``page:...``,
``section:...``, ``doc:...``, ``input:question``). This module knows nothing
about the pipeline's stages: an edge is "B read a reference A wrote", rows are
graph depth, lanes are the declared source context. A new stage that declares
its references shows up on the map with no change here.

Runs made before the contract existed are translated by ``lineage_legacy``
(frozen; it never needs updating for new runs).

The output dict is the published ``lineage.json``; its schema is
``diagnostics/lineage.schema.json`` and the display spec is
``diagnostics/LINEAGE.md``.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from diagnostics import lineage_legacy
from facts import claims, text_values
from diagnostics.loader import RunArtifacts, _read

SCHEMA_VERSION = 1
_PASS, _WARN, _FAIL, _UNUSED = "pass", "warn", "fail", "unused"
_STATUS = {"ok": _PASS, "included": _PASS, "experimental": _PASS,
           "failed": _FAIL, "rejected": _FAIL, "error": _FAIL,
           "excluded": _UNUSED}
_NO_LANE = {"", "unknown", None}
_MAX_FACTS = 200
# Credentials that can ride along in a logged request URL or error message.
_SECRET = re.compile(r"((?:api[_-]?key|apikey|key|token|access_token|secret|auth|signature|sig)=)[^&\s'\"<>]+", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


# --------------------------------------------------------------------------- graph
def _status(raw: str) -> str:
    return _STATUS.get(str(raw or "ok"), _WARN)


def _matches(read: str, written: str) -> bool:
    return written.startswith(read[:-1]) if read.endswith("*") else written == read


def _resolve_edges(nodes: list[dict]) -> tuple[dict, dict]:
    """(edges, gaps). An exact read links the latest earlier writer of that
    reference. A read ending in ``*`` links the earlier writers whose reference
    starts with the prefix, keeping only the newest of any that feed one another
    (a step that read "every API agent step" read the last one). An exact read
    nobody wrote is a lineage gap; an unmatched ``*`` read is just optional."""
    writers: dict[str, list[str]] = defaultdict(list)
    edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    gaps: dict[str, list[str]] = defaultdict(list)
    parents: dict[str, set] = defaultdict(set)

    def ancestors(nid: str) -> set:
        seen, stack = set(), list(parents[nid])
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(parents[cur])
        return seen

    for node in nodes:
        nid = node["id"]
        for read in node["_reads"]:
            if read.endswith("*"):
                found: dict[str, list[str]] = defaultdict(list)
                for ref, ws in writers.items():
                    if _matches(read, ref):
                        for w in ws:
                            found[w].append(ref)
                fed = set().union(*(ancestors(w) for w in found)) if found else set()
                for w, refs in found.items():
                    if w not in fed and w != nid:
                        edges[(w, nid)].extend(refs)
            elif writers.get(read):
                edges[(writers[read][-1], nid)].append(read)
            else:
                gaps[nid].append(read)
        for a, b in list(edges):
            if b == nid:
                parents[nid].add(a)
        for ref in node["_writes"]:
            writers[ref].append(nid)
    return edges, gaps


def _layers(ids: list[str], edges: dict, stage_of: dict) -> dict:
    """Row of each node: its longest path from the inputs, then pulled down to
    sit just above the first step that uses it (so parallel sections line up in
    one row before the step that merges them). Nodes ids are in write order,
    so every parent precedes its children."""
    parents, children = defaultdict(list), defaultdict(list)
    for a, b in edges:
        parents[b].append(a)
        children[a].append(b)
    layer: dict[str, int] = {}
    for nid in ids:
        layer[nid] = 1 + max((layer[p] for p in parents[nid]), default=-1)
    for nid in reversed(ids):
        if children[nid] and parents[nid]:  # inputs stay on row 0
            layer[nid] = max(layer[nid], min(layer[c] for c in children[nid]) - 1)
    # A step nothing reads (a forecast run, an unused section) lines up with
    # the other steps of its stage rather than floating up its own lane.
    deepest_of_stage: dict = defaultdict(int)
    for nid in ids:
        deepest_of_stage[stage_of[nid]] = max(deepest_of_stage[stage_of[nid]], layer[nid])
    for nid in ids:
        if parents[nid] and not children[nid]:
            layer[nid] = deepest_of_stage[stage_of[nid]]
    return layer


def _lanes(nodes: list[dict], edges: dict) -> None:
    """A declared lane wins ("*" = spans every lane). Otherwise a node takes the
    one lane its parents share, else the one lane all its children share; a
    node fed from several lanes, or feeding a spanning node, spans them ("*")."""
    by_id = {n["id"]: n for n in nodes}
    parents, children = defaultdict(list), defaultdict(list)
    for a, b in edges:
        parents[b].append(a)
        children[a].append(b)
    lane = {n["id"]: (n["_lane"] if n["_lane"] not in _NO_LANE else None) for n in nodes}
    fixed = {nid for nid, ln in lane.items() if ln}
    for _ in range(len(nodes)):
        changed = False
        for n in nodes:
            nid = n["id"]
            if nid in fixed or by_id[nid]["kind"] == "input":
                continue
            ups = {lane[p] for p in parents[nid] if by_id[p]["kind"] != "input"} - {None}
            downs = {lane[c] for c in children[nid]} - {None}
            real_ups, real_downs = ups - {"*"}, downs - {"*"}
            new = None
            if len(real_ups) > 1:
                new = "*"
            elif len(real_ups) == 1:
                new = next(iter(real_ups))
            elif len(real_downs) == 1 and "*" not in downs:
                new = next(iter(real_downs))
            elif ups or downs:
                new = "*"
            if new != lane[nid]:
                lane[nid], changed = new, True
        if not changed:
            break
    for n in nodes:
        n["lane"] = None if n["kind"] == "input" else (lane[n["id"]] or "*")


# --------------------------------------------------------------------------- calls
def _call_owner(row: dict, claimed: dict, nodes: list[dict]) -> str | None:
    """The step that made a logged call: the step that claimed it, else the
    fetch step for the page it asked for, else the first step in its lane that
    finished after it started."""
    if row.get("id") in claimed:
        return claimed[row["id"]]
    target = row.get("target")
    if target:
        key = lineage_legacy.canon(target)
        for n in nodes:
            if n["kind"] == "fetch" and lineage_legacy.canon(str(n["label"])) == key:
                return n["id"]
    lane, started = row.get("lane"), str(row.get("ts") or "")
    if lane and started:
        later = [n for n in nodes if n.get("lane") == lane and str(n.get("ts") or "") >= started]
        if later:
            return min(later, key=lambda n: str(n["ts"]))["id"]
    return None


def _calls(art: RunArtifacts, nodes: list[dict], events: list[dict]) -> list[dict]:
    by_seq = {n["seq"]: n["id"] for n in nodes if n.get("seq") is not None}
    claimed = {cid: f"n{e['seq']}" for e in events for cid in (e["lineage"].get("calls") or [])}
    calls: list[dict] = []
    for row in art.calls:  # trace/calls.jsonl, written by call_log
        label = row.get("label")
        calls.append({
            "service": row.get("service"), "kind": row.get("kind") or "http",
            "request": (f"{label} · " if label else "") + str(row.get("request") or ""),
            "status_code": row.get("status"), "started_at": row.get("ts"),
            "duration_s": row.get("duration_s"),
            "returned": f"{row['returned']:,} bytes" if isinstance(row.get("returned"), int) else row.get("returned"),
            "outcome": row.get("error") or "", "node_id": _call_owner(row, claimed, nodes),
        })
    calls += lineage_legacy.calls_from_events(events, by_seq, has_call_log=bool(art.calls))
    if not art.calls:  # older runs: model calls only exist as usage records
        for rec in art.llm_calls:
            calls.append({
                "service": rec.get("endpoint") or "model", "kind": "model",
                "request": f"{rec.get('name_of_task')} · {rec.get('model_used')}",
                "status_code": None, "started_at": rec.get("started_at"),
                "duration_s": round(float(rec.get("duration_seconds") or 0), 1),
                "returned": f"{int(rec.get('input_characters') or 0):,} → {int(rec.get('output_characters') or 0):,} chars",
                "outcome": rec.get("error") or "",
                "node_id": lineage_legacy.node_for_llm_call(rec, nodes),
            })
    for i, call in enumerate(calls, 1):
        call["id"] = f"c{i}"
        call["failed"] = bool(call["outcome"]) or (isinstance(call["status_code"], int) and call["status_code"] >= 400)
    last_failed: dict[str, str] = {}
    for call in calls:
        nid = call["node_id"]
        call["retry_of"] = last_failed.get(nid) if nid else None
        if nid:
            if call["failed"]:
                last_failed[nid] = call["id"]
            else:
                last_failed.pop(nid, None)
    return calls


# --------------------------------------------------------------------------- facts
def _facts(nodes: list[dict], edges: dict, texts: dict) -> list[dict]:
    """Every line that states a value, followed through the graph.

    A fact is identified by its set of values. Its origin is the first step
    whose output holds all of them; from there it follows the children that
    still hold them (a page it came from is shown as the first step). Where no
    child holds it, the deepest child is where it was lost: "gone" if that step
    cut text or failed, otherwise "dropped" (a step chose to leave it out)."""
    children, parents = defaultdict(list), defaultdict(list)
    for a, b in edges:
        children[a].append(b)
        parents[b].append(a)
    by_id = {n["id"]: n for n in nodes}
    order = {n["id"]: i for i, n in enumerate(nodes)}
    # JSON outputs are records about a step (a decision, a check), not the text
    # it passed on, so they cannot show whether a fact survived.
    values = {nid: text_values(t) for nid, t in texts.items()
              if not str((by_id[nid]["output"] or {}).get("file", "")).endswith(".json")}

    def holds(nid: str, key: frozenset):
        if nid not in values:
            return None  # this step's output was not saved
        return key <= values[nid]

    seen, found = set(), []
    for node in nodes:
        nid = node["id"]
        # Claims are written by model/code steps as prose; link lists (search),
        # raw pages (fetch) and JSON tool traffic are where facts come FROM.
        if (node["kind"] not in ("model", "code") or nid not in texts
                or str((node["output"] or {}).get("file", "")).endswith(".json")):
            continue
        for fact in claims(texts[nid], seen=seen, limit=_MAX_FACTS - len(found)):
            found.append((nid, fact.claim, fact.source, fact.key))
        if len(found) >= _MAX_FACTS:
            break

    out = []
    for i, (where, claim, source, key) in enumerate(found, 1):
        # walk up to the first step that already held every value
        origin = where
        while True:
            ups = [p for p in parents[origin] if by_id[p]["kind"] not in ("fetch", "input") and holds(p, key)]
            if not ups:
                break
            origin = min(ups, key=order.get)
        steps = []
        page = next((p for p in sorted(parents[origin], key=order.get)
                     if by_id[p]["kind"] == "fetch" and holds(p, key)), None)
        if page:
            steps.append({"node_id": page, "state": "present"})
        steps.append({"node_id": origin, "state": "present"})
        cur, lost_at, final = origin, None, "present"
        while children[cur]:
            kids = sorted(children[cur], key=order.get)
            known = [c for c in kids if holds(c, key) is not None]
            held = [c for c in known if holds(c, key)]
            deepest = lambda c: (by_id[c]["layer"], -order[c])  # noqa: E731
            if held or not known:
                # follow the route that carries it furthest toward the end; a step
                # whose output was not saved is followed only when nothing else is
                cur = max(held or kids, key=deepest)
                steps.append({"node_id": cur, "state": "present" if held else "unknown"})
                continue
            partial = [c for c in known if key & values[c]]
            # A step that cut text is the likeliest place a value went missing.
            cutters = [c for c in known if by_id[c]["cuts"] or by_id[c]["status"] == _FAIL]
            blame = max(cutters or known, key=deepest)
            if partial:
                cur = max(partial, key=lambda c: (len(key & values[c]), by_id[c]["layer"]))
                steps.append({"node_id": cur, "state": "modified",
                              "note": f"{len(key & values[cur])} of {len(key)} values kept"})
                final = "modified"
                continue
            child = by_id[blame]
            lost = "gone" if (child["cuts"] or child["status"] == _FAIL) else "dropped"
            steps.append({"node_id": blame, "state": lost})
            lost_at, final = blame, lost
            break
        out.append({"id": f"f{i}", "claim": claim, "source": source, "numbers": sorted(key),
                    "plan_item": False, "origin": origin, "steps": steps,
                    "lost_at": lost_at, "final_state": final})
    return out


def _plan_facts(checks, nodes: list[dict], start: int) -> list[dict]:
    """Evidence-plan items, from the Qwen plan_items check (quotes verified by code)."""
    check = next((c for c in checks or [] if c.check_id == "evidence.plan_items"), None)
    items = ((check.value or {}).get("detail") if check else None) or []
    plan = next((n["id"] for n in nodes if "doc:plan" in n["_writes"]), None)
    brief = next((n["id"] for n in reversed(nodes) if "doc:brief" in n["_writes"]), None)
    research = next((n["id"] for n in reversed(nodes) if "doc:compiler_input" in n["_writes"]), None)
    out = []
    for i, item in enumerate(items, start):
        if item.get("in_brief"):
            state, at, quote = "present", brief, item.get("quote")
        elif item.get("in_research"):
            state, at, quote = "dropped", brief, item.get("research_quote")
        else:
            state, at, quote = "never_fetched", plan, ""
        steps = [{"node_id": plan, "state": "present"}] if plan else []
        if state != "never_fetched" and research:
            steps.append({"node_id": research, "state": "present"})
        if at and at != plan:
            steps.append({"node_id": at, "state": state, "quote": quote})
        out.append({"id": f"p{i}", "claim": item.get("text", ""), "source": "", "numbers": [],
                    "plan_item": True, "origin": plan, "steps": steps,
                    "lost_at": None if state == "present" else at, "final_state": state})
    return out


# --------------------------------------------------------------------------- build
def build(art: RunArtifacts, checks=None, *, files_prefix: str | None = None) -> dict:
    events = lineage_legacy.translate(art)
    question = art.question
    nodes: list[dict] = []
    inputs = sorted({r for e in events for r in e["lineage"].get("reads", []) if r.startswith("input:")})
    for ref in inputs:
        nodes.append({"id": ref, "seq": None, "stage": "input", "label": ref.split(":", 1)[1].replace("_", " "),
                      "kind": "input", "_reads": [], "_writes": [ref], "_lane": None, "status": _PASS,
                      "status_raw": "ok", "error": "", "ts": None, "duration_s": None, "model": None,
                      "cuts": [], "note": "", "output": None, "input": None, "pass": None, "inferred": False})
    texts: dict[str, str] = {}
    for e in events:
        lin = e["lineage"]
        nid = f"n{e['seq']}"
        out_file = e.get("payload_file") or None
        nodes.append({
            "id": nid, "seq": e["seq"], "stage": e.get("stage"), "label": e.get("label"),
            "kind": lin.get("kind") or "code", "_reads": list(lin.get("reads") or []),
            "_writes": list(lin.get("writes") or []), "_lane": lin.get("lane"), "pass": lin.get("pass"),
            "status": _status(e.get("status")), "status_raw": e.get("status") or "ok",
            "error": e.get("error") or "", "ts": e.get("ts"), "duration_s": lin.get("duration_s"),
            "model": (e.get("meta") or {}).get("model") or lin.get("model"),
            "cuts": list(lin.get("cuts") or []), "note": lin.get("note") or "",
            "output": {"file": out_file, "chars": int(e.get("chars") or 0)} if out_file else None,
            "input": {"file": lin.get("input_file"), "chars": lin.get("input_chars")},
            "inferred": bool(lin.get("legacy")),
            "reads_defaulted": bool(lin.get("reads_defaulted")),
        })
        if out_file:
            texts[nid] = _read(os.path.join(art.question_dir, "trace", out_file))
    edges, gaps = _resolve_edges(nodes)
    ids = [n["id"] for n in nodes]
    layer = _layers(ids, edges, {n["id"]: n["stage"] for n in nodes})
    _lanes(nodes, edges)
    parents, children = defaultdict(list), defaultdict(list)
    for a, b in edges:
        parents[b].append(a)
        children[a].append(b)
    last_layer = max(layer.values(), default=0)
    lanes: list[str] = []
    for n in nodes:
        n["layer"] = layer[n["id"]]
        n["group"] = f"{n['lane']}|{n['layer']}|{n['stage']}"
        n["parents"], n["children"] = sorted(parents[n["id"]], key=ids.index), sorted(children[n["id"]], key=ids.index)
        n["gaps"] = gaps.get(n["id"], [])
        if n["input"] is not None:
            n["input"]["from"] = n["parents"]
            if not n["input"].get("chars"):
                n["input"]["chars"] = sum((nodes[ids.index(p)]["output"] or {}).get("chars", 0) for p in n["parents"])
        n["dead_end"] = (not n["children"] and n["kind"] not in ("input", "note")
                         and n["layer"] < last_layer and n["status"] != _UNUSED)
        if n["lane"] and n["lane"] != "*" and n["lane"] not in lanes:
            lanes.append(n["lane"])
    calls = _calls(art, nodes, events)
    for n in nodes:
        n["call_ids"] = [c["id"] for c in calls if c["node_id"] == n["id"]]
    facts = _facts(nodes, edges, texts)
    facts += _plan_facts(checks, nodes, 1)
    edge_list = []
    by_id = {n["id"]: n for n in nodes}
    for (a, b), refs in edges.items():
        src = by_id[a]
        edge_list.append({"from": a, "to": b, "refs": sorted(set(refs))[:5], "ref_count": len(set(refs)),
                          "chars": (src["output"] or {}).get("chars"),
                          "cut": bool(src["cuts"]), "inferred": src["inferred"] or by_id[b]["inferred"]})
    edge_list.sort(key=lambda e: (ids.index(e["from"]), ids.index(e["to"])))
    losses = defaultdict(lambda: defaultdict(int))
    for f in facts:
        if f["lost_at"]:
            losses[by_id[f["lost_at"]]["stage"]][f["final_state"]] += 1
        elif f["final_state"] == "modified":
            stage = next((by_id[s["node_id"]]["stage"] for s in f["steps"] if s["state"] == "modified"), "?")
            losses[stage]["modified"] += 1
    for n in nodes:
        for key in ("_reads", "_writes"):
            n[key.lstrip("_")] = n.pop(key)
        n.pop("_lane", None)
    return {
        "schema_version": SCHEMA_VERSION,
        "lineage_source": "legacy-translation" if any(n["inferred"] for n in nodes) else "declared",
        "run": {
            "question_id": art.forecast.get("question_id"), "title": art.forecast.get("title") or question.get("title"),
            "question_type": art.forecast.get("question_type"), "run_id": art.run_id,
            "run_timestamp": art.forecast.get("run_timestamp"), "timings": art.forecast.get("timings") or {},
            "files_prefix": files_prefix, "files_layout": "{files_prefix}/steps/{file}.gz",
            "layers": last_layer + 1,
        },
        "lanes": lanes,
        "nodes": nodes,
        "edges": edge_list,
        "calls": calls,
        "facts": facts,
        "losses": {stage: dict(v) for stage, v in losses.items()},
    }


def payload_files(lineage: dict) -> list[str]:
    """Every trace file a step page may open (outputs and saved inputs)."""
    files = set()
    for n in lineage["nodes"]:
        for key in ("output", "input"):
            if n.get(key) and n[key].get("file"):
                files.add(n[key]["file"])
    return sorted(files)


def redact(text: str) -> str:
    """Mask credentials in anything that leaves the machine."""
    return _BEARER.sub(r"\1***", _SECRET.sub(r"\1***", text))


def dumps(lineage: dict) -> str:
    return redact(json.dumps(lineage, ensure_ascii=False, indent=1, default=str))
