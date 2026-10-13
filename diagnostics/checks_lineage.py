"""Checks read off the run lineage (diagnostics.lineage.build).

These take the built lineage rather than the raw artifacts, so they are run
after it is built (eval_tools/diagnose_run.py), not from runner.CODE_CHECKS.
"""
from __future__ import annotations

from collections import Counter

from diagnostics.result import INFO, PASS, WARN, Check

CAT = "lineage"


def _name(node: dict) -> str:
    return f"{node['stage']} · {str(node['label'])[:90]}"


def source(lin: dict) -> Check:
    legacy = lin["lineage_source"] != "declared"
    return Check(f"{CAT}.source", CAT, "How the flow map was built", INFO,
                 "translated from an older trace (links inferred per stage)" if legacy
                 else "every step declared what it read and wrote",
                 value={"lineage_source": lin["lineage_source"], "nodes": len(lin["nodes"]),
                        "edges": len(lin["edges"]), "lanes": lin["lanes"]})


def gaps(lin: dict) -> Check:
    rows = [(n, g) for n in lin["nodes"] for g in n.get("gaps", [])]
    return Check(f"{CAT}.gaps", CAT, "Every input a step read was produced earlier", WARN if rows else PASS,
                 f"{len(rows)} read(s) with no earlier writer",
                 value={"gaps": [{"node": n["id"], "ref": g} for n, g in rows]},
                 evidence=[f"{_name(n)} read {g}, which no earlier step wrote" for n, g in rows][:30])


def undeclared(lin: dict) -> Check:
    """Steps traced without saying what they read (the trace defaulted it)."""
    rows = [n for n in lin["nodes"] if n.get("reads_defaulted")]
    if lin["lineage_source"] != "declared":
        return Check(f"{CAT}.undeclared", CAT, "Every step declared what it read", INFO,
                     "not applicable to an older run")
    return Check(f"{CAT}.undeclared", CAT, "Every step declared what it read", WARN if rows else PASS,
                 f"{len(rows)} step(s) without declared inputs (their links on the map are guesses)",
                 value={"nodes": [n["id"] for n in rows]},
                 evidence=[_name(n) for n in rows][:30])


def dead_ends(lin: dict) -> Check:
    rows = [n for n in lin["nodes"] if n.get("dead_end")]
    return Check(f"{CAT}.dead_ends", CAT, "Work that nothing used", WARN if rows else PASS,
                 f"{len(rows)} step(s) whose output no later step read",
                 value={"nodes": [n["id"] for n in rows]},
                 evidence=[f"{_name(n)} ({(n['output'] or {}).get('chars', 0):,} chars)" for n in rows][:30])


def cuts(lin: dict) -> Check:
    rows = [(n, c) for n in lin["nodes"] for c in n.get("cuts", [])]

    def line(n, c):
        of = f" of {c['of']:,}" if c.get("of") else ""
        share = f" ({c['kept'] / c['of']:.0%} kept)" if c.get("of") else ""
        return f"{_name(n)}: kept {c.get('kept', 0):,}{of} chars{share} — {c.get('reason', '')}"

    return Check(f"{CAT}.cuts", CAT, "Where text was cut or compressed", INFO,
                 f"{len(rows)} cut(s) across {len({n['id'] for n, _ in rows})} step(s)",
                 value={"cuts": [{"node": n["id"], **c} for n, c in rows]},
                 evidence=[line(n, c) for n, c in rows][:40])


def fact_losses(lin: dict) -> Check:
    facts = [f for f in lin["facts"] if not f["plan_item"]]
    if not facts:
        return Check(f"{CAT}.facts", CAT, "Where stated values were lost", INFO, "no facts found")
    states = Counter(f["final_state"] for f in facts)
    by_stage = sorted(((stage, sum(v.values()), v) for stage, v in lin["losses"].items()),
                      key=lambda r: -r[1])
    lost = states["dropped"] + states["gone"]
    return Check(f"{CAT}.facts", CAT, "Where stated values were lost", INFO,
                 f"{len(facts)} facts followed: {states['present']} kept, {states['modified']} changed, "
                 f"{states['dropped']} dropped, {states['gone']} cut or lost to a failure"
                 + (f"; most losses at {by_stage[0][0]}" if lost and by_stage else ""),
                 value={"states": dict(states), "by_stage": {s: v for s, _, v in by_stage}},
                 evidence=[f"{stage}: " + ", ".join(f"{k} {n}" for k, n in v.items()) for stage, _, v in by_stage])


def plan_coverage(lin: dict) -> Check:
    items = [f for f in lin["facts"] if f["plan_item"]]
    if not items:
        return Check(f"{CAT}.plan", CAT, "Plan items through the pipeline", INFO,
                     "not assessed (needs the Qwen plan check)")
    states = Counter(f["final_state"] for f in items)
    status = WARN if states["never_fetched"] or states["dropped"] else PASS
    return Check(f"{CAT}.plan", CAT, "Plan items through the pipeline", status,
                 f"{states['present']} reached the brief, {states['dropped']} found but dropped, "
                 f"{states['never_fetched']} never fetched",
                 value={"states": dict(states)},
                 evidence=[f"{f['final_state']}: {f['claim'][:140]}" for f in items if f["final_state"] != "present"])


CHECKS = (source, gaps, undeclared, dead_ends, cuts, fact_losses, plan_coverage)


def run(lin: dict) -> list[Check]:
    return [fn(lin) for fn in CHECKS]
