"""Web search: one engine finds the URLs, one shared pass ranks, scrapes and
extracts them.

The engines (SerpAPI -> Tavily -> Firecrawl) are a fallback chain for the
SEARCH alone: an engine that errors or returns no results hands over to the
next. Ranking, the scrape cycles and extraction then run once, on the winning
engine's results.

Until 2026-10-27 each engine ran that whole pipeline itself and the chain fell
through on any exception. A ranking or extract LLM failure therefore re-ran
everything on the next engine (same LLM, mostly the same pages, a second
search bill), while an engine that worked but found nothing stopped the chain
with an empty section, the one case another engine could have fixed.
"""

from __future__ import annotations

import datetime
import logging
import re

from bot.llm.client import call_llm
from bot.llm.cost_manager import HardLimitExceededError
from bot.research.search import firecrawl_research, serp_research, tavily_research
from bot.research.search.query_maker import (
    DEFAULT_QUERY_COUNT,
    DEFAULT_QUERY_GENERATION_MODEL,
    GoogleSearchQuery,
    generate_google_search_query_plan,
)
from bot.research.search.scrape_cycles import (
    DEFAULT_EXTRACT_MODEL,
    DEFAULT_MAX_SCRAPE_CYCLES,
    Cycle,
    build_url_date_map,
    run_scrape_cycles,
    scraped_ok_urls,
    _norm_url,
)
from bot.research.search.search_common import (
    DEFAULT_MAX_RANKED_URLS,
    DEFAULT_URL_RANKING_MODEL,
    SNIPPET_OMITTED_NOTE,
    RankedUrlGroup,
    SearchEngine,
    SearchHit,
    _dedupe_ranked_url_groups,
    _extract_json_value,
    _normalise_query,
    _parse_ranked_url_groups,
    _record_ranked_url_groups,
    exclude_social_results,
)
from bot.trace import research_trace, source_ledger
from bot.utils import display_source_date

logger = logging.getLogger(__name__)

ENGINES: dict[str, SearchEngine] = {
    engine.label: engine
    for engine in (serp_research.ENGINE, tavily_research.ENGINE, firecrawl_research.ENGINE)
}

_MAX_RANKING_INPUT_RESULTS = 80

# An engine that fails with one of these signals will not recover within the
# process (out of credits, auth failure, or a missing API key), so it is
# remembered and skipped for the rest of the run. Transient failures (timeouts,
# connection resets, rate-limit blips) are deliberately excluded so they can be
# retried on later questions.
_EXHAUSTING_ERROR_MARKERS = (
    "402",
    "payment required",
    "401",
    "unauthorized",
    "invalid api key",
    "403",
    "forbidden",
    "432",
    "usage limit",
    "quota",
    "insufficient",
    "out of credit",
    "no credit",
    "run out",
    "out of search",
    "searches left",
    "missing ",  # e.g. "Missing TAVILY_API_KEY for Tavily search research."
)

# Process-level memo of engines that failed with a non-recoverable
# (credit/auth/missing-key) signal; skipped for the remainder of the process.
EXHAUSTED_ENGINES: set[str] = set()


class WebSearchFailed(RuntimeError):
    """No engine returned results; the message lists why each one failed."""


def is_exhausting_error(text: str | None, engine_label: str = "") -> bool:
    """True when an engine's error will not recover this run."""
    lowered = str(text or "").lower()
    # SerpAPI answers 429 when the plan's searches are used up. On 2026-10-05 it
    # did so for every question, each time costing 40-120 s before Tavily took
    # over. (Tavily's 429 is a short rate limit, so this is SerpAPI only.)
    if engine_label == serp_research.ENGINE.label and "429" in lowered:
        return True
    return any(marker in lowered for marker in _EXHAUSTING_ERROR_MARKERS)


async def plan_queries(
    *,
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    asknews_research: str = "",
    preset_queries: list[str] | None = None,
    max_queries: int = DEFAULT_QUERY_COUNT,
    query_model: str = DEFAULT_QUERY_GENERATION_MODEL,
    temperature: float = 0.2,
) -> list[str]:
    """The queries every engine searches: ``preset_queries`` as given (the
    focused artifact retry), else the question title plus the generated plan."""
    if preset_queries:
        return [_normalise_query(query) for query in preset_queries if _normalise_query(query)]
    query_plan = await generate_google_search_query_plan(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        asknews_research=asknews_research,
        max_queries=max_queries,
        model=query_model,
        temperature=temperature,
    )
    return _queries_with_title(title, query_plan)


async def search_with_fallback(
    engines: list[SearchEngine],
    queries: list[str],
    *,
    reason: str = "",
) -> tuple[SearchEngine | None, list[SearchHit], list[str]]:
    """Search with each engine in order until one returns results.

    Returns ``(engine, hits, failures)``; ``engine`` is None when none did.
    ``failures`` reads "<engine> (<why>)" for every engine tried or skipped.
    """
    failures: list[str] = []
    for engine in engines:
        if engine.label in EXHAUSTED_ENGINES:
            failures.append(f"{engine.label} (skipped: out of credits earlier this run)")
            continue
        try:
            hits = await engine.search(queries, reason)
        except HardLimitExceededError:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("[web-search] %s search failed (%s); trying the next engine", engine.label, detail)
            failures.append(f"{engine.label} ({' '.join(detail.split())[:120]})")
            if is_exhausting_error(detail, engine.label):
                EXHAUSTED_ENGINES.add(engine.label)
            continue
        if not hits:
            logger.warning("[web-search] %s returned no results; trying the next engine", engine.label)
            failures.append(f"{engine.label} (no results)")
            continue
        return engine, hits, failures
    return None, [], failures


async def research_from_hits(
    engine: SearchEngine,
    hits: list[SearchHit],
    queries: list[str],
    *,
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    retry: bool = False,
    max_ranked_urls: int = DEFAULT_MAX_RANKED_URLS,
    max_scrape_cycles: int = DEFAULT_MAX_SCRAPE_CYCLES,
    ranking_model: str = DEFAULT_URL_RANKING_MODEL,
    extract_model: str = DEFAULT_EXTRACT_MODEL,
) -> str:
    """Rank, scrape and extract one engine's results; return the section text.

    ``retry`` marks the focused artifact retry: URLs the main pass already
    fetched cannot close the gap it was launched for, so ranking drops them.
    """
    for hit in hits:
        source_ledger.record_url_event(
            hit.url,
            source_ledger.ROLE_CANDIDATE,
            round_label="search",
            detail=" | ".join([f"query: {hit.query}", *(f"{k.lower()}: {v}" for k, v in hit.extras)]),
        )
    research_trace.emit(
        "search_results",
        f"{engine.label} results (per-query rank order)",
        research_trace.format_search_results(hits),
        meta={"queries": queries, "result_count": len(hits), "engine": engine.label},
        reads=research_trace.feeds(), writes=[research_trace.ref_url(h.url) for h in hits],
        kind="search",
    )
    groups, digest = await rank_hits(
        engine,
        hits,
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        max_ranked_urls=max_ranked_urls,
        model=ranking_model,
        exclude_already_scraped=retry,
    )
    _record_ranked_url_groups(groups)
    cycles = await run_scrape_cycles(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        groups=groups,
        max_cycles=max_scrape_cycles,
        model=extract_model,
        url_dates=build_url_date_map((h.url, h.date) for h in hits),
    )
    report = cycles[-1].cumulative_report if cycles else ""
    report = report.replace("# SerpAPI Scraped Research", f"# {engine.report_name} Scraped Research", 1)
    return format_section(engine, queries=queries, hits=hits, groups=groups, cycles=cycles,
                          report=report, digest=digest)


async def run_web_research(
    engines: list[SearchEngine],
    *,
    title: str,
    resolution_criteria: str = "",
    background: str = "",
    fine_print: str = "",
    asknews_research: str = "",
    preset_queries: list[str] | None = None,
    max_scrape_cycles: int = DEFAULT_MAX_SCRAPE_CYCLES,
    reason: str = "",
) -> str:
    """Plan, search with fallback, then rank, scrape and extract once.

    Raises WebSearchFailed when no engine returned results.
    """
    queries = await plan_queries(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        asknews_research=asknews_research,
        preset_queries=preset_queries,
    )
    engine, hits, failures = await search_with_fallback(engines, queries, reason=reason)
    if engine is None:
        raise WebSearchFailed("; ".join(failures) or "no search engine enabled")
    return await research_from_hits(
        engine,
        hits,
        queries,
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        retry=bool(preset_queries),
        max_scrape_cycles=max_scrape_cycles,
    )


async def rank_hits(
    engine: SearchEngine,
    hits: list[SearchHit],
    *,
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    max_ranked_urls: int = DEFAULT_MAX_RANKED_URLS,
    model: str = DEFAULT_URL_RANKING_MODEL,
    exclude_already_scraped: bool = False,
) -> tuple[list[RankedUrlGroup], str]:
    """Group and rank the URLs worth scraping, with one prompt for every engine.

    Returns ``(ranked_groups, digest)``. The digest is written only for an
    engine with ``digest`` set, and is "" otherwise.
    """
    candidates = exclude_social_results(hits)[:_MAX_RANKING_INPUT_RESULTS]
    if not candidates:
        return [], ""

    prompt = _build_ranking_prompt(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        results=candidates,
        max_ranked_urls=max_ranked_urls,
        digest=engine.digest,
    )
    response = await call_llm(
        prompt,
        model=model,
        temperature=0.1,
        _label=f"{engine.slug}-url-ranking",
    )
    json_text, digest = _split_ranking_response(response) if engine.digest else (response, "")
    groups = _dedupe_ranked_url_groups(
        _parse_ranked_url_groups(_extract_json_value(json_text)),
        max_ranked_urls,
        exclude_already_scraped=exclude_already_scraped,
    )
    research_trace.emit(
        "rank",
        f"{engine.slug} ranked url groups",
        research_trace.ranked_groups_payload(groups),
        meta={"model": model, "digest_chars": len(digest)},
        input=prompt,
        reads=[research_trace.ref_url(h.url) for h in candidates],
        writes=[research_trace.ref_url(item.url) for group in groups for item in group.urls],
        kind="model",
    )
    return groups, digest


_RANKING_FENCE_PATTERN = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.DOTALL)


def _split_ranking_response(response: str) -> tuple[str, str]:
    """Split the ranking reply into (JSON text, search-results digest).

    The ranking JSON decides everything that gets scraped, so the digest is
    carried OUTSIDE it rather than as a field inside it: the extract step was
    rewritten to this same prose-outside-JSON shape after one unescaped newline
    in a report-inside-JSON payload discarded a whole research branch (44773).
    A reply with no fence still parses — the digest is simply empty and the
    section falls back to rendering the raw results.
    """
    text = str(response or "")
    for match in _RANKING_FENCE_PATTERN.finditer(text):
        body = match.group(1)
        if '"ranked_url_groups"' not in body:
            continue
        digest = text[match.end():].strip()
        # Drop a leading label line ("PART 2: ...") if the model emitted one.
        if digest.lower().startswith("part 2"):
            digest = digest.split("\n", 1)[1].strip() if "\n" in digest else ""
        return body, digest
    return text, ""


def _hit_lines(hit: SearchHit, *, prefix: str, indent: str, snippet: str) -> list[str]:
    lines = [prefix, f"{indent}URL: {hit.url}"]
    lines.extend(f"{indent}{key}: {value}" for key, value in hit.extras)
    if hit.score is not None:
        lines.append(f"{indent}Score: {hit.score}")
    lines.extend([
        f"{indent}Date: {display_source_date(hit.url, hit.date)}",
        f"{indent}Query: {hit.query}",
        f"{indent}Snippet: {snippet}",
    ])
    return lines


_RANKING_JSON_SHAPE = """{
  "ranked_url_groups": [
    {
      "group": "Current event facts",
      "group_purpose": "Track the latest concrete developments and timeline.",
      "urls": [
        {
          "url": "https://example.com/page",
          "purpose": "What this specific page is likely useful for when scraped later."
        }
      ]
    }
  ]
}"""

_DIGEST_INSTRUCTIONS = """PART 2: after that block, a digest of the candidate results above, as plain
markdown. Write it directly — NOT inside JSON, quotes, or a code fence.

This digest REPLACES the result snippets downstream: the forecaster, the
evidence compiler and the artifact check will see your digest and nothing else
from this list. Most of these pages will never be scraped, so for them your
digest is the only record that will ever exist.

- Carry every concrete fact VERBATIM: figures, counts, dates, identifiers,
  named entities, and quoted claims, each next to the URL it came from. A dated
  numeric fact in a snippet is the highest-value thing on this list — one
  such snippet was the only historical precedent an entire past run found.
- Drop results that carry no fact bearing on the question: pure marketing,
  navigation text, generic explainers, and snippets that only restate the
  page title. Dropping them entirely is correct; do not pad.
- Never invent, complete, or date anything the snippet does not state. Keep
  "(year not stated in source)" where the year is absent, and never assume a
  year-less date falls in the current year.
- Keep both sides: a snippet cutting against the apparent majority reading
  of this list must survive, not be smoothed away.
- Group results that report the same underlying fact into one entry listing all
  their URLs, rather than repeating the fact.
- Order the entries by how much they bear on the question."""


def _build_ranking_prompt(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    results: list[SearchHit],
    max_ranked_urls: int,
    digest: bool = False,
) -> str:
    result_lines = [
        "\n".join(_hit_lines(item, prefix=f"{index}. Title: {item.title}", indent="   ",
                             snippet=item.snippet or "Not provided."))
        for index, item in enumerate(results, start=1)
    ]
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if digest:
        output_format = (
            "Output format — exactly two parts, in this order.\n\n"
            "PART 1: one fenced JSON block, in this exact shape:\n\n"
            f"```json\n{_RANKING_JSON_SHAPE}\n```\n\n{_DIGEST_INSTRUCTIONS}"
        )
    else:
        output_format = f"Return only valid JSON in this exact shape:\n{_RANKING_JSON_SHAPE}"

    return f"""
You are ranking web search result URLs for a forecasting research pipeline.

Today's date is {today}. Date discipline: a result whose "Date" line is missing or whose
snippet shows a date WITHOUT a year (e.g. "Aug 7") must never be assumed to be from the
current year or from the question's resolution window — old posts and syndicated copies
surface constantly. No report can describe events after today. If you select such a URL,
its stated purpose must say "date unconfirmed" rather than asserting it covers the
resolution window.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or "Not provided."}

Background:
{background or "Not provided."}

Fine print:
{fine_print or "Not provided."}

Candidate search results:
{chr(10).join(result_lines)}

Choose up to {max_ranked_urls} total URLs that should be scraped next.

Group the chosen URLs by the distinct research purpose they serve. Rank groups
from most important to least important for answering the forecasting question,
and rank URLs within each group from best to worst.

Important grouping rules:
- Put overlapping sources with the same purpose in the same group.
- Do not repeat the same URL in multiple groups.
- If a URL could serve multiple purposes, choose the single best group for it.
- Prefer enough groups to cover different evidence types instead of letting one
  category crowd out everything else.
- Use question-specific groups when useful. Common group types include current
  event facts, official/resolution sources, procedural or legal mechanics,
  historical/base-rate evidence, political or stakeholder incentives, public
  sentiment, and quantitative indicators.

Rank higher:
- Official resolution sources, primary datasets, laws/regulations, company/government pages, and reputable statistics.
- Pages likely to contain dated facts, quantitative evidence, definitions, methodology, historical data, or recent developments.
- Sources that directly bear on the resolution criteria.

Rank lower or omit:
- Duplicates, thin SEO pages, social posts without evidence, broad homepages, and pages unlikely to have stable scrapeable text.

{output_format}
""".strip()


def format_section(
    engine: SearchEngine,
    *,
    queries: list[str],
    hits: list[SearchHit],
    groups: list[RankedUrlGroup],
    cycles: list[Cycle],
    report: str,
    digest: str = "",
) -> str:
    ranked_group_lines = []
    for group_index, group in enumerate(groups, start=1):
        ranked_group_lines.extend(
            [
                f"{group_index}. {group.group}",
                f"   Group purpose: {group.group_purpose}",
            ]
        )
        for url_index, item in enumerate(group.urls, start=1):
            ranked_group_lines.extend(
                [
                    f"   {url_index}. {item.url}",
                    f"      Purpose: {item.purpose}",
                ]
            )
        ranked_group_lines.append("")

    cycle_lines = []
    for cycle in cycles:
        cycle_lines.append(f"Cycle {cycle.cycle}:")
        for scrape in cycle.scrapes:
            status = "ok" if scrape.ok else f"failed ({scrape.error})"
            cycle_lines.append(f"- [{status}] {scrape.group}: {scrape.url}")
        if cycle.lacking_groups:
            cycle_lines.append(f"  Lacking after cycle: {', '.join(cycle.lacking_groups)}")
        else:
            cycle_lines.append("  Lacking after cycle: none")
        cycle_lines.append("")

    if digest:
        results_heading = (
            f"{engine.report_name} search results considered (digest written by the "
            "ranking step, which read every result's full snippet; results judged "
            "irrelevant were dropped rather than summarised):"
        )
        results_block = digest
    else:
        results_heading = f"Raw {engine.report_name} search results considered:"
        # Snippets of unscraped pages are kept whole: for those pages the snippet
        # is the only copy of the evidence (44619, 44879 [E9]).
        scraped_urls = scraped_ok_urls(cycles)
        raw_lines = [
            "NOTE: snippets below may show dates without a year (e.g. \"Aug 7\") — these are "
            "often OLD posts; never assume such a date is current-year or falls in the "
            "question's resolution window. Where a Date line is decoded from a tweet ID it is "
            "authoritative for that post.",
            "",
        ]
        for index, hit in enumerate(hits, start=1):
            snippet = (
                SNIPPET_OMITTED_NOTE
                if _norm_url(hit.url) in scraped_urls
                else (hit.snippet or "Not provided.")
            )
            raw_lines.extend(_hit_lines(hit, prefix=f"[{index}] {hit.title}", indent="    ", snippet=snippet))
            raw_lines.append("")
        results_block = "\n".join(raw_lines).strip() if hits else "No search results found."

    settings = "\n".join(engine.settings())
    query_lines = "\n".join(f"- {query}" for query in queries)
    return f"""
======================================================================
{engine.label.upper()} RESEARCH
======================================================================
{chr(10) + settings + chr(10) if settings else ""}
Generated Google-style queries:
{query_lines or "- No queries generated."}

Ranked URL groups for later scraping:
{chr(10).join(ranked_group_lines).strip() if ranked_group_lines else "No URL groups were ranked."}

Scrape cycles:
{chr(10).join(cycle_lines).strip() if cycle_lines else "No scrape cycles ran."}

Compiled scraped research:
{report or "No scraped research report generated."}

{results_heading}
{results_block}
======================================================================
""".strip()


def _queries_with_title(title: str, query_plan: list[GoogleSearchQuery]) -> list[str]:
    queries = [title.strip(), *(item.query for item in query_plan)]
    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        normalised = _normalise_query(query)
        key = normalised.lower()
        if normalised and key not in seen:
            seen.add(key)
            deduped.append(normalised)
    return deduped
