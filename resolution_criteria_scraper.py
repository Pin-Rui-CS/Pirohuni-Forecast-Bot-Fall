"""Question source URL scraper.

Takes the full forecast question context, extracts any URLs embedded in it,
scrapes those URLs, and returns clean formatted content ready for an LLM to
read when making a forecast.

Integrated into forecasting_bot.py (imported at module level, called with use_llm_cleaning=True
for binary, numeric, and multiple-choice question types).

Usage:
    import asyncio
    from resolution_criteria_scraper import scrape_resolution_sources

    result = asyncio.run(scrape_resolution_sources(
        resolution_criteria="Resolves YES if ... (see https://example.com/data)",
        question_text="Will X happen by 2026?",
        use_llm_cleaning=False,   # set True to also run LLM summarization
    ))
    print(result)
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import llm_provider  # noqa: E402
from config import llm_rate_limiter  # noqa: E402
from monetary_cost_manager import (  # noqa: E402
    HardLimitExceededError,
    MonetaryCostManager,
)

# Resolution sources are scraped with Firecrawl FIRST (its rendering handles
# JS-heavy and PDF sources well); the local Crawl4AI basic crawl is the
# fallback. The Firecrawl client, exhaustion flag, and per-question credit
# budget live in research.firecrawl_scrape (shared with the general research
# scrape path); resolution scrapes pass priority=True, which gives them first
# claim on the per-question credit cap.
from research.firecrawl_scrape import (  # noqa: E402
    RESOLUTION_MAX_AGE_MS,
    FirecrawlCreditError as _FirecrawlCreditError,
    firecrawl_exhausted,
    firecrawl_scrape_markdown,
    mark_firecrawl_exhausted,
    release_resolution_reserve,
)

logger = logging.getLogger(__name__)


def _require_llm_route(model: str, label: str):
    """Route this model and fail fast when its endpoint has no key.

    Which key is required depends on where the model is routed, not on a
    single global provider -- under a mixed profile the resolution scraper and
    the compiler can legitimately need different ones.
    """
    route = llm_provider.route_for(model, label=label)
    if not llm_provider.api_key_for(route.endpoint):
        raise ValueError(
            f"{route.endpoint.api_key_env} is required for LLM-based source cleaning."
        )
    return route


def _log_openrouter_call(label: str, model: str, route) -> None:
    logger.info(
        "%s | model=%s | %s usage recorded",
        label,
        model,
        route.endpoint.label,
    )

# ===========================================================================
# 1. URL extraction
# ===========================================================================

_URL_PATTERN = re.compile(r'https?://[^\s\)\]\'"<>]+', re.IGNORECASE)

def extract_urls(text: str) -> list[str]:
    """Return unique HTTP/HTTPS URLs found in the supplied question text.

    Strips trailing punctuation and deduplicates while preserving order.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for url in _URL_PATTERN.findall(text):
        url = url.rstrip(".,;:!?)")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ===========================================================================
# 2. Heuristic content cleaning
# ===========================================================================

_MAX_CONTENT_CHARS = 8_000
# Per-URL budget for resolution-source scrapes. The resolution source is by
# definition the page the question resolves off, and a head-truncation here
# silently drops whatever the page keeps at its tail (the 44382 miss: an
# 18k budget cut a 27-row country table to 6 alphabetical rows, making the
# counted set unverifiable). The budget therefore matches the summarizer's own
# input capacity; pages larger than the summarizer input are chunk-summarized
# in scrape_resolution_sources rather than silently cut.
_CRAWL4AI_CONTENT_BUDGET = 100_000
# Reduced per-URL budget for URLs found only in the question BACKGROUND text
# (not the resolution criteria or fine print). Background links are usually
# context (Wikipedia primers, news articles), and on 44773 two Wikipedia pages
# at ~60K chars each consumed two extra 15K-token summary calls.
#
# Raised 20K -> 50K on 45087. The old value sat BELOW the length of an ordinary
# institutional page: WastewaterSCAN's monthly newsletter cleans to 23,386
# chars and keeps its per-pathogen results at the tail, so a 20K head-cut
# discarded "no positive detections of WNV in July 2026" (offset 21,998) and
# the matching Mpox clade Ib line (offset 20,558) — the entire reference class
# for the question — while the summarizer they were trimmed for used 46,123 of
# its 100,000-char budget. A background cap must clear the document class that
# stores its data at the tail (institutional pages and newsletters, 20-40K) and
# still trim the 60K+ primers that motivated the tier; 50K does both.
#
# A fetch-time cut is made blind: this stage does not know what the question
# needs, cannot see whether another source covers the same fact, and its loss
# is irreversible for the rest of the run. Never set this below the length of
# a document class we expect to read.
_BACKGROUND_CONTENT_CHARS = 50_000

# The Wayback history pass reconstructs a resolution page's value history and
# update cadence from archive captures — built for slowly-updating
# institutional pages (the 44382 class). For live market/quote pages the live
# scrape already contains the full price history, and archive captures would
# only inject STALE prices (the 44267 misdating class), so it is skipped.
# The domain list and helper live in Adapters.Wayback, shared with the
# failed-scrape snapshot fallback.
from Adapters.Wayback import is_market_data_url as _is_market_data_url  # noqa: E402

# Lines matching any of these patterns are likely boilerplate
_BOILERPLATE_PATTERNS = [
    re.compile(r'^\s*\[.{1,60}\]\([^)]{1,200}\)\s*$'),       # bare markdown link line
    re.compile(r'(cookie notice|accept cookies|privacy policy'
               r'|terms of service|newsletter|subscribe now)', re.I),
    re.compile(r'(javascript is (required|disabled)|enable javascript'
               r'|browser not supported)', re.I),
]

_FRONTMATTER = re.compile(r'^---\n.*?\n---\n', re.DOTALL)
# Matches markdown links/images — two variants depending on whether we need
# to capture the URL.
# Group layout for _MD_LINK_KEEP_URL: group(1)=label, group(2)=url
_MD_LINK_STRIP = re.compile(r'!\[[^\]]*\]\([^)]*\)|\[([^\]]+)\]\([^)]*\)')
_MD_LINK_KEEP  = re.compile(r'!\[[^\]]*\]\([^)]*\)|\[([^\]]+)\]\(([^)]*)\)')


def _strip_md_links(content: str) -> str:
    """Convert [text](url) → text and remove ![img](url) entirely."""
    def _replace(m: re.Match) -> str:
        if m.group(0).startswith('!'):
            return ''
        return m.group(1)
    return _MD_LINK_STRIP.sub(_replace, content)


def _inline_md_links(content: str) -> str:
    """Convert [text](url) → text (url) and remove ![img](url) entirely.

    Preserves the URL as plain text while still removing markdown syntax so
    the boilerplate filter (which matches raw [text](url) lines) does not
    accidentally drop useful entries.
    """
    def _replace(m: re.Match) -> str:
        if m.group(0).startswith('!'):
            return ''
        url = (m.group(2) or "").strip()
        return f"{m.group(1)} ({url})" if url else m.group(1)
    return _MD_LINK_KEEP.sub(_replace, content)


def _clean_content(content: str, max_chars: int = _MAX_CONTENT_CHARS, keep_urls: bool = False) -> str:
    """Heuristic cleanup of scraped markdown:

    1. Strip YAML frontmatter (added by the scraper's save_result helper).
    2. Handle markdown links:
       - keep_urls=False (default): [text](url) → text  (URL discarded)
       - keep_urls=True:            [text](url) → text (url)  (URL kept as plain text)
       Images are removed in both modes.
    3. Drop obvious boilerplate lines (cookie notices, bare nav links, etc.).
    4. Collapse runs of blank lines to at most two.
    5. Truncate to max_chars with a notice.
    """
    content = _FRONTMATTER.sub("", content).strip()
    content = _inline_md_links(content) if keep_urls else _strip_md_links(content)

    lines = [
        line for line in content.splitlines()
        if not any(p.search(line) for p in _BOILERPLATE_PATTERNS)
    ]
    content = re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()

    if len(content) > max_chars:
        content = content[:max_chars] + _truncation_notice(len(content), max_chars)

    return content


def _truncation_notice(original: int, kept: int) -> str:
    """State what a cut discarded, not merely that one happened.

    A silent head-cut is indistinguishable downstream from a page that never
    had the data -- artifact-check then reports "missing" and the forecasters
    widen for the wrong reason. This is the shared symptom behind the 44382,
    44512, 44619 and 44875 misses; on 44875 the retrievable payload was 232 KB
    of date-ascending JSON, so an 8,000-char head-cut would have kept 2023 and
    dropped every row the question was about.
    """
    dropped = max(0, original - kept)
    return (
        f"\n\n[TRUNCATED: kept the first {kept:,} of {original:,} chars "
        f"({dropped / original:.1%} discarded, from the END of the document). "
        f"Absence of a fact below is NOT evidence the source lacks it.]"
    )


# ===========================================================================
# 3. Optional LLM-based summarization
# ===========================================================================

_LLM_MAX_INPUT = 100_000  # chars sent to the LLM


def _build_resolution_summary_prompt(
    question_text: str,
    resolution_criteria: str,
    url: str,
    content: str,
    key_terms: list[str] | None = None,
) -> str:
    key_terms_section = ""
    if key_terms:
        terms_list = ", ".join(f'"{t}"' for t in key_terms)
        key_terms_section = (
            f"## Key Terms to Search For\n"
            f"The resolution criteria require entries matching these specific terms/labels: "
            f"{terms_list}\n"
            f"Search the scraped content for these exact strings and report how many times "
            f"each appears, and in what context.\n\n"
        )

    return (
        "You are a research assistant helping a forecaster understand the official "
        "resolution source material for a forecasting question.\n\n"
        f"## Forecast Question\n{question_text}\n\n"
        f"## Resolution Criteria\n{resolution_criteria}\n\n"
        f"{key_terms_section}"
        f"## Scraped Resolution Source Content ({url})\n{content[:_LLM_MAX_INPUT]}\n\n"
        "## Task\n"
        "Write one structured summary of the scraped resolution source content. "
        "Use exactly these four sections:\n\n"
        "**1. CURRENT STATE:** What does the source currently show? Include exact "
        "dates, labels, values, or status fields that matter for resolution.\n\n"
        "**2. GAP TO RESOLUTION:** What exactly would need to appear or change on "
        "the source for this question to resolve? Has any part of the criteria "
        "already been met?\n\n"
        "**3. HISTORICAL PATTERN:** If the scraped content contains relevant past "
        "entries, list the most relevant dates and describe the cadence. If not, "
        "state that the pattern is not present in the scraped content.\n\n"
        "**4. KEY AMBIGUITY:** Flag any mismatch between the resolution criteria "
        "and what the scraped source actually displays, including labels, date "
        "ranges, formatting, or scoping issues.\n\n"
        "## Important Rules\n"
        "- Base your summary only on the scraped content provided above.\n"
        "- Do not search for, identify, request, or recommend additional links.\n"
        "- Do not use external knowledge to fill gaps in the scraped data.\n"
        "- If information is missing, say 'not present in scraped content'.\n"
        "- Quote exact strings where they are important to the resolution criteria.\n"
        "- TABLES AND ENUMERATIONS: if the content contains a table or list that the "
        "resolution value counts over or reads from, reproduce the resolution-relevant "
        "rows (entity + status/value) rather than summarizing them away — for a question "
        "that counts rows in a category, that means EVERY row and its classification. "
        "Then state the row arithmetic explicitly (e.g. '27 rows present: 9 Clear, 12 "
        "Partial, 6 Unclear'). If the rows present do not add up to a total the page "
        "states, or the table appears cut off, say so explicitly ('N of M rows present; "
        "content appears truncated').\n"
        "- Summarize by reading for relevance, not to hit a length. Include every "
        "detail that bears on the resolution criteria, however small — do NOT drop "
        "or compress resolution-relevant facts to make the summary shorter. The "
        "only things to leave out are navigation, boilerplate, and content with no "
        "bearing on the question."
    )


def _summary_token_cap(num_pages: int) -> int:
    """Output token budget for the resolution summary, scaled by page count.

    The cap is a guardrail against boilerplate/formatting cruft bloating the
    summary — it is NOT a compression target. More scraped pages mean more
    legitimate content to report, so the budget grows with the number of pages.
    """
    return min(2000 + 1500 * max(0, num_pages), 8000)


async def _summarize_snapshot_history(
    url: str,
    snapshots: list,
    question_text: str,
    resolution_criteria: str,
    model: str,
    max_tokens: int = 1200,
) -> str:
    """Turn dated Wayback captures of the resolution source into a compact,
    dated history: value time series, update cadence, and observed changes.

    One LLM call regardless of snapshot count. The output feeds the compiler's
    Resolution Mechanics section, giving the forecaster a real same-source flow
    rate instead of an improvised one.
    """
    route = _require_llm_route(model, "resolution-scraper/wayback-history")
    model = route.model_id
    client = llm_provider.async_client_for(route)

    snapshot_blocks = "\n\n---\n\n".join(
        f"## Snapshot captured {snapshot.iso_date}\n{snapshot.text}"
        for snapshot in snapshots
    )
    prompt = (
        "You are a research assistant reconstructing the HISTORY of the official "
        "resolution source for a forecasting question, from dated Wayback Machine "
        "captures of the page.\n\n"
        f"## Forecast Question\n{question_text}\n\n"
        f"## Resolution Criteria\n{resolution_criteria}\n\n"
        f"## Dated captures of {url} (oldest first)\n{snapshot_blocks[:_LLM_MAX_INPUT]}\n\n"
        "## Task\n"
        "Write a compact history with exactly these three sections:\n\n"
        "**1. VALUE TIME SERIES:** For each capture date, the value(s) the "
        "resolution criteria care about, one line per capture: "
        "`YYYY-MM-DD (capture): <value(s)>`. Quote exact figures/labels. If a "
        "capture does not show the value, write 'not visible in capture'.\n\n"
        "**2. UPDATE CADENCE:** Any 'last updated'/'as of' stamps visible in the "
        "captures, and what the differences between captures imply about how often "
        "the page actually changes. State the observed gaps in weeks/months.\n\n"
        "**3. OBSERVED CHANGES:** What changed between consecutive captures "
        "(entries added/removed, statuses reclassified, totals moved). Compute the "
        "simple rate of change per month where the series allows it, showing the "
        "arithmetic.\n\n"
        "## Rules\n"
        "- Use ONLY the captures above. No external knowledge, no speculation "
        "about what later values 'should' be.\n"
        "- These captures are HISTORICAL. Do not present any of them as the "
        "current value; the live page was scraped separately.\n"
        "- If the captures are unreadable or irrelevant to the resolution "
        "criteria, say exactly that in one line per section."
    )
    messages = [{"role": "user", "content": prompt}]

    await llm_provider.gate_for(route).wait_async()
    async with llm_rate_limiter:
        usage_handle = MonetaryCostManager.start_openrouter_call(
            "resolution-scraper/wayback-history",
            model,
            {"messages": messages, "max_tokens": max_tokens},
        )
        response = await client.chat.completions.create(
            **llm_provider.build_kwargs(
                route,
                {"messages": messages, "max_tokens": max_tokens, "temperature": 0.1},
            )
        )
    usage_handle.record_response(response)
    _log_openrouter_call("resolution-scraper/wayback-history", model, route)
    return response.choices[0].message.content.strip()


async def _build_wayback_history_section(
    url: str,
    question_text: str,
    resolution_criteria: str,
    model: str,
) -> str:
    """Fetch Wayback history for the primary resolution URL and summarize it.

    Returns a ready-to-append markdown section, or "" when no usable history
    exists. Every failure is soft — history is a bonus, never a blocker.
    """
    try:
        from Adapters.Wayback import fetch_snapshot_history

        snapshots = await fetch_snapshot_history(url)
    except Exception as exc:
        logger.warning("Wayback history fetch failed for %s: %s", url, exc)
        return ""
    if len(snapshots) < 2:
        return ""
    try:
        history = await _summarize_snapshot_history(
            url=url,
            snapshots=snapshots,
            question_text=question_text,
            resolution_criteria=resolution_criteria,
            model=model,
        )
    except HardLimitExceededError:
        raise
    except Exception as exc:
        logger.warning("Wayback history summarization failed for %s: %s", url, exc)
        return ""
    if not history.strip():
        return ""
    capture_dates = ", ".join(snapshot.iso_date for snapshot in snapshots)
    return (
        "\n\n## Resolution Source History (Wayback Machine)\n\n"
        f"Historical captures of {url} ({capture_dates}) — use for the value's "
        "flow rate and the page's real update cadence; the live scrape above "
        "remains the current value.\n\n"
        f"{history}"
    )


async def _llm_summarize(
    url: str,
    content: str,
    question_text: str,
    resolution_criteria: str,
    model: str,
    key_terms: list[str] | None = None,
    max_tokens: int = 2000,
) -> str:
    """Summarize scraped page content into a structured forecast-ready report.

    Uses the same provider setup as the main forecasting bot.
    Falls back to the heuristically-cleaned content if the call fails.
    """
    route = _require_llm_route(model, "resolution-scraper/page-summary")
    model = route.model_id
    client = llm_provider.async_client_for(route)

    prompt = _build_resolution_summary_prompt(question_text, resolution_criteria, url, content, key_terms)
    messages = [{"role": "user", "content": prompt}]

    await llm_provider.gate_for(route).wait_async()
    async with llm_rate_limiter:
        usage_handle = MonetaryCostManager.start_openrouter_call(
            "resolution-scraper/page-summary",
            model,
            {"messages": messages, "max_tokens": max_tokens},
        )
        response = await client.chat.completions.create(
            **llm_provider.build_kwargs(
                route,
                {"messages": messages, "max_tokens": max_tokens, "temperature": 0.1},
            )
        )
    usage_handle.record_response(response)
    _log_openrouter_call("resolution-scraper/page-summary", model, route)
    return response.choices[0].message.content.strip()


# ===========================================================================
# 4. Follow-up link extraction and compilation
# ===========================================================================

@dataclass(frozen=True)
class _ResolutionScrapeResult:
    url: str
    provider_used: str
    success: bool
    content: str = ""
    error: str = ""


def _truncate_scrape_content(content: str, max_chars: int = _CRAWL4AI_CONTENT_BUDGET) -> str:
    content = str(content or "").strip()
    if len(content) <= max_chars:
        return content
    if max_chars <= 100:
        return content[:max_chars].rstrip()
    kept = max_chars - 200
    return content[:kept].rstrip() + _truncation_notice(len(content), kept)


def _format_combined_resolution_content(sources: list[tuple[str, str]]) -> str:
    return "\n\n---\n\n".join(
        f"## Source: {url}\n\n{content}" for url, content in sources
    )


# Hard cap on summary LLM calls per question when the combined resolution
# content exceeds one summarizer input. Bounds token spend on pathological
# link-heavy questions; sources are ordered resolution-criteria-first, so the
# batches that matter most are always summarized.
_MAX_SUMMARY_CALLS = 3


def _batch_resolution_sources(
    sources: list[tuple[str, str]], max_chars: int = _LLM_MAX_INPUT
) -> list[list[tuple[str, str]]]:
    """Group (url, content) sources into batches that each fit one summarizer
    input, instead of silently tail-truncating the combined content.

    An individual source larger than ``max_chars`` is split into sequential
    labelled parts so no portion of a resolution page is dropped unseen.
    Source order is preserved.
    """
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_size = 0
    overhead = 40  # per-source "## Source:" header/separator allowance

    def flush() -> None:
        nonlocal current, current_size
        if current:
            batches.append(current)
            current = []
            current_size = 0

    for url, content in sources:
        pieces: list[tuple[str, str]] = []
        if len(content) > max_chars:
            n_parts = -(-len(content) // max_chars)
            for i in range(n_parts):
                pieces.append(
                    (f"{url} (part {i + 1}/{n_parts})", content[i * max_chars:(i + 1) * max_chars])
                )
        else:
            pieces.append((url, content))
        for piece_url, piece in pieces:
            size = len(piece) + len(piece_url) + overhead
            if current and current_size + size > max_chars:
                flush()
            current.append((piece_url, piece))
            current_size += size
    flush()
    return batches


async def _basic_crawl_markdown(url: str, timeout: int) -> str:
    """Fetch one page with a single-page browser crawl; return full raw markdown.

    Thin wrapper over the shared ``Crawl4AI.crawl.basic_crawl_markdown`` so the
    resolution scraper and the research scrapers use one identical basic crawl.
    The resolution source is the page that holds the value being resolved, so its
    full markdown is what we want, handed downstream to the LLM-cleaning step.
    Returns "" if the page did not load successfully.
    """
    from Crawl4AI.crawl import basic_crawl_markdown

    return await basic_crawl_markdown(url, timeout=timeout)


async def _scrape_resolution_url(
    url: str,
    question_text: str,
    resolution_criteria: str,
    timeout: int,
) -> _ResolutionScrapeResult:
    """Scrape one resolution URL, Firecrawl first with a Crawl4AI fallback.

    The resolution source is the page named by the resolution criteria, so the
    value being resolved is on that page by definition; the full page markdown is
    handed to the LLM-cleaning step to read against the criteria.

    Firecrawl's /v2/scrape is tried first (it renders JS and PDFs well). On a
    credit/auth exhaustion it is disabled for the rest of the run; on any other
    Firecrawl failure (timeout, page error, empty result) we fall back to the
    local Crawl4AI basic crawl for just this URL. The basic crawl returns the full
    raw page markdown (no relevance filter, no link-following) so short,
    semantically-thin facts (e.g. a bare "Predictions 3,903,957") are preserved.
    """
    import source_ledger

    from Crawl4AI.crawl import claim_scrape_url, get_cached_scrape_content, record_scrape_content

    if claim_scrape_url(url) is not None:
        cached = get_cached_scrape_content(url)
        if cached:
            content = _truncate_scrape_content(cached)
            source_ledger.record_url_event(
                url,
                source_ledger.ROLE_SCRAPED,
                engine=source_ledger.ENGINE_CACHE,
                ok=True,
                chars=len(content),
                round_label="single pass",
            )
            return _ResolutionScrapeResult(
                url=url,
                provider_used="scrape cache",
                success=True,
                content=content,
            )
        source_ledger.record_url_event(
            url,
            source_ledger.ROLE_SCRAPED,
            engine=source_ledger.ENGINE_SKIPPED_DUPLICATE,
            ok=False,
            error="skipped duplicate: URL has already been scraped in this process",
            round_label="single pass",
        )
        return _ResolutionScrapeResult(
            url=url,
            provider_used="dedupe registry",
            success=False,
            error="skipped duplicate: URL has already been scraped in this process",
        )

    def _record_and_return(engine: str, provider: str, content: str, error: str) -> _ResolutionScrapeResult:
        success = bool(content.strip())
        source_ledger.record_url_event(
            url,
            source_ledger.ROLE_SCRAPED,
            engine=engine,
            ok=success,
            error="" if success else error,
            chars=len(content),
            round_label="single pass",
        )
        return _ResolutionScrapeResult(
            url=url,
            provider_used=provider,
            success=success,
            content=content,
            error="" if success else error,
        )

    # ------------------------------------------------------------------
    # 0. Google Sheets: fetch the CSV export directly. Page-scraping a large
    #    sheet returns whatever rows the rendered grid shows (for PLATracker
    #    that meant early-2021 rows only, dropping the resolution window), so
    #    the deterministic export path always runs first for sheet URLs.
    # ------------------------------------------------------------------
    try:
        from Adapters.GoogleSheets import GoogleSheetsAdapter

        _sheets_adapter = GoogleSheetsAdapter()
        if _sheets_adapter.can_handle(url):
            try:
                result = await _sheets_adapter.extract(url, query=question_text, timeout=timeout)
                record_scrape_content(url, result.content)
                content = _truncate_scrape_content(result.content)
                if content.strip():
                    return _record_and_return(
                        f"adapter:{_sheets_adapter.name}",
                        f"adapter:{_sheets_adapter.name}",
                        content,
                        "",
                    )
                logger.info(
                    "Google Sheets CSV export returned no content for %s; "
                    "falling back to page scraping.", url,
                )
            except Exception as exc:
                logger.warning(
                    "Google Sheets CSV export failed for %s: %s — falling back to "
                    "page scraping.", url, exc,
                )
    except ImportError as exc:
        logger.warning("Google Sheets adapter unavailable: %s", exc)

    # ------------------------------------------------------------------
    # 0.5. Yahoo Finance quote pages: fetch the chart API directly — the same
    #      data the page renders, without the scrape lottery (44773: the
    #      resolution source BZ=F). Fail-open: the adapter itself falls back
    #      to a plain crawl, and an empty result falls through to Firecrawl.
    # ------------------------------------------------------------------
    try:
        from Adapters.YahooQuotes import YahooQuotesAdapter

        _quotes_adapter = YahooQuotesAdapter()
        if _quotes_adapter.can_handle(url):
            try:
                result = await _quotes_adapter.extract(url, query=question_text, timeout=timeout)
                record_scrape_content(url, result.content)
                content = _truncate_scrape_content(result.content)
                if content.strip():
                    return _record_and_return(
                        f"adapter:{_quotes_adapter.name}",
                        f"adapter:{_quotes_adapter.name}",
                        content,
                        "",
                    )
                logger.info(
                    "Yahoo quotes adapter returned no content for %s; "
                    "falling back to page scraping.", url,
                )
            except Exception as exc:
                logger.warning(
                    "Yahoo quotes adapter failed for %s: %s — falling back to "
                    "page scraping.", url, exc,
                )
    except ImportError as exc:
        logger.warning("Yahoo quotes adapter unavailable: %s", exc)

    # ------------------------------------------------------------------
    # 1. Firecrawl single-page scrape (skipped once credits/auth exhausted).
    # ------------------------------------------------------------------
    if not firecrawl_exhausted():
        try:
            raw = await firecrawl_scrape_markdown(
                url, timeout, max_age_ms=RESOLUTION_MAX_AGE_MS, priority=True
            )
            record_scrape_content(url, raw)
            content = _truncate_scrape_content(raw)
            if content.strip():
                return _record_and_return(
                    source_ledger.ENGINE_FIRECRAWL, "firecrawl-scrape", content, ""
                )
            logger.info("Firecrawl returned no content for %s; falling back to Crawl4AI.", url)
        except _FirecrawlCreditError as exc:
            mark_firecrawl_exhausted()
            logger.warning(
                "Firecrawl exhausted (credits/auth) on %s: %s — falling back to "
                "Crawl4AI for this and all remaining resolution URLs this run.",
                url, exc,
            )
        except Exception as exc:
            logger.warning(
                "Firecrawl scrape failed for %s: %s — falling back to Crawl4AI.", url, exc
            )

    # ------------------------------------------------------------------
    # 2. Crawl4AI basic crawl fallback.
    # ------------------------------------------------------------------
    crawl_error = ""
    try:
        raw = await _basic_crawl_markdown(url, timeout)
        record_scrape_content(url, raw)
        content = _truncate_scrape_content(raw)
        if content.strip():
            return _record_and_return(
                source_ledger.ENGINE_CRAWL4AI_BASIC, "crawl4ai-basic", content, ""
            )
        crawl_error = "Crawl4AI returned no content."
    except HardLimitExceededError:
        raise
    except Exception as exc:
        crawl_error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 3. Wayback snapshot fallback: pages the live web will not serve
    #    (paywalls, bot walls — the 44773 NYT case). Never for market/quote
    #    pages, where an archive capture is stale price data (44267 class);
    #    snapshot_fallback_text stamps the capture date in-band.
    # ------------------------------------------------------------------
    try:
        from Adapters import Wayback as wayback_module

        snapshot_text = await wayback_module.snapshot_fallback_text(url)
    except Exception as exc:
        logger.warning("Wayback snapshot fallback failed for %s: %s", url, exc)
        snapshot_text = ""
    if snapshot_text.strip():
        record_scrape_content(url, snapshot_text)
        return _record_and_return(
            "wayback-snapshot",
            "wayback-snapshot",
            _truncate_scrape_content(snapshot_text),
            "",
        )

    return _record_and_return(
        source_ledger.ENGINE_CRAWL4AI_BASIC, "crawl4ai-basic", "", crawl_error
    )


async def _scrape_resolution_urls(
    urls: list[str],
    question_text: str,
    resolution_criteria: str,
    max_concurrent: int,
    timeout: int,
) -> list[_ResolutionScrapeResult]:
    semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def scrape_one(url: str) -> _ResolutionScrapeResult:
        async with semaphore:
            return await _scrape_resolution_url(
                url=url,
                question_text=question_text,
                resolution_criteria=resolution_criteria,
                timeout=timeout,
            )

    results = await asyncio.gather(*(scrape_one(url) for url in urls))
    import research_trace

    for result in results:
        research_trace.emit(
            "scrape",
            result.url,
            result.content if result.success else (result.error or "(no content)"),
            status="ok" if result.success else "failed",
            error="" if result.success else result.error,
            meta={"engine": result.provider_used, "phase": "resolution"},
        )
    return results


# ===========================================================================
# 6. Main pipeline
# ===========================================================================

async def scrape_resolution_sources(
    resolution_criteria: str,
    question_text: str = "",
    use_llm_cleaning: bool = False,
    llm_model: str = "anthropic/claude-sonnet-5",
    max_concurrent: int = 5,
    timeout: int = 30,
    max_urls: int = 10,
    question_type: str = "",
    fine_print: str = "",
) -> str:
    """Scrape the URLs embedded in the question and summarize them once.

    Candidate URLs are gathered from BOTH the resolution criteria and the
    question context (background + fine print), deduped with criteria URLs
    first. They are NOT exclusive: the primary source that holds the resolved
    value is often linked only in the background (e.g. a congress.gov bill page)
    while the criteria cite only a generic definitions/FAQ link. Scraping is a
    free local crawl, and all sources feed a single combined summary call, so
    every candidate (up to ``max_urls`` as a guard against pathological link
    counts) is scraped concurrently rather than pre-filtered.

    ``fine_print`` is read for URLs at the CRITERIA tier, not the background
    tier. A source the question names as the thing it resolves off is a
    resolution source wherever the question happens to name it, and fine print
    is where backup/fallback sources are conventionally declared ("in case of
    accessibility issues ... may check the August monthly update"). On 45087
    that backup source was passed in only via ``question_text``, so it drew the
    reduced background budget and was cut 558 chars short of the answer.
    Background and evidence-plan links keep the reduced tier: promoting those
    would reintroduce the 44773 cost problem the tier exists to prevent.
    """

    criteria_urls = extract_urls(
        "\n".join(part for part in (resolution_criteria, fine_print) if part)
    )
    seen: set[str] = set()
    urls: list[str] = []
    for url in [*criteria_urls, *extract_urls(question_text)]:
        if url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        logger.info("No external URLs found in question text or resolution criteria.")
        release_resolution_reserve()
        return ""

    if len(urls) > max_urls:
        logger.info(
            "Found %d resolution URL(s); scraping the first %d.", len(urls), max_urls
        )
        urls = urls[:max_urls]
    logger.info("Found %d resolution URL(s): %s", len(urls), urls)
    try:
        results = await _scrape_resolution_urls(
            list(urls),
            question_text=question_text,
            resolution_criteria=resolution_criteria,
            max_concurrent=max_concurrent,
            timeout=timeout,
        )
    finally:
        # Resolution scraping holds first claim on the per-question Firecrawl
        # credit budget; once its URLs are done (or failed), hand the rest of
        # the cap to the general research path.
        release_resolution_reserve()

    # Deep series retrieval, primary resolution URL only. Free (plain HTTP,
    # no LLM, no Firecrawl), so it is not budgeted -- but it IS restricted to
    # one URL and to questions whose answer is a number, because that is the
    # only case where a historical trend is the thing being asked for.
    series_section = ""
    if criteria_urls or urls:
        series_section = await _build_measured_series_section(
            (criteria_urls or urls)[0],
            question_text=question_text,
            resolution_criteria=resolution_criteria,
            question_type=question_type,
            timeout=timeout,
        )

    criteria_url_set = set(criteria_urls)
    sections: list[str] = []
    cleaned_sources: list[tuple[str, str]] = []
    for result in results:
        if not result.success:
            logger.warning("Failed to scrape %s: %s", result.url, result.error)
            sections.append(f"## Source: {result.url}\n_Scrape failed: {result.error}_")
            continue

        # Criteria URLs get the full 44382-safe budget; background-only URLs
        # get the reduced tier (they are usually context, and every char here
        # is paid for again in the batched summary calls).
        if use_llm_cleaning:
            heuristic_max = (
                _LLM_MAX_INPUT
                if result.url in criteria_url_set
                else _BACKGROUND_CONTENT_CHARS
            )
        else:
            heuristic_max = _MAX_CONTENT_CHARS
        cleaned = _clean_content(result.content, max_chars=heuristic_max, keep_urls=False)
        if not cleaned.strip():
            sections.append(f"## Source: {result.url}\n_No usable content extracted._")
            continue

        cleaned_sources.append((result.url, cleaned))
        sections.append(
            f"## Source: {result.url}\n"
            f"_Scraped via {result.provider_used}_\n\n"
            f"{cleaned}"
        )

    if not sections and not series_section:
        return ""

    # Historical captures of the primary resolution URL (the page the value
    # resolves off). Gives the forecaster a same-source flow rate and update
    # cadence; soft-fails to "" when the page has no usable archive history.
    history_section = ""
    if use_llm_cleaning:
        primary_url = criteria_urls[0] if criteria_urls else urls[0]
        if _is_market_data_url(primary_url):
            logger.info(
                "Skipping Wayback history for %s: a live market/quote page already "
                "carries its own history; archive captures would only add stale prices.",
                primary_url,
            )
        else:
            history_section = await _build_wayback_history_section(
                primary_url,
                question_text=question_text,
                resolution_criteria=resolution_criteria,
                model=llm_model,
            )

    if use_llm_cleaning and cleaned_sources:
        try:
            batches = _batch_resolution_sources(cleaned_sources)
            omitted_urls = [url for batch in batches[_MAX_SUMMARY_CALLS:] for url, _ in batch]
            batches = batches[:_MAX_SUMMARY_CALLS]
            summaries: list[str] = []
            for index, batch in enumerate(batches, start=1):
                summary = await _llm_summarize(
                    url=", ".join(url for url, _ in batch),
                    content=_format_combined_resolution_content(batch),
                    question_text=question_text,
                    resolution_criteria=resolution_criteria,
                    model=llm_model,
                    max_tokens=_summary_token_cap(len(batch)),
                )
                if len(batches) > 1:
                    summary = (
                        f"### Summary part {index}/{len(batches)} "
                        f"(covers: {', '.join(url for url, _ in batch)})\n\n{summary}"
                    )
                summaries.append(summary)
            summary_text = "\n\n".join(summaries)
            if omitted_urls:
                summary_text += (
                    "\n\n_Note: content from the following source(s) exceeded the "
                    "summarization budget and was NOT summarized: "
                    + ", ".join(omitted_urls) + "_"
                )
            source_lines = "\n".join(f"- {url}" for url, _ in cleaned_sources)
            return (
                "# Resolution Criteria Sources\n\n"
                f"{series_section}"
                "## Summary\n\n"
                f"{summary_text}\n\n"
                "## Scraped Sources\n\n"
                f"{source_lines}"
                f"{history_section}"
            )
        except HardLimitExceededError:
            raise
        except Exception as exc:
            logger.warning("LLM summary failed: %s - returning heuristic output", exc)

    return (
        "# Resolution Criteria Sources\n\n"
        + series_section
        + "\n\n---\n\n".join(sections)
        + history_section
    )


# ===========================================================================
# Measured-series retrieval (free: plain HTTP, no LLM, no Firecrawl)
# ===========================================================================

# Question types whose answer is a number with a history behind it. Binary and
# multiple-choice questions resolve on an event, not a level, so the deep
# retrieval would only ever be wasted requests.
_SERIES_QUESTION_TYPES = {"numeric", "discrete"}


async def _build_measured_series_section(
    primary_url: str,
    *,
    question_text: str,
    resolution_criteria: str,
    question_type: str,
    timeout: int,
) -> str:
    """Retrieve and reduce the historical series behind the resolution source.

    Returns "" whenever this does not apply or nothing was found, so the caller
    is unaffected. The reduced block is ~2-3 KB by construction and therefore
    survives every downstream character budget intact -- the raw payload (232 KB
    on 44875) never enters the prose pipeline at all.
    """
    if question_type and question_type.lower() not in _SERIES_QUESTION_TYPES:
        return ""
    if not primary_url:
        return ""

    try:
        from series_discovery import discover_series
        from series_reduce import reduce_series
    except ImportError as exc:
        logger.warning("Series discovery unavailable: %s", exc)
        return ""

    try:
        artifact = await discover_series(primary_url, timeout=timeout)
    except Exception as exc:
        logger.warning("Series discovery failed for %s: %s", primary_url, exc)
        return ""

    if artifact is None:
        logger.info("No historical series found behind %s; no rung satisfied the gate.",
                    primary_url)
        return ""

    logger.info(
        "Measured series for %s: %d rows via %s from %s",
        primary_url, artifact.rows, artifact.rung, artifact.endpoint,
    )
    try:
        reduced = reduce_series(
            artifact.table,
            endpoint=artifact.endpoint,
            metric_hint=f"{question_text}\n{resolution_criteria}",
        )
    except Exception as exc:
        logger.warning("Series reduction failed for %s: %s", artifact.endpoint, exc)
        return ""

    try:
        import source_ledger

        source_ledger.record_url_event(
            artifact.endpoint,
            source_ledger.ROLE_SCRAPED,
            engine="series-discovery",
            ok=True,
            chars=len(artifact.payload),
            round_label="single pass",
        )
    except Exception:  # pragma: no cover - ledger is best-effort
        pass

    return (
        "## Measured historical series\n\n"
        f"_Retrieved deterministically from the resolution source "
        f"({artifact.rung} rung); statistics below are computed, not summarised, "
        f"and must be reproduced verbatim rather than paraphrased._\n\n"
        f"{reduced.text}\n"
        "---\n\n"
    )
