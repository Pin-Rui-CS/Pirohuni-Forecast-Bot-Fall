"""Brief writer loss study: across every stored forecast, what the brief writer
did with each fact it was given.

    # 1. code only, every forecast in the library (downloads are cached)
    python eval_tools/brief_loss_study.py measure [--limit N] [--question-id ID ...]

    # 2. Qwen (SoCLaaS only) labels a stratified sample: which facts matter
    python eval_tools/brief_loss_study.py classify [--concurrency 3] [--limit-runs N]

    # 3. tables for the report
    python eval_tools/brief_loss_study.py report

Output in --out (default data/brief-loss-study): facts.jsonl, runs.jsonl,
labels.jsonl, plan.jsonl, stability.jsonl, report.md. `classify` is resumable:
labels already written are skipped.

The unit is a fact: a line of the brief writer's input (the byte-exact
`compiler_input` trace payload) that states a value (facts.py). Its
state is read off the brief the writer returned, before the code adds banners:
kept, changed (some values kept), dropped with a reason (values only in the
Balance Check's "items EXCLUDED" passage) or dropped silently.

Never forecasts and never calls a paid model: paid keys are blanked before any
bot import, and the Qwen task asserts a SoCLaaS route per call.
"""
from __future__ import annotations

import os

for _key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_key] = ""
for _key in ("TIER1_MODEL", "TIER2_MODEL", "QWEN_RESEARCH_LABELS"):
    os.environ.pop(_key, None)
os.environ.setdefault("QWEN_LADDER", "1")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import collections  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from facts import (  # noqa: E402
    CHANGED, DROPPED_SILENTLY, DROPPED_WITH_REASON, KEPT, brief_state, claims, split_excluded, text_values,
)
from diagnostics.loader import load, long_path  # noqa: E402
from eval_tools.diagnose_run import fetch  # noqa: E402  (also loads .env)
from eval_tools.supabase_rest import SupabaseREST  # noqa: E402

STATES = (KEPT, CHANGED, DROPPED_WITH_REASON, DROPPED_SILENTLY)
MAX_VALUES = 20  # more values than this on one line = a squashed table, reported apart
_SECTION = re.compile(r"^===== SECTION: (.+?) \(([\d,]+) chars\) =====$", re.M)
# A search-result entry ("[12] Title: ... | Score: 0.71" or "[3] Name — https://...")
# rather than a claim an extract step wrote.
_LISTING = re.compile(r"(Score:\s*\d|^\s*\[\d+\]\s*(Title:|[^\]]{0,200}\s—\s*https?://))", re.I)
_COMPRESSED = ("[Experimental Qwen condensation", "[Section condensed from", "[COMPILER INPUT BUDGET TRUNCATION")
_OFFICIAL = re.compile(
    r"(\.gov(\.[a-z]{2})?$|\.gob\.[a-z]{2}$|\.gouv\.[a-z]{2}$|\.gc\.ca$|\.int$|\.mil$|europa\.eu$|\.jus\.br$|"
    r"\.leg\.br$|ecb\.europa|bankofengland|boj\.or\.jp|federalreserve|stlouisfed|bls\.gov|census\.gov|"
    r"ons\.gov\.uk|destatis|insee\.fr|ine\.es|statcan|abs\.gov\.au|imf\.org|worldbank\.org|oecd\.org|un\.org)"
)
_MARKETS = ("polymarket.com", "kalshi.com", "manifold.markets", "metaculus.com", "predictit.org")
_WIKI = ("wikipedia.org", "wikidata.org", "fandom.com")
_NEWS = (
    "reuters.com", "apnews.com", "bloomberg.com", "nytimes.com", "wsj.com", "ft.com", "cnn.com", "bbc.co",
    "bbc.com", "theguardian.com", "cnbc.com", "foxnews.com", "washingtonpost.com", "politico", "axios.com",
    "thehill.com", "aljazeera.com", "npr.org", "nbcnews.com", "cbsnews.com", "abcnews", "usatoday.com",
    "time.com", "economist.com", "forbes.com", "businessinsider.com", "yahoo.com", "news", "globo.com",
    "folha.uol.com.br", "estadao", "lemonde.fr", "spiegel.de", "elpais.com", "scmp.com", "nikkei",
    "oregonlive.com", "tribune", "times", "post", "herald", "journal", "gazette",
)
_DATA = ("tradingeconomics.com", "fred.", "data.", "api.", "statista.com", "ourworldindata.org", "macrotrends.net")


def section_kind(name: str) -> str:
    """Coarse group of a compiler-input section, by its provider name. Analysis
    labels only; the raw name is kept on every row."""
    low = name.lower()
    if low.startswith("evidence plan"):
        return "plan"
    if "focused artifact retry" in low:
        return "retry"
    if "resolution" in low:
        return "resolution_source"
    if "asknews" in low:
        return "asknews"
    if "public data apis" in low:
        return "api_agent"
    if any(m in low for m in ("kalshi", "manifold", "polymarket", "metaculus")):
        return "markets"
    if any(m in low for m in ("tavily", "serpapi", "firecrawl", "search")):
        return "web_search"
    return "other"


def source_type(host: str) -> str:
    host = (host or "").lower()
    if not host:
        return "unknown"
    if any(host.endswith(m) or m in host for m in _MARKETS):
        return "market"
    if any(m in host for m in _WIKI):
        return "wiki"
    if _OFFICIAL.search(host):
        return "official"
    if any(m in host for m in _DATA):
        return "data"
    if any(m in host for m in _NEWS):
        return "news"
    return "other"


def value_type(raw: str, key: frozenset) -> str:
    if any(v.endswith("%") for v in key):
        return "percent"
    if re.search(r"[$€£¥]\s?\d|\d\s?(?:USD|EUR|GBP|billion|million|trillion|bn|tn)\b", raw, re.I):
        return "money"
    if any("." in v for v in key):
        return "decimal"
    return "integer"


def parse_sections(payload: str) -> list[tuple[str, str]]:
    marks = list(_SECTION.finditer(payload or ""))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(payload)
        out.append((m.group(1), payload[m.end():end].strip("\n")))
    return out


def _brief_payload(art) -> str:
    events = art.events("brief")
    pre = [e for e in events if "pre-banner" in str(e.get("label"))]
    pick = (pre or [e for e in events if e.get("status") == "ok"] or events)
    return art.payload(pick[-1]) if pick else ""


def _cached_dir(out_root: str, run_id: str, qid: int) -> str:
    return os.path.join(out_root, run_id, f"q{qid}")


# --------------------------------------------------------------------------- measure
def measure_run(art, meta: dict) -> tuple[list[dict], dict] | None:
    compiler = art.events("compiler_input")
    brief = _brief_payload(art)
    if not compiler or not brief:
        return None
    sections = parse_sections(art.payload(compiler[-1]))
    body, excluded = split_excluded(brief)
    body_values, excluded_values = text_values(body), text_values(excluded)

    # Forecast transcripts (runs made since the lineage contract): did any run
    # still see a fact the brief dropped? The raw-research run reads the
    # sections directly, so it is the one that can recover a drop.
    forecasts = [e for e in art.events("forecast") if e.get("payload_file")]
    raw_values = [text_values(art.payload(e)) for e in forecasts if "raw research" in str(e.get("label"))]
    brief_run_values = [text_values(art.payload(e)) for e in forecasts if "raw research" not in str(e.get("label"))]

    seen: set = set()
    ordered = [s for s in sections if section_kind(s[0]) != "plan"] + [s for s in sections if section_kind(s[0]) == "plan"]
    rows = []
    for name, content in ordered:
        n_lines = max(1, content.count("\n") + 1)
        compressed = any(marker in content for marker in _COMPRESSED)
        for fact in claims(content, seen=seen):
            state = brief_state(fact.key, body_values, excluded_values, source=fact.source, excluded_text=excluded)
            row = {
                "run_id": meta["run_id"], "question_id": meta["question_id"],
                "fact_id": hashlib.sha1(f"{meta['run_id']}|{meta['question_id']}|{sorted(fact.key)}".encode()).hexdigest()[:12],
                "claim": fact.claim, "values": sorted(fact.key), "state": state,
                "section": name, "section_kind": section_kind(name), "section_compressed": compressed,
                "source": fact.source, "source_type": source_type(fact.source),
                "value_type": value_type(fact.raw, fact.key),
                "position": ("first", "middle", "last")[min(2, 3 * fact.line_no // n_lines)],
                "table_row": fact.raw.lstrip().startswith("|"),
                "search_listing": bool(_LISTING.search(fact.raw)),
                "kept_share": round(len(fact.key & body_values) / len(fact.key), 2),
                "n_values": len(fact.key),
            }
            if state in (DROPPED_SILENTLY, DROPPED_WITH_REASON) and forecasts:
                row["raw_run_has"] = any(fact.key <= v for v in raw_values) if raw_values else None
                row["brief_runs_have"] = sum(1 for v in brief_run_values if fact.key <= v)
            rows.append(row)
    states = collections.Counter(r["state"] for r in rows if r["section_kind"] != "plan")
    run = {
        **meta,
        "input_chars": sum(len(c) for _, c in sections), "sections": len(sections),
        "section_names": [n for n, _ in sections],
        "compressed_sections": sum(1 for _, c in sections if any(m in c for m in _COMPRESSED)),
        "brief_chars": len(brief), "evidence_items": len(set(re.findall(r"\[E(\d+)\]", brief))),
        "has_excluded_passage": bool(excluded), "forecast_transcripts": len(forecasts),
        "facts": sum(states.values()), **{f"n_{s}": states.get(s, 0) for s in STATES},
    }
    return rows, run


def cmd_measure(args) -> int:
    client = SupabaseREST.from_env()
    if client is None:
        raise SystemExit("needs SUPABASE_URL and SUPABASE_SERVICE_KEY")
    params = {"select": "run_id,question_id,title,question_type,run_at,blob_prefix", "order": "run_at.asc", "limit": "1000"}
    forecasts = client.select("forecasts", params)
    if args.question_id:
        forecasts = [f for f in forecasts if f["question_id"] in set(args.question_id)]
    if args.limit:
        forecasts = forecasts[: args.limit]
    os.makedirs(args.out, exist_ok=True)
    all_rows, runs, skipped = [], [], []
    for i, f in enumerate(forecasts, 1):
        qdir = _cached_dir(args.cache, f["run_id"], f["question_id"])
        if not os.path.exists(long_path(os.path.join(qdir, "trace", "trace.jsonl"))):
            qdir, _, _ = fetch(client, f["question_id"], f["run_id"], args.cache)
        art = load(qdir)
        meta = {"run_id": f["run_id"], "question_id": f["question_id"], "title": f["title"],
                "question_type": f["question_type"], "month": str(f["run_at"])[:7], "cache_dir": qdir}
        result = measure_run(art, meta)
        if result is None:
            skipped.append(f"{f['question_id']} {f['run_id']}")
            continue
        rows, run = result
        all_rows += rows
        runs.append(run)
        print(f"[{i}/{len(forecasts)}] {f['question_id']}: {run['facts']} facts, "
              f"kept {run['n_kept']}, changed {run['n_changed']}, dropped {run['n_dropped_silently']} silently "
              f"+ {run['n_dropped_with_reason']} with a reason", flush=True)
    with open(os.path.join(args.out, "facts.jsonl"), "w", encoding="utf-8") as fh:
        for row in all_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(os.path.join(args.out, "runs.jsonl"), "w", encoding="utf-8") as fh:
        for run in runs:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
    print(f"\n{len(runs)} runs, {len(all_rows)} facts -> {args.out}" + (f"; skipped (no input or brief): {skipped}" if skipped else ""))
    return 0


# --------------------------------------------------------------------------- classify
# Per-run sample sizes by state. Stratified so the drop RATE of each category can
# be estimated: each label is weighted by (facts in its run x state) / (sampled).
SAMPLE = {DROPPED_SILENTLY: 15, DROPPED_WITH_REASON: 5, KEPT: 7, CHANGED: 3}  # 30 = one call per run


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def sample_run(rows: list[dict], seed: str) -> list[dict]:
    """Stratified by state, then spread across sections inside each state."""
    rng = random.Random(seed)
    picked = []
    for state, n in SAMPLE.items():
        pool = [r for r in rows if r["state"] == state and r["section_kind"] != "plan"
                and r.get("n_values", 0) <= MAX_VALUES]
        by_section = collections.defaultdict(list)
        for r in pool:
            by_section[r["section_kind"]].append(r)
        for group in by_section.values():
            rng.shuffle(group)
        chosen = []
        while len(chosen) < n and any(by_section.values()):
            for key in sorted(by_section):
                if by_section[key] and len(chosen) < n:
                    chosen.append(by_section[key].pop())
        picked += chosen
    return picked


def question_text(cache_dir: str) -> str:
    art = load(cache_dir)
    q = art.question
    parts = [art.forecast.get("title") or q.get("title") or "", q.get("resolution_criteria") or "",
             q.get("fine_print") or "", q.get("description") or ""]
    return "\n\n".join(p for p in parts if p)


async def cmd_classify_async(args) -> int:
    from diagnostics import qwen_tasks

    if not qwen_tasks.qwen_available():
        raise SystemExit("SoCLaaS key not set; classify needs Qwen on SoCLaaS")
    facts = _read_jsonl(os.path.join(args.out, "facts.jsonl"))
    runs = _read_jsonl(os.path.join(args.out, "runs.jsonl"))
    if args.limit_runs:
        runs = runs[: args.limit_runs]
    labels_path = os.path.join(args.out, "labels.jsonl")
    done = {(r["run_id"], r["question_id"], r["fact_id"]) for r in _read_jsonl(labels_path)}
    plan_path = os.path.join(args.out, "plan.jsonl")
    plan_done = {(r["run_id"], r["question_id"]) for r in _read_jsonl(plan_path)}
    by_run = collections.defaultdict(list)
    for row in facts:
        by_run[(row["run_id"], row["question_id"])].append(row)

    jobs = []  # (run, question, batch)
    for run in runs:
        key = (run["run_id"], run["question_id"])
        sample = [r for r in sample_run(by_run[key], f"{key}") if (key[0], key[1], r["fact_id"]) not in done]
        if not sample:
            continue
        qtext = question_text(run["cache_dir"])
        for i in range(0, len(sample), args.batch):
            jobs.append((run, qtext, sample[i:i + args.batch]))
    print(f"{len(jobs)} label batches to run ({len(done)} labels already done)", flush=True)

    gate = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()

    async def label(job, path, tag):
        run, qtext, batch = job
        async with gate:
            try:
                answers = await qwen_tasks.classify_facts(qtext, [r["claim"] for r in batch])
            except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the study
                print(f"  batch failed for {run['question_id']}: {type(exc).__name__}: {exc}", flush=True)
                return
        async with lock:
            with open(path, "a", encoding="utf-8") as fh:
                for row, answer in zip(batch, answers):
                    fh.write(json.dumps({"run_id": row["run_id"], "question_id": row["question_id"],
                                         "fact_id": row["fact_id"], "state": row["state"], "pass": tag,
                                         **answer}, ensure_ascii=False) + "\n")
        print(f"  {tag}: {run['question_id']} +{len(batch)}", flush=True)

    await asyncio.gather(*(label(job, labels_path, "main") for job in jobs))

    # Stability: label a deterministic tenth of the batches a second time.
    if args.stability > 0 and jobs:
        rng = random.Random("stability")
        again = rng.sample(jobs, max(1, int(len(jobs) * args.stability)))
        await asyncio.gather(*(label(job, os.path.join(args.out, "stability.jsonl"), "repeat") for job in again))

    # Plan coverage: the plan items the brief writer's input held but the brief lacked.
    async def plan(run):
        key = (run["run_id"], run["question_id"])
        if key in plan_done:
            return
        async with gate:
            try:
                check = await qwen_tasks.plan_items(load(run["cache_dir"]))
            except Exception as exc:  # noqa: BLE001
                print(f"  plan check failed for {run['question_id']}: {exc}", flush=True)
                return
        async with lock:
            with open(plan_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"run_id": key[0], "question_id": key[1], "status": check.status,
                                     "detail": (check.value or {}).get("detail") or []}, ensure_ascii=False) + "\n")
    if not args.skip_plan:
        await asyncio.gather(*(plan(run) for run in runs))
    print("classify done", flush=True)
    return 0


# --------------------------------------------------------------------------- report
CATEGORY_NAMES = {
    1: "current value of the target", 2: "past values / base rate", 3: "rule, schedule or mechanism",
    4: "named projection or forecast", 5: "market price", 6: "driver context", 7: "not relevant",
}


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


def _state_table(rows: list[dict], field: str, title: str) -> list[str]:
    groups = collections.defaultdict(collections.Counter)
    for r in rows:
        groups[r[field]][r["state"]] += 1
    lines = [f"### {title}", "", "| group | facts | kept | changed | dropped with reason | dropped silently |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, c in sorted(groups.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(c.values())
        lines.append(f"| {name} | {n} | {_pct(c[KEPT], n)} | {_pct(c[CHANGED], n)} | "
                     f"{_pct(c[DROPPED_WITH_REASON], n)} | {_pct(c[DROPPED_SILENTLY], n)} |")
    return lines + [""]


def cmd_report(args) -> int:
    facts = _read_jsonl(os.path.join(args.out, "facts.jsonl"))
    runs = _read_jsonl(os.path.join(args.out, "runs.jsonl"))
    labels = _read_jsonl(os.path.join(args.out, "labels.jsonl"))
    repeats = _read_jsonl(os.path.join(args.out, "stability.jsonl"))
    plans = _read_jsonl(os.path.join(args.out, "plan.jsonl"))
    blobs = [r for r in facts if r["section_kind"] != "plan" and r.get("n_values", 0) > MAX_VALUES]
    evidence = [r for r in facts if r["section_kind"] != "plan" and r.get("n_values", 0) <= MAX_VALUES]
    total = collections.Counter(r["state"] for r in evidence)
    n = len(evidence)
    lines = [
        "# Brief writer loss study — data tables", "",
        f"{len(runs)} forecasts, {n} facts in the brief writer's evidence sections "
        f"(excluded: {len(facts) - n - len(blobs)} plan-section facts, and {len(blobs)} lines carrying more than "
        f"{MAX_VALUES} values — whole tables squashed into one line, whose state cannot be read value by value).", "",
        "| state | facts | share |", "| --- | ---: | ---: |",
        *[f"| {s} | {total[s]} | {_pct(total[s], n)} |" for s in STATES], "",
        "Facts per run: median " + (str(sorted(r["facts"] for r in runs)[len(runs) // 2]) if runs else "—")
        + f"; briefs with an EXCLUDED passage: {sum(r['has_excluded_passage'] for r in runs)}/{len(runs)}.", "",
    ]
    for field, title in (("section_kind", "By section"), ("search_listing", "Search-result listing vs written claim"),
                         ("source_type", "By source type"),
                         ("value_type", "By value type"), ("position", "By position within its section"),
                         ("table_row", "Table row vs prose"), ("section_compressed", "Section compressed before the brief")):
        lines += _state_table(evidence, field, title)
    qtype = {(r["run_id"], r["question_id"]): r["question_type"] for r in runs}
    for r in evidence:
        r["question_type"] = qtype.get((r["run_id"], r["question_id"]), "?")
    lines += _state_table(evidence, "question_type", "By question type")

    # Forecast use of dropped facts (runs with saved transcripts only)
    used = [r for r in evidence if "raw_run_has" in r]
    if used:
        raw_known = [r for r in used if r["raw_run_has"] is not None]
        lines += ["### Dropped facts and the forecast runs (runs with saved transcripts)", "",
                  f"{len(used)} dropped facts in {len({(r['run_id'], r['question_id']) for r in used})} runs: "
                  f"the raw-research run's transcript holds {sum(r['raw_run_has'] for r in raw_known)} of {len(raw_known)}; "
                  f"brief-reading runs hold {sum(1 for r in used if r['brief_runs_have'])} (values present by coincidence or recalled).", ""]

    if labels:
        # weights: stratum size / sampled, per (run, state)
        size = collections.Counter((r["run_id"], r["question_id"], r["state"]) for r in evidence)
        sampled = collections.Counter((r["run_id"], r["question_id"], r["state"]) for r in labels)
        weight = {k: size[k] / sampled[k] for k in sampled if sampled[k]}
        verified = [r for r in labels if r.get("category") and (r["category"] == 7 or r.get("verified"))]
        unverified = len(labels) - len(verified)
        cat_state = collections.defaultdict(collections.Counter)
        for r in verified:
            cat_state[r["category"]][r["state"]] += weight[(r["run_id"], r["question_id"], r["state"])]
        lines += ["## Which drops matter (Qwen labels, weighted to all facts)", "",
                  f"{len(labels)} labels; {unverified} discarded (quote not found in the question, or no category). "
                  "Rates are estimates: each label stands for the facts of its run and state.", "",
                  "| category | est. facts | kept | changed | dropped with reason | dropped silently |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for cat in sorted(cat_state):
            c = cat_state[cat]
            m = sum(c.values())
            lines.append(f"| {cat}. {CATEGORY_NAMES.get(cat, '?')} | {m:.0f} | {_pct(c[KEPT], m)} | {_pct(c[CHANGED], m)} | "
                         f"{_pct(c[DROPPED_WITH_REASON], m)} | {_pct(c[DROPPED_SILENTLY], m)} |")
        lines.append("")
        # The same breakdowns as above, restricted to decision-relevant facts (1-6).
        fact_by_id = {(r["run_id"], r["question_id"], r["fact_id"]): r for r in evidence}
        for field, title in (("position", "position within its section"), ("section_kind", "section"),
                             ("section_compressed", "section compressed before the brief")):
            groups = collections.defaultdict(collections.Counter)
            for r in verified:
                fact = fact_by_id.get((r["run_id"], r["question_id"], r["fact_id"]))
                if fact is None or r["category"] == 7:
                    continue
                groups[fact[field]][r["state"]] += weight[(r["run_id"], r["question_id"], r["state"])]
            lines += [f"### Decision-relevant facts (categories 1-6) by {title}", "",
                      "| group | est. facts | kept | changed | dropped with reason | dropped silently |",
                      "| --- | ---: | ---: | ---: | ---: | ---: |"]
            for name, c in sorted(groups.items(), key=lambda kv: -sum(kv[1].values())):
                m = sum(c.values())
                lines.append(f"| {name} | {m:.0f} | {_pct(c[KEPT], m)} | {_pct(c[CHANGED], m)} | "
                             f"{_pct(c[DROPPED_WITH_REASON], m)} | {_pct(c[DROPPED_SILENTLY], m)} |")
            lines.append("")
        if repeats:
            first = {(r["run_id"], r["question_id"], r["fact_id"]): r.get("category") for r in labels}
            pairs = [(first.get((r["run_id"], r["question_id"], r["fact_id"])), r.get("category")) for r in repeats]
            pairs = [p for p in pairs if p[0] is not None]
            agree = sum(a == b for a, b in pairs)
            relevant_agree = sum((a == 7) == (b == 7) for a, b in pairs)
            lines += [f"Label stability on {len(pairs)} re-labelled facts: same category {_pct(agree, len(pairs))}; "
                      f"same relevant/not-relevant call {_pct(relevant_agree, len(pairs))}.", ""]
        by_id = {(r["run_id"], r["question_id"], r["fact_id"]): r for r in evidence}
        titles = {(r["run_id"], r["question_id"]): r["title"] for r in runs}
        lines += ["## Examples: decision-relevant facts dropped silently", ""]
        for cat in range(1, 7):
            examples = [r for r in verified if r["category"] == cat and r["state"] == DROPPED_SILENTLY][:5]
            if not examples:
                continue
            lines.append(f"### {cat}. {CATEGORY_NAMES[cat]}")
            for r in examples:
                f = by_id.get((r["run_id"], r["question_id"], r["fact_id"]), {})
                lines.append(f"- Q{r['question_id']} ({titles.get((r['run_id'], r['question_id']), '')[:70]}): "
                             f"\"{f.get('claim', '')[:200]}\" — {f.get('section_kind')}, {f.get('source') or 'no source'}")
            lines.append("")
    if plans:
        states = collections.Counter()
        for p in plans:
            for item in p["detail"]:
                states["in brief" if item.get("in_brief") else
                       "in input, dropped by brief" if item.get("in_research") else "not in the brief writer's input"] += 1
        lines += ["## Evidence-plan items", "", *[f"- {k}: {v}" for k, v in states.most_common()], ""]
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--limit", type=int)
    m.add_argument("--question-id", type=int, action="append")
    c = sub.add_parser("classify")
    c.add_argument("--concurrency", type=int, default=4)  # SoCLaaS refuses a 5th parallel stream
    c.add_argument("--batch", type=int, default=30)
    c.add_argument("--limit-runs", type=int)
    c.add_argument("--stability", type=float, default=0.1)
    c.add_argument("--skip-plan", action="store_true")
    sub.add_parser("report")
    for p in sub.choices.values():
        p.add_argument("--out", default="data/brief-loss-study")
        p.add_argument("--cache", default="diagnostics_runs")
    args = parser.parse_args(argv)
    if args.mode == "measure":
        return cmd_measure(args)
    if args.mode == "classify":
        return asyncio.run(cmd_classify_async(args))
    return cmd_report(args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
