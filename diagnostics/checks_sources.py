"""Search usage, scraping outcomes, the resolution source, and the API agent."""
from __future__ import annotations

import json
import re
from collections import Counter

from diagnostics.loader import RunArtifacts
from diagnostics.result import FAIL, INFO, PASS, WARN, Check, skipped
from research_trace import citation_match

CAT = "sources"
_URL = re.compile(r"https?://[^\s\)\]\'\"<>`]+")
_REFUSED = re.compile(r"refus|rate.?limit|too many requests|blocked", re.I)


def search_usage(a: RunArtifacts) -> Check:
    title = "Search and fetch usage (credits proxy)"
    searches = [{"label": e.get("label"), "queries": len((e.get("meta") or {}).get("queries") or []),
                 "results": (e.get("meta") or {}).get("result_count")} for e in a.events("search_results")]
    per_tool = Counter((row["tool"], row["role"]) for row in a.ledger)
    budget = a.fetch_budget.replace("- ", "").replace("**", "").strip()
    detail = (f"{sum(s['queries'] for s in searches)} search queries in {len(searches)} search call(s); "
              f"{budget or 'no Firecrawl budget line'}")
    return Check(f"{CAT}.search_usage", CAT, title, INFO,
                 detail + ". Exact credit balances need each provider's account API (not recorded).",
                 value={"searches": searches,
                        "ledger": {f"{tool}/{role}": n for (tool, role), n in sorted(per_tool.items())},
                        "firecrawl": budget},
                 evidence=[f"{s['label']}: {s['queries']} queries -> {s['results']} results" for s in searches])


def scrape_outcomes(a: RunArtifacts) -> Check:
    title = "Ranked links were scraped successfully"
    rows = a.ledger
    if not rows:
        return skipped(f"{CAT}.scrapes", CAT, title, "no URL ledger in audit.md")
    scraped = [r for r in rows if r["role"] == "scraped" and not r["engine"].startswith("apiagent")]
    failed = [r for r in scraped if r["status"] != "ok"]
    thin = [e for e in a.events("scrape") if e.get("status") == "thin"]
    ranked = sum(1 for r in rows if r["role"] == "ranked-for-scrape")
    status = WARN if failed or thin else PASS
    return Check(f"{CAT}.scrapes", CAT, title, status,
                 f"{len(scraped)} scraped of {ranked} ranked ({len(failed)} failed, {len(thin)} thin)",
                 value={"ranked": ranked, "scraped": len(scraped), "failed": len(failed), "thin": len(thin),
                        "engines": dict(Counter(r["engine"] for r in scraped))},
                 evidence=[f"FAILED {r['url'][:150]} ({r['engine']})" for r in failed] +
                          [f"THIN {str(e.get('label'))[:150]}: {str(e.get('error'))[:100]}" for e in thin])


def resolution_source(a: RunArtifacts) -> Check:
    title = "Resolution source fetched (and its history, if numeric)"
    criteria = " ".join(str(a.question.get(k) or "") for k in ("resolution_criteria", "fine_print"))
    urls = list(dict.fromkeys(u.rstrip(".,;)") for u in _URL.findall(criteria)))
    if not urls:
        return Check(f"{CAT}.resolution_source", CAT, title, INFO, "the question names no source URL")
    scraped = {r["url"] for r in a.ledger if r["role"] == "scraped" and r["status"] == "ok"} | \
              {str(e.get("label")) for e in a.events("scrape") if e.get("status") == "ok"}
    got = [u for u in urls if any(citation_match(u, s) == "url" for s in scraped)]
    has_series = ("Measured historical series" in a.research_md or "Same calendar window" in a.research_md)
    numeric = a.forecast.get("question_type") in ("numeric", "discrete")
    status = FAIL if not got else WARN if numeric and not has_series else PASS
    detail = f"{len(got)}/{len(urls)} source URL(s) fetched"
    if numeric:
        detail += "; measured history series " + ("present" if has_series else "NOT obtained")
    return Check(f"{CAT}.resolution_source", CAT, title, status, detail,
                 value={"urls": urls, "fetched": got, "measured_series": has_series},
                 evidence=[f"not fetched: {u}" for u in urls if u not in got])


def api_agent(a: RunArtifacts) -> Check:
    title = "API agent retrieved data"
    steps = a.events("apiagent")
    if not steps:
        return skipped(f"{CAT}.apiagent", CAT, title, "the API agent did not run")
    calls, problems, used = 0, [], Counter()
    for event in steps:
        try:
            step = json.loads(a.payload(event))
        except ValueError:
            continue
        outputs = step.get("outputs") or {}
        for call in step.get("calls") or []:
            if call.get("name") != "call_api":
                continue
            calls += 1
            api = (call.get("args") or {}).get("api", "?")
            out = outputs.get(call.get("id")) or {}
            used[api] += 1
            note = str(out.get("note") or "")
            if out.get("error"):
                problems.append(f"{api}: error {str(out['error'])[:120]}")
            elif not out.get("rowCount"):
                problems.append(f"{api}: 0 rows" + (f" -- {note[:120]}" if _REFUSED.search(note) else ""))
    included = any(name == "Public Data APIs" for name, _ in a.providers)
    useful = calls - len(problems)
    status = FAIL if calls and not useful else WARN if problems else PASS
    return Check(f"{CAT}.apiagent", CAT, title, status,
                 f"{calls} data call(s), {useful} returned rows; section "
                 + ("included in research" if included else "NOT included"),
                 value={"apis": dict(used), "calls": calls, "useful": useful, "included": included},
                 evidence=problems[:12])


def uncited_scrapes(a: RunArtifacts) -> Check:
    title = "Scraped pages that the brief cites"
    brief = a.brief
    if not brief:
        return skipped(f"{CAT}.uncited", CAT, title, "no brief")
    pages = list(dict.fromkeys(r["url"] for r in a.ledger if r["role"] == "scraped" and r["status"] == "ok"
                               and not r["engine"].startswith("apiagent")))
    if not pages:
        return skipped(f"{CAT}.uncited", CAT, title, "no scraped pages")
    match = {u: citation_match(u, brief) for u in pages}
    uncited = [u for u, m in match.items() if m is None]
    share = len(uncited) / len(pages)
    status = WARN if share > 0.5 else INFO
    return Check(f"{CAT}.uncited", CAT, title, status,
                 f"{len(pages) - len(uncited)}/{len(pages)} scraped pages cited "
                 f"({sum(1 for m in match.values() if m == 'domain')} by domain only)",
                 value={"pages": len(pages), "uncited": uncited},
                 evidence=[f"never cited: {u[:150]}" for u in uncited[:12]])


CHECKS = (search_usage, scrape_outcomes, resolution_source, api_agent, uncited_scrapes)
