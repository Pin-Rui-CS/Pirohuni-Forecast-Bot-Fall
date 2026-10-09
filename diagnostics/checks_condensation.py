"""What each condensation stage cut, what survived, and how much junk remained.

Everything here is measured, not judged: sizes before and after each cap,
the share of numbers and URLs that reach the next stage, and the density of
navigation/boilerplate lines. (Asking Qwen whether Qwen removed the right
boilerplate would grade the testee with itself -- see the plan.)
"""
from __future__ import annotations

import re

from diagnostics.loader import RunArtifacts
from diagnostics.result import INFO, PASS, WARN, Check, skipped

CAT = "condensation"
_SCRAPE_CAP = 40_000
_EXTRACT_CAP = 180_000
_RAW_VIEW_CAP = 200_000
# The raw-research Qwen forecaster failed at 165k (46133) and 180k (46022)
# characters and succeeded at 130k (45859).
_QWEN_RAW_RISK = 150_000
_FIRECRAWL_SIZE = re.compile(r"\[firecrawl\] scraped (\S+) \((\d+) chars md")
_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,})(?![\w])")
_URLISH = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|gov|net|edu|io|co|uk|us|int|info)(?:/[^\s)\]`'\"|]*)?", re.I)
_BOILER = re.compile(
    r"skip to (main )?content|cookie|privacy policy|terms of (use|service)|all rights reserved|"
    r"sign (in|up)|log ?in|subscribe|newsletter|^\s*(menu|home|search)\s*$|©|"
    r"^\s*!\[[^\]]*\]\([^)]*\)\s*$|^\s*\[[^\]]+\]\([^)]*\)\s*$|^\s*(\|\s*)+$|^\s*\|?(\s*-{3,}\s*\|)+\s*$",
    re.I)


def _numbers(text: str) -> set[str]:
    out = set()
    for token in _NUMBER.findall(text or ""):
        clean = token.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", clean):  # a year is a date, not a value
            continue
        out.add(clean.rstrip("0").rstrip(".") if "." in clean else clean)
    return out


def _hosts(text: str) -> set[str]:
    return {m.group(0).lower().split("/")[0].removeprefix("www.") for m in _URLISH.finditer(text or "")}


def scrape_truncation(a: RunArtifacts) -> Check:
    title = "Scraped pages cut by the 40k cap"
    scrapes = [e for e in a.events("scrape") if e.get("status") in ("ok", "thin")]
    if not scrapes:
        return skipped(f"{CAT}.scrape_cap", CAT, title, "no scrape events")
    sizes = {}
    for line in a.run_log_all:
        match = _FIRECRAWL_SIZE.search(line)
        if match:
            sizes[match.group(1)] = int(match.group(2))
    cut = []
    for event in scrapes:
        meta = event.get("meta") or {}
        url = str(event.get("label"))
        hit = meta.get("truncated") or int(event.get("chars") or 0) >= _SCRAPE_CAP
        if not hit:
            continue
        raw = int(meta.get("raw_chars") or 0) or sizes.get(url, 0)
        cut.append((url, raw))
    status = WARN if cut else PASS
    return Check(f"{CAT}.scrape_cap", CAT, title, status,
                 f"{len(cut)} of {len(scrapes)} pages hit the cap",
                 value={"cut": [{"url": u, "raw_chars": r} for u, r in cut]},
                 evidence=[f"{u[:140]}: kept 40,000 of {r:,} chars ({40_000 / r:.0%})" if r
                           else f"{u[:140]}: cut at 40,000 (original size not recorded)" for u, r in cut])


def extract_cap(a: RunArtifacts) -> Check:
    title = "Extraction input under its 180k cap"
    calls = [c for c in a.llm_calls if str(c.get("name_of_task") or "").startswith("serp-scrape-extract")]
    if not calls:
        return skipped(f"{CAT}.extract_cap", CAT, title, "no extraction calls")
    near = [c for c in calls if int(c.get("input_characters") or 0) >= _EXTRACT_CAP * 0.97]
    return Check(f"{CAT}.extract_cap", CAT, title, WARN if near else PASS,
                 f"largest extraction input {max(int(c.get('input_characters') or 0) for c in calls):,} chars",
                 value={"inputs": [int(c.get("input_characters") or 0) for c in calls]},
                 evidence=[f"{c.get('name_of_task')}: {int(c.get('input_characters') or 0):,} chars (cap reached)"
                           for c in near])


def compiler_fit(a: RunArtifacts) -> Check:
    title = "Sections cut to fit the brief writer's budget"
    events = a.events("compiler_input")
    if not events:
        return skipped(f"{CAT}.compiler_fit", CAT, title, "no compiler_input event")
    sections = (events[-1].get("meta") or {}).get("sections") or []
    provider_sizes = {str(e.get("label")): int(e.get("chars") or 0) for e in a.events("provider")}
    rows, worst = [], []
    for section in sections:
        name, after = section.get("name"), int(section.get("chars") or 0)
        before = int(section.get("chars_before_fit") or provider_sizes.get(name) or after)
        kept = after / before if before else 1.0
        rows.append({"section": name, "before": before, "after": after, "kept": round(kept, 3)})
        if kept < 0.6:
            worst.append(f"{name}: {before:,} -> {after:,} chars ({kept:.0%} kept)")
    visible_cuts = [f"{e.get('label')}: {e.get('error')}" for e in a.events("precompress")
                    if e.get("status") == "truncated"]
    brief_cut = [e for e in a.events("brief") if e.get("status") == "truncated"]
    status = WARN if visible_cuts or brief_cut or worst else PASS
    source = "chars_before_fit" if any("chars_before_fit" in s for s in sections) else \
             "provider sizes (approximate: before cleaning)"
    return Check(f"{CAT}.compiler_fit", CAT, title, status,
                 f"{len(worst)} section(s) kept under 60%; {len(visible_cuts)} visible truncation(s); "
                 f"brief output {'TRUNCATED' if brief_cut else 'complete'}; sizes from {source}",
                 value={"sections": rows}, evidence=worst + visible_cuts)


def precompression_loss(a: RunArtifacts) -> Check:
    title = "Precompression kept URLs and numbers"
    if not a.events("precompress"):
        return Check(f"{CAT}.precompress", CAT, title, INFO, "no precompression in this run")
    if not a.precompression:
        return skipped(f"{CAT}.precompress", CAT, title,
                       "precompression folders not available (GitHub artifact expired or not downloaded)")
    lost = []
    for name, entry in a.precompression.items():
        effort = (entry["manifest"] or {}).get("selected_effort")
        checks = ((entry["results"].get(effort) or {}).get("checks") or {}) if effort else {}
        urls, nums = len(checks.get("missing_urls") or []), len(checks.get("missing_number_tokens") or [])
        if urls or nums:
            lost.append(f"{name} ({effort}): {urls} URLs and {nums} numbers missing from the summary")
    return Check(f"{CAT}.precompress", CAT, title, WARN if lost else PASS,
                 f"{len(lost)} of {len(a.precompression)} compressed chunk(s) lost URLs or numbers",
                 evidence=lost)


def raw_view(a: RunArtifacts) -> Check:
    title = "Raw-research forecaster input size"
    events = a.events("raw_view")
    forecasts = [c for c in a.llm_calls if "-forecast[" in str(c.get("name_of_task") or "")]
    if events:
        meta = events[-1].get("meta") or {}
        chars, uncapped = int(meta.get("chars") or 0), int(meta.get("uncapped_chars") or 0)
    elif forecasts:
        chars = uncapped = max(int(c.get("input_characters") or 0) for c in forecasts)
    else:
        return skipped(f"{CAT}.raw_view", CAT, title, "no forecast calls")
    raw_call = max(forecasts, key=lambda c: int(c.get("input_characters") or 0)) if forecasts else {}
    on_qwen = "qwen" in str(raw_call.get("model_used") or "")
    evidence = []
    if uncapped > _RAW_VIEW_CAP:
        evidence.append(f"view cut: {uncapped:,} chars of research -> {_RAW_VIEW_CAP:,}")
    if on_qwen and chars >= _QWEN_RAW_RISK:
        evidence.append(f"{chars:,} chars on Qwen: past the ~150k size where its streams have failed "
                        "(46133 at 165k, 46022 at 180k)" + ("; this run's call FAILED" if raw_call.get("error") else ""))
    return Check(f"{CAT}.raw_view", CAT, title, WARN if evidence else PASS,
                 f"{chars:,} chars to {raw_call.get('model_used', '?')}", value={"chars": chars, "uncapped": uncapped},
                 evidence=evidence)


def survival(a: RunArtifacts) -> Check:
    """Share of numbers and sources that reach each next stage (item 7, measured part)."""
    title = "Numbers and sources surviving each stage"
    compiler = a.events("compiler_input")
    if not compiler or not a.brief:
        return skipped(f"{CAT}.survival", CAT, title, "no compiler input or brief")
    research = "\n".join(content for name, content in a.providers if name != "Evidence Plan")
    stages = [("research", research), ("brief writer input", a.payload(compiler[-1])), ("brief", a.brief)]
    rows = []
    for (name_a, text_a), (name_b, text_b) in zip(stages, stages[1:]):
        na, nb = _numbers(text_a), _numbers(text_b)
        ha, hb = _hosts(text_a), _hosts(text_b)
        rows.append({"from": name_a, "to": name_b,
                     "numbers": f"{len(na & nb)}/{len(na)}", "numbers_kept": round(len(na & nb) / len(na), 3) if na else None,
                     "sources": f"{len(ha & hb)}/{len(ha)}", "sources_kept": round(len(ha & hb) / len(ha), 3) if ha else None})
    return Check(f"{CAT}.survival", CAT, title, INFO,
                 "; ".join(f"{r['from']} -> {r['to']}: numbers {r['numbers']}, sources {r['sources']}" for r in rows),
                 value={"stages": rows,
                        "dropped_numbers_sample": sorted(_numbers(stages[1][1]) - _numbers(a.brief))[:40]})


def boilerplate(a: RunArtifacts) -> Check:
    """Navigation/boilerplate line density at each stage (item 8, code proxy)."""
    title = "Boilerplate left at each stage"

    def density(text: str) -> tuple[float, int]:
        lines = [l for l in (text or "").splitlines() if l.strip()]
        if not lines:
            return 0.0, 0
        return sum(1 for l in lines if _BOILER.search(l)) / len(lines), len(lines)

    compiler = a.events("compiler_input")
    stages = [
        ("scraped pages", "\n".join(a.payload(e) for e in a.events("scrape") if e.get("status") == "ok")),
        ("extract reports", "\n".join(a.payload(e) for e in a.events("extract_report"))),
        ("brief writer input", a.payload(compiler[-1]) if compiler else ""),
        ("brief", a.brief),
    ]
    rows = []
    for name, text in stages:
        share, lines = density(text)
        rows.append({"stage": name, "boilerplate_share": round(share, 3), "lines": lines})
    late = [r for r in rows[2:] if r["boilerplate_share"] > 0.10]
    return Check(f"{CAT}.boilerplate", CAT, title, WARN if late else INFO,
                 "; ".join(f"{r['stage']}: {r['boilerplate_share']:.0%} of {r['lines']:,} lines" for r in rows),
                 value={"stages": rows},
                 evidence=[f"{r['stage']} still {r['boilerplate_share']:.0%} boilerplate" for r in late])


CHECKS = (scrape_truncation, extract_cap, compiler_fit, precompression_loss, raw_view, survival, boilerplate)
