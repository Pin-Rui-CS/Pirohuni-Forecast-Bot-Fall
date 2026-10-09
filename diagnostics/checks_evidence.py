"""The brief: structure, where each evidence item came from, and history coverage."""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from diagnostics.checks_condensation import _hosts
from diagnostics.loader import RunArtifacts
from diagnostics.result import FAIL, INFO, PASS, WARN, Check, skipped

CAT = "evidence"
_ITEM = re.compile(r"^\s*\[E(\d+)\]\s*(.+)$", re.M)
_REQUIRED_SECTIONS = ("## Key Evidence", "## Balance Check", "## Gaps And Cautions")
_HISTORY_WORDS = re.compile(r"histor|prior year|previous year|past (year|winner|result|edition)|base rate|"
                            r"annual|each year|every year|trend|time series|track record", re.I)
_YEAR = re.compile(r"(?<![\d.])(19[5-9]\d|20[0-4]\d)(?![\d.])")  # not 1997 in "1997.5"
_VALUE = re.compile(r"\d[\d,]*\.?\d*")


def _items(brief: str) -> list[tuple[str, str]]:
    return [(f"E{n}", text.strip()) for n, text in _ITEM.findall(brief)]


def brief_structure(a: RunArtifacts) -> Check:
    title = "Brief has its required sections and sourced evidence"
    brief = a.brief
    if not brief:
        return Check(f"{CAT}.brief_structure", CAT, title, FAIL, "no compiled brief")
    missing = [s for s in _REQUIRED_SECTIONS if s not in brief]
    items = _items(brief)
    unsourced = [eid for eid, text in items if not _hosts(text) and "http" not in text]
    status = FAIL if missing or not items else WARN if unsourced else PASS
    return Check(f"{CAT}.brief_structure", CAT, title, status,
                 f"{len(items)} evidence items, {len(unsourced)} without a source; "
                 + (f"missing sections: {', '.join(missing)}" if missing else "all sections present"),
                 value={"items": len(items), "unsourced": unsourced, "missing_sections": missing,
                        "chars": len(brief)})


def attribution(a: RunArtifacts) -> Check:
    """Each [E#] item -> source hosts -> which research task fetched them (item 9)."""
    title = "Every brief evidence item traces to a research task"
    items = _items(a.brief)
    if not items:
        return skipped(f"{CAT}.attribution", CAT, title, "no evidence items in the brief")
    host_tools: dict[str, set[str]] = {}
    for row in a.ledger:
        host = re.sub(r"^https?://(www\.)?", "", row["url"].lower()).split("/")[0]
        host_tools.setdefault(host, set()).add(f"{row['tool']} ({row['phase']})")
    sections = [(name, content.lower()) for name, content in a.providers if name != "Evidence Plan"]
    rows, orphans = [], []
    for eid, text in items:
        hosts = sorted(_hosts(text))
        tools = sorted({t for h in hosts for t in host_tools.get(h, ())})
        found_in = sorted({name for name, content in sections for h in hosts if h in content})
        rows.append({"item": eid, "hosts": hosts, "tasks": tools, "sections": found_in})
        if not tools and not found_in:
            orphans.append(f"{eid}: {text[:160]}")
    share = len(orphans) / len(items)
    status = WARN if share > 0.3 else PASS
    by_section = Counter(s for r in rows for s in r["sections"])
    return Check(f"{CAT}.attribution", CAT, title, status,
                 f"{len(items) - len(orphans)}/{len(items)} items traced; items per research section: "
                 + ", ".join(f"{k} {v}" for k, v in by_section.most_common()),
                 value={"items": rows}, evidence=orphans[:10])


def banner_vs_reality(a: RunArtifacts) -> Check:
    title = "Artifact banner is truthful"
    check = a.forecast.get("artifact_check") or {}
    banner_found = "FOUND —" in a.brief or "FOUND -" in a.brief
    resolve = str(a.question.get("scheduled_resolve_time") or "")[:10]
    run_day = str(a.forecast.get("run_timestamp") or "")[:10]
    try:
        future = dt.date.fromisoformat(resolve) > dt.date.fromisoformat(run_day)
    except ValueError:
        future = False
    if banner_found and future:
        return Check(f"{CAT}.banner", CAT, title, WARN,
                     f"banner says the resolution value was FOUND, but the question resolves {resolve} "
                     f"(after the run on {run_day}); forecasters are told to weight it heavily",
                     value={"status": check.get("status"), "resolve": resolve, "run": run_day})
    return Check(f"{CAT}.banner", CAT, title, PASS, f"banner status {check.get('status') or 'none'}",
                 value={"status": check.get("status")})


def temporal_flags(a: RunArtifacts) -> Check:
    title = "No impossible (future-dated) claims"
    text = a.brief + str(a.forecast.get("artifact_check") or "")
    flags = re.findall(r"TEMPORAL (?:IMPOSSIBILITY|FLAG)[^\]]{0,200}", text)
    return Check(f"{CAT}.temporal", CAT, title, WARN if flags else PASS,
                 f"{len(flags)} temporal flag(s)", evidence=flags[:5])


def history_coverage(a: RunArtifacts) -> Check:
    """Did the plan ask for history, and how many dated values reached the brief (item 10)."""
    title = "Historical record reached the brief"
    artifact = " ".join(a.plan_list("Required Evidence Artifact"))
    asked = bool(_HISTORY_WORDS.search(artifact))
    # Count a year only where a table row pairs it with a value: "| 2018 | ... | 2,469 |".
    # A loose "year and number on one line" rule counted 1997 from "1997.5" and
    # years listed as missing (46133). Prefer the brief's Extracted Artifact Rows.
    rows_section = re.search(r"## Extracted Artifact Rows(.*?)(?=\n## |\Z)", a.brief, re.S)
    scope = rows_section.group(1) if rows_section else a.brief
    years = set()
    for line in scope.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip(" *`") for c in line.strip().strip("|").split("|")]
        cell_years = [y for c in cells for y in _YEAR.findall(c) if re.fullmatch(r"\W*\d{4}\W*", c)]
        values = [c for c in cells if _VALUE.fullmatch(c.replace(" ", "")) and not _YEAR.fullmatch(c)]
        if cell_years and values:
            years.update(cell_years)
    if not asked:
        return Check(f"{CAT}.history", CAT, title, INFO,
                     f"plan did not ask for a historical record; {len(years)} dated value year(s) in the brief",
                     value={"asked": False, "years": sorted(years)})
    status = WARN if len(years) < 5 else PASS
    return Check(f"{CAT}.history", CAT, title, status,
                 f"plan asked for history; brief carries values for {len(years)} distinct year(s): "
                 + ", ".join(sorted(years)),
                 value={"asked": True, "years": sorted(years)},
                 evidence=[f"required artifact: {artifact[:240]}"])


CHECKS = (brief_structure, attribution, banner_vs_reality, temporal_flags, history_coverage)
