"""Scrape cycles shared by every web-search provider (SerpAPI, Tavily, Firecrawl Search).

Each provider searches and ranks URLs into purpose groups its own way, then
hands the groups to ``run_scrape_cycles``: fetch the top URL per group down the
scrape ladder (adapter -> Firecrawl -> Crawl4AI -> Firecrawl render-wait ->
Wayback snapshot), have the extract LLM report what the pages say and which
groups still lack evidence, and repeat for those groups up to
``DEFAULT_MAX_SCRAPE_CYCLES`` times.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable

from bot.config import ENABLE_FIRECRAWL_GENERAL_SCRAPE
from bot.llm.client import call_llm
from bot.llm.cost_manager import HardLimitExceededError, MonetaryCostManager
from bot.utils import _truncate_text, tweet_url_date
from bot.trace import research_trace
from bot.trace import source_ledger
from bot.research.search.search_common import RankedUrl, RankedUrlGroup, _limited_gather

logger = logging.getLogger(__name__)

DEFAULT_EXTRACT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_MAX_SCRAPE_CYCLES = 3
# Per-scrape retention, applied BEFORE the extract stage reads the page.
#
# Was 18_000 — one fifth of the budget of the only stage that consumes it. That
# is a fetch-time cut made blind: this layer does not know which of the page's
# facts the question needs, cannot see whether any other source covers them,
# and its loss is irreversible for the rest of the run (the focused artifact
# retry can only search again, and searching cannot un-truncate a document).
# On 45087 it cut the WastewaterSCAN June, May and March newsletters at exactly
# 18,000 chars — all three mid-H5-influenza, all three before the West Nile
# Virus and Mpox clade Ib sections that were the question's entire reference
# class. Head-keep makes that a systematic bias rather than random loss:
# boilerplate lives at the top of a document and the specific data at the
# bottom, so the same category of evidence dies every time (the 44619
# section-ordering lesson, one layer upstream).
#
# 40K clears that document class with margin. It is deliberately NOT raised to
# the full extract budget: the extract stage head-truncates the CONCATENATION
# of a cycle's scrapes (_build_extract_prompt), not each scrape, so a per-scrape cap equal
# to the combined cap would let the first page of a cycle consume the whole
# budget and silently delete every later page. The two constants below must
# move together, and the invariant is:
#
#     _MAX_SCRAPE_CHARS * (typical scrapes per cycle) <= _MAX_EXTRACT_INPUT_CHARS
#
# Until the extract stage cuts by relevance (or splits into several calls)
# instead of by position, that inequality is what keeps the cut off the data.
_MAX_SCRAPE_CHARS = 40_000
# Raised 90K -> 180K alongside _MAX_SCRAPE_CHARS so ~4-5 full-size pages in one
# cycle still fit whole. This is a head-cut of the joined cycle text, so when it
# does bind it drops the LAST scrapes of the cycle entirely, not a tail from
# each — the reason it must stay comfortably above the per-scrape cap.
_MAX_EXTRACT_INPUT_CHARS = 180_000
# Below this, a fetch is treated as "shell only" and the ladder keeps escalating
# instead of accepting the page. Deliberately generous: because inadequate
# content is never DISCARDED (the best attempt is returned with a warning
# banner), this threshold governs how hard we try, not what we believe. A false
# "inadequate" costs one extra fetch on the free rungs; a false "adequate" cost
# 44875 its resolution source (bsky.jazco.dev/stats returned 246-250 chars of
# nav boilerplate five times, each recorded ok, so the ladder never escalated).
_MIN_SUBSTANTIVE_SCRAPE_CHARS = 500
# Wall/placeholder markers are only consulted on short pages: a long article
# that happens to contain "access denied" is fine, a 300-char page that says it
# is not.
_WALL_MARKER_MAX_CHARS = 2_000
_WALL_MARKERS = (
    "enable javascript",
    "requires javascript",
    "javascript is disabled",
    "javascript to run",
    "please enable js",
    "checking your browser",
    "verify you are human",
    "are you a robot",
    "access denied",
    "403 forbidden",
    "subscribe to continue",
    "subscribe to read",
    "create an account to continue",
)
# Firecrawl's own page-render timeout for general research scrapes (its HTTP
# client gets +15 s headroom on top; see research.firecrawl_scrape).
_FIRECRAWL_GENERAL_TIMEOUT_SECONDS = 30
# Render-wait rung: held open long enough for client-side JS to populate the
# DOM. 8 s was the value verified against bsky.jazco.dev/stats; the longer
# page timeout gives the render room to finish inside the request.
_RENDER_WAIT_MS = 8_000
_FIRECRAWL_RENDER_TIMEOUT_SECONDS = 45
_ENGINE_FIRECRAWL_RENDER = "firecrawl-render-wait"
# Soft-stop gate for the scrape-cycle loop: measured extract calls ran 6K-25K
# input tokens in chars/4 units (44773 ledger); rescaled x1.25 to the
# 3.2-chars/token units introduced 2026-07-19 (same physical budget). A cycle
# that cannot afford this much would eat into the input tokens reserved for
# compile/forecast — stop cycling and report with the cycles already done.
_EXPECTED_CYCLE_INPUT_TOKENS = 31_250
# Minimum pages an extract call should cover. Below this, _cycle_targets
# backfills from the groups that still need evidence, so one extract call
# covers more ground instead of the loop paying a fresh call per page.
# Deliberately backfill-only: an earlier version also STOPPED the loop when a
# late cycle held one page, which would have discarded the last ranked URL for
# a group the extractor had just reported as lacking -- the 44512/44619 shape,
# where the decisive evidence is exactly the thing that never got fetched.
_MIN_CYCLE_TARGETS = 3


@dataclass(frozen=True)
class Scrape:
    cycle: int
    group: str
    group_purpose: str
    url: str
    purpose: str
    ok: bool
    content: str = ""
    error: str = ""
    # Size of the page before the _MAX_SCRAPE_CHARS cut (0 = not measured).
    # Recorded in the trace so a diagnosis can see what the cap removed.
    raw_chars: int = 0


def scrape_adequacy(content: str) -> tuple[bool, str]:
    """Did this fetch return usable research material, or only the page shell?

    Returns ``(adequate, reason)``; ``reason`` is empty when adequate.

    Every rung of the scrape ladder used to gate on ``content.strip()`` alone,
    which asks "did bytes come back", not "did the page's data come back".
    Those differ precisely on the pages that matter most for numeric questions:
    client-side-rendered dashboards, paywall interstitials, cookie/bot walls.
    All return a real DOM full of navigation and no data, so the ladder
    short-circuited at the first rung and the free fallbacks never ran.

    This is a *thin content* test, not a *wrong content* test. Judging whether
    the page holds the specific value we came for is the extract stage's job;
    doing it here would need the question in scope and would turn every scrape
    into an LLM call.
    """
    text = (content or "").strip()
    if not text:
        return False, "no content"
    if len(text) < _WALL_MARKER_MAX_CHARS:
        lowered = text.lower()
        for marker in _WALL_MARKERS:
            if marker in lowered:
                return False, (
                    f"{len(text)} chars matching a wall/placeholder page ({marker!r})"
                )
    if len(text) < _MIN_SUBSTANTIVE_SCRAPE_CHARS:
        return False, (
            f"only {len(text)} chars, under the {_MIN_SUBSTANTIVE_SCRAPE_CHARS}-char "
            "substantive-content floor (navigation/boilerplate shell)"
        )
    return True, ""


def thin_content_banner(reason: str, engines_tried: Iterable[str]) -> str:
    """Prefix marking content that the ladder accepted only as a last resort.

    Downstream readers (extract, artifact-check, compiler) otherwise cannot tell
    a genuinely sparse page from one whose payload never loaded, and would read
    the silence as evidence the value does not exist.
    """
    tried = ", ".join(dict.fromkeys(e for e in engines_tried if e)) or "all engines"
    return (
        "[SCRAPE WARNING — INADEQUATE CONTENT] This fetch succeeded but returned "
        f"{reason}. Engines tried: {tried}. The page is most likely client-side "
        "rendered (its data loads via JavaScript after the HTML), paywalled, or behind "
        "a bot wall, so the data it displays to a human was NOT retrieved. Treat the "
        "text below as incomplete: the ABSENCE of a value here is NOT evidence that the "
        "value does not exist or was not published.\n\n"
    )


def _cache_payload(cached: str) -> tuple[str, str]:
    """``(content, error)`` for cache-served text, banner-marked when thin.

    A URL scraped twice in one run (main pass, then artifact retry) takes the
    cache path the second time. Without this the retry copy would arrive
    unmarked while the original carried the warning — 44875 hit exactly that.
    """
    adequate, reason = scrape_adequacy(cached)
    if adequate:
        return _truncate_text(cached, _MAX_SCRAPE_CHARS), ""
    banner = thin_content_banner(reason, ["cache (earlier attempt this run)"])
    return (
        banner + _truncate_text(cached, _MAX_SCRAPE_CHARS - len(banner)),
        f"inadequate content: {reason}",
    )


@dataclass(frozen=True)
class Cycle:
    cycle: int
    scrapes: list[Scrape]
    # ``report`` is this cycle's NEW findings only. Cycles used to re-emit the
    # whole accumulated report every time, which cost a full rewrite in output
    # tokens per cycle (44879 cycles 2 and 3 spent $0.057 and $0.063 to absorb
    # one ~4.5K page each) and let a rewrite silently drop an earlier claim
    # (the 44267 shape). ``cumulative_report`` is every cycle's findings joined
    # in order and is what the provider section, the salvage floor, and the next
    # cycle's prompt all read.
    report: str
    lacking_groups: list[str]
    cumulative_report: str = ""


def scraped_ok_urls(cycles: list[Cycle]) -> set[str]:
    """Normalised URLs whose page content was successfully scraped and fed to
    the extract stage in any cycle."""
    return {
        _norm_url(scrape.url)
        for cycle in cycles
        for scrape in cycle.scrapes
        if scrape.ok
    }


async def run_scrape_cycles(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    groups: list[RankedUrlGroup],
    max_cycles: int = DEFAULT_MAX_SCRAPE_CYCLES,
    model: str = DEFAULT_EXTRACT_MODEL,
    url_dates: dict[str, str] | None = None,
) -> list[Cycle]:
    if not groups or max_cycles < 1:
        return []

    cycles: list[Cycle] = []
    extract_refs: list[str] = []
    next_index = {group.group: 0 for group in groups}
    needed = {group.group for group in groups}
    report_parts: list[str] = []
    report = ""

    for cycle_no in range(1, max_cycles + 1):
        if MonetaryCostManager.would_breach_input_reserve(_EXPECTED_CYCLE_INPUT_TOKENS):
            logger.warning(
                "Stopping scrape cycles before cycle %d: another cycle would eat into "
                "the input tokens reserved for compile/forecast; keeping %d completed "
                "cycle(s).",
                cycle_no,
                len(cycles),
            )
            break
        targets = _cycle_targets(groups, needed, next_index, cycle_no)
        if not targets:
            break

        scrapes = await _scrape_targets(
            title=title,
            resolution_criteria=resolution_criteria,
            background=background,
            fine_print=fine_print,
            targets=targets,
            cycle_no=cycle_no,
        )
        try:
            delta, lack = await _extract_with_one_retry(
                cycle_no=cycle_no,
                title=title,
                resolution_criteria=resolution_criteria,
                background=background,
                fine_print=fine_print,
                groups=groups,
                cycles=[*cycles, Cycle(cycle_no, scrapes, "", [])],
                previous_report=report,
                # After cycle 1 the extractor is asked to fill only the
                # categories the previous cycle reported lacking; those are the
                # only ones this cycle fetched pages for.
                focus_groups=set(needed) if cycles else None,
                model=model,
                url_dates=url_dates,
            )
        except HardLimitExceededError:
            raise
        except Exception as exc:
            # Salvage: a completed cycle's report is already parsed and
            # validated, so a later cycle's extract failure must not discard
            # it (44773 incident: one malformed extract response threw away
            # the whole provider, and the search chain paid for a full
            # replacement pass). Only salvage past the quality floor — a thin
            # report should still fail the provider so the chain can try the
            # next one.
            if _has_salvageable_report(cycles, groups):
                logger.warning(
                    "Extract failed twice on cycle %d (%s: %s); salvaging the "
                    "validated report from cycle %d instead of discarding the "
                    "provider.",
                    cycle_no,
                    type(exc).__name__,
                    exc,
                    cycles[-1].cycle,
                )
                break
            raise

        valid_names = {group.group for group in groups}
        lack = [name for name in lack if name in valid_names]
        report = _append_cycle_report(report_parts, delta, cycle_no)
        cycles.append(
            Cycle(
                cycle=cycle_no,
                scrapes=scrapes,
                report=delta,
                lacking_groups=lack,
                cumulative_report=report,
            )
        )
        # Trace each cycle's own findings. Deltas make cycle-over-cycle drops
        # impossible by construction (nothing rewrites an earlier cycle), but
        # the per-cycle payload is still what makes each cycle's yield
        # auditable against its cost.
        trace_tool, trace_phase = source_ledger.current_context()
        extract_ref = research_trace.ref_doc(f"extract:{trace_tool}:{trace_phase}:{cycle_no}")
        research_trace.emit(
            "extract_report",
            f"{trace_tool} extract report — cycle {cycle_no}",
            delta,
            # Each cycle reads its own pages and the previous cycle's report;
            # the provider's section joins every cycle (mark_output below).
            reads=[research_trace.ref_page(scrape.url) for scrape in scrapes] + extract_refs[-1:],
            writes=[extract_ref],
            kind="model",
            meta={
                "chain": f"extract:{trace_tool}:{trace_phase}",
                "cycle": cycle_no,
                "lacking_groups": lack,
                "scraped_urls": [scrape.url for scrape in scrapes],
                "cumulative_chars": len(report),
            },
        )
        extract_refs.append(extract_ref)
        research_trace.mark_output(list(extract_refs))
        needed = set(lack)
        if not needed:
            break

    return cycles


def _append_cycle_report(parts: list[str], delta: str, cycle_no: int) -> str:
    """Append one cycle's new findings and return the cumulative report.

    Cycle 1's report is kept as-is (it carries the "# ... Scraped Research"
    heading the section renderer expects). Later cycles are appended under a
    cycle-labelled heading so provenance survives the join and nothing an
    earlier cycle established can be overwritten.
    """
    delta = str(delta or "").strip()
    if delta:
        if parts:
            parts.append(f"## Additional findings from scrape cycle {cycle_no}\n\n{delta}")
        else:
            parts.append(delta)
    return "\n\n".join(parts).strip()


async def _extract_with_one_retry(cycle_no: int, **extract_kwargs) -> tuple[str, list[str]]:
    """Run the extract call, retrying once on failure.

    Extract failures are usually stochastic output-formatting slips (e.g. an
    unescaped newline inside the JSON), so one fresh sample is cheap insurance
    before falling back to salvage. Budget refusals are never retried.
    """
    try:
        return await extract_scraped_research(**extract_kwargs)
    except HardLimitExceededError:
        raise
    except Exception as exc:
        logger.warning(
            "Extract failed on cycle %d (%s: %s); retrying the extract call once.",
            cycle_no,
            type(exc).__name__,
            exc,
        )
        return await extract_scraped_research(**extract_kwargs)


def _has_salvageable_report(
    cycles: list[Cycle], groups: list[RankedUrlGroup]
) -> bool:
    """Quality floor for salvaging after a failed extract.

    There must be a previous validated cycle whose report is substantive:
    non-empty and with at least one evidence group filled. Salvaging a thin
    report would count the provider as a success and suppress the search
    chain's fall-through to the next provider, which could do better.
    """
    if not cycles:
        return False
    last = cycles[-1]
    # Judged on the CUMULATIVE report: with additive cycles the last cycle's
    # own delta may legitimately be small, and salvaging must weigh everything
    # the provider actually established, not just its most recent increment.
    if not (last.cumulative_report or last.report).strip():
        return False
    return len(set(last.lacking_groups)) < len({group.group for group in groups})


async def extract_scraped_research(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    groups: list[RankedUrlGroup],
    cycles: list[Cycle],
    previous_report: str,
    model: str = DEFAULT_EXTRACT_MODEL,
    url_dates: dict[str, str] | None = None,
    focus_groups: set[str] | None = None,
) -> tuple[str, list[str]]:
    prompt = _build_extract_prompt(
        title=title,
        resolution_criteria=resolution_criteria,
        background=background,
        fine_print=fine_print,
        groups=groups,
        cycles=cycles,
        previous_report=previous_report,
        url_dates=url_dates,
        focus_groups=focus_groups,
    )
    response = await call_llm(
        prompt,
        model=model,
        temperature=0.1,
        _label="scrape-extract",
    )
    delta, raw_lack = _parse_extract_response(response)
    lack: list[str] = []
    if isinstance(raw_lack, list):
        for item in raw_lack:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("group") or item.get("name") or "").strip()
            else:
                name = ""
            if name and name not in lack:
                lack.append(name)
    # The caller appends this to the cumulative report, so an empty reply must
    # come back empty — returning ``previous_report`` here would duplicate the
    # whole accumulated report into the next cycle's slice.
    return delta, lack


_FENCED_BLOCK_PATTERN = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.DOTALL)


def _parse_extract_response(text: str) -> tuple[str, list]:
    """Split an extract response into (markdown report, lacking_groups list).

    The report is prose and is never parsed, so it cannot fail; only the small
    lacking_groups trailer is structured. This function NEVER raises on
    malformed model output (the 44773 incident: one unescaped newline inside
    the old report-inside-JSON format discarded a whole research branch).
    Resolution order:
    1. The LAST fenced code block that parses as a JSON object with a
       "lacking_groups" key — report is everything before that block.
    2. Legacy shape: the whole response is one JSON object with a "report"
       key (old prompt format, or a model regression).
    3. A bare (unfenced) trailing JSON object with "lacking_groups".
    4. No trailer found: the whole text is the report and lacking_groups is
       empty — cycles stop, and the downstream artifact-check still catches
       any genuinely missing artifact.
    """
    text = str(text or "")

    # 1. Last fenced block carrying lacking_groups.
    matches = list(_FENCED_BLOCK_PATTERN.finditer(text))
    for match in reversed(matches):
        parsed = _try_json_object(match.group(1))
        if parsed is not None and "lacking_groups" in parsed:
            report = (text[: match.start()] + text[match.end():]).strip()
            return report, parsed.get("lacking_groups") or []

    # 2. Legacy: whole response is the old JSON envelope.
    parsed = _try_json_object(text)
    if parsed is not None and "report" in parsed:
        return str(parsed.get("report", "")).strip(), parsed.get("lacking_groups") or []

    # 3. Bare trailing JSON object with lacking_groups.
    decoder = json.JSONDecoder()
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "lacking_groups" in value:
            return text[:index].strip(), value.get("lacking_groups") or []
        break  # the last {...} is something else (e.g. quoted data); stop scanning

    # 4. Prose only: keep everything, stop cycling.
    return text.strip(), []


def _try_json_object(text: str) -> dict | None:
    try:
        parsed = json.loads(str(text or "").strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _cycle_targets(
    groups: list[RankedUrlGroup],
    needed: set[str],
    next_index: dict[str, int],
    cycle_no: int,
) -> list[tuple[RankedUrlGroup, RankedUrl]]:
    targets: list[tuple[RankedUrlGroup, RankedUrl]] = []
    for group in groups:
        if group.group not in needed:
            continue
        index = next_index.get(group.group, 0)
        if index >= len(group.urls):
            continue
        targets.append((group, group.urls[index]))
        next_index[group.group] = index + 1

    # Backfill thin cycles. An extract call carries ~15-16K chars of fixed
    # overhead (the accumulated prior report is re-sent every cycle), so a cycle
    # holding one page pays full price for it: on 44879 cycles 2 and 3 each
    # scraped a single ~4.5K-char CISA alert and cost $0.057 / $0.063. Pulling
    # deeper URLs from the groups that still need evidence amortises that one
    # overhead across more pages instead of paying it per page — strictly more
    # coverage for the same LLM spend (it costs Firecrawl credits, which are
    # capped separately by FIRECRAWL_QUESTION_CREDIT_CAP).
    if len(targets) < _MIN_CYCLE_TARGETS:
        for group in groups:
            if group.group not in needed:
                continue
            while len(targets) < _MIN_CYCLE_TARGETS:
                index = next_index.get(group.group, 0)
                if index >= len(group.urls):
                    break
                targets.append((group, group.urls[index]))
                next_index[group.group] = index + 1
            if len(targets) >= _MIN_CYCLE_TARGETS:
                break
    return targets


async def _scrape_targets(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    targets: list[tuple[RankedUrlGroup, RankedUrl]],
    cycle_no: int,
) -> list[Scrape]:
    async def scrape_one(group: RankedUrlGroup, item: RankedUrl) -> Scrape:
        query = _crawl_query(
            title=title,
            resolution_criteria=resolution_criteria,
            background=background,
            fine_print=fine_print,
            group=group,
            item=item,
        )

        def _finish(scrape: Scrape, engine: str) -> Scrape:
            source_ledger.record_url_event(
                scrape.url,
                source_ledger.ROLE_SCRAPED,
                engine=engine,
                ok=scrape.ok,
                error=scrape.error,
                chars=len(scrape.content),
                round_label=f"cycle {cycle_no}",
                detail=group.group,
            )
            tool, phase = source_ledger.current_context()
            # ok + an error string means the ladder exhausted itself and fell
            # back to banner-marked thin content; that is neither a clean "ok"
            # nor a "failed", and the trace should not flatten it into either.
            if scrape.ok:
                status = "thin" if scrape.error else "ok"
            else:
                status = "failed"
            research_trace.emit(
                "scrape",
                scrape.url,
                scrape.content if scrape.ok else (scrape.error or "(no content)"),
                status=status,
                error=scrape.error,
                meta={
                    "engine": engine,
                    "group": group.group,
                    "cycle": cycle_no,
                    "phase": f"{tool} / {phase}",
                    "raw_chars": scrape.raw_chars or len(scrape.content),
                    "truncated": scrape.content.endswith("[Truncated.]"),
                },
                reads=[research_trace.ref_url(scrape.url)],
                writes=[research_trace.ref_page(scrape.url)] if scrape.ok else [],
                cuts=[{"kept": len(scrape.content), "of": scrape.raw_chars,
                       "reason": "scrape cap (40,000 chars)"}]
                if scrape.ok and scrape.content.endswith("[Truncated.]") else None,
                kind="fetch",
            )
            return scrape

        adapter = None
        try:
            from bot.scrapers.adapters import find_adapter

            adapter = find_adapter(item.url)
        except Exception as exc:
            print(f"[adapter] registry unavailable for {item.url}: {type(exc).__name__}: {exc}")

        if adapter is not None:
            from bot.scrapers.crawler.crawl import claim_scrape_url, get_cached_scrape_content, record_scrape_content

            duplicate_payload = claim_scrape_url(item.url)
            if duplicate_payload is not None:
                cached = get_cached_scrape_content(item.url)
                if cached:
                    cached_content, cached_error = _cache_payload(cached)
                    return _finish(
                        Scrape(
                            cycle=0,
                            group=group.group,
                            group_purpose=group.group_purpose,
                            url=item.url,
                            purpose=item.purpose,
                            ok=True,
                            content=cached_content,
                            error=cached_error,
                        ),
                        engine=source_ledger.ENGINE_CACHE,
                    )
                return _finish(
                    Scrape(
                        cycle=0,
                        group=group.group,
                        group_purpose=group.group_purpose,
                        url=item.url,
                        purpose=item.purpose,
                        ok=False,
                        error="skipped duplicate: URL has already been scraped in this process",
                    ),
                    engine=source_ledger.ENGINE_SKIPPED_DUPLICATE,
                )
            try:
                print(f"[adapter] {adapter.name} handling {item.url}")
                result = await adapter.extract(item.url, query=query)
                record_scrape_content(item.url, result.content)
                return _finish(
                    Scrape(
                        cycle=0,
                        group=group.group,
                        group_purpose=group.group_purpose,
                        url=item.url,
                        purpose=item.purpose,
                        ok=bool(result.content.strip()),
                        content=_truncate_text(result.content, _MAX_SCRAPE_CHARS),
                        raw_chars=len(result.content or ""),
                        error="" if result.content.strip() else f"{adapter.name} returned no content.",
                    ),
                    engine=f"adapter:{adapter.name}",
                )
            except Exception as exc:
                return _finish(
                    Scrape(
                        cycle=0,
                        group=group.group,
                        group_purpose=group.group_purpose,
                        url=item.url,
                        purpose=item.purpose,
                        ok=False,
                        error=f"adapter failed: {type(exc).__name__}: {exc}",
                    ),
                    engine=f"adapter:{adapter.name}",
                )

        from bot.scrapers.crawler.crawl import (
            basic_crawl_markdown,
            claim_scrape_url,
            get_cached_scrape_content,
            record_scrape_content,
        )

        duplicate_payload = claim_scrape_url(item.url)
        if duplicate_payload is not None:
            cached = get_cached_scrape_content(item.url)
            if cached:
                cached_content, cached_error = _cache_payload(cached)
                return _finish(
                    Scrape(
                        cycle=0,
                        group=group.group,
                        group_purpose=group.group_purpose,
                        url=item.url,
                        purpose=item.purpose,
                        ok=True,
                        content=cached_content,
                        error=cached_error,
                    ),
                    engine=source_ledger.ENGINE_CACHE,
                )
            return _finish(
                Scrape(
                    cycle=0,
                    group=group.group,
                    group_purpose=group.group_purpose,
                    url=item.url,
                    purpose=item.purpose,
                    ok=False,
                    error="skipped duplicate: URL has already been scraped in this process",
                ),
                engine=source_ledger.ENGINE_SKIPPED_DUPLICATE,
            )

        # Firecrawl first (main-content markdown, JS/PDF rendering, bot-wall
        # bypass); Crawl4AI below stays as the free fallback. The shared
        # per-question credit budget in research.firecrawl_scrape hard-caps
        # spend and reserves first claim for the resolution-path URLs.
        # Best inadequate attempt seen so far. Adequacy decides whether to keep
        # escalating; it never decides whether to keep the text. If every rung
        # comes back thin we still return the longest thing we got, banner-
        # marked, rather than discarding it as a failure.
        best_thin_content = ""
        best_thin_engine = ""
        best_thin_reason = ""
        engines_tried: list[str] = []

        def _note_thin(content: str, engine: str, reason: str) -> None:
            nonlocal best_thin_content, best_thin_engine, best_thin_reason
            engines_tried.append(engine)
            if content.strip() and len(content.strip()) > len(best_thin_content):
                best_thin_content = content.strip()
                best_thin_engine = engine
                best_thin_reason = reason

        firecrawl_error = ""
        if ENABLE_FIRECRAWL_GENERAL_SCRAPE:
            from bot.scrapers.firecrawl_scrape import (
                GENERAL_MAX_AGE_MS,
                FirecrawlBudgetExceededError,
                FirecrawlCreditError,
                firecrawl_exhausted,
                firecrawl_scrape_markdown,
                mark_firecrawl_exhausted,
            )

            if not firecrawl_exhausted():
                try:
                    content = await firecrawl_scrape_markdown(
                        item.url,
                        _FIRECRAWL_GENERAL_TIMEOUT_SECONDS,
                        max_age_ms=GENERAL_MAX_AGE_MS,
                    )
                    record_scrape_content(item.url, content)
                    adequate, reason = scrape_adequacy(content)
                    if adequate:
                        return _finish(
                            Scrape(
                                cycle=0,
                                group=group.group,
                                group_purpose=group.group_purpose,
                                url=item.url,
                                purpose=item.purpose,
                                ok=True,
                                content=_truncate_text(content, _MAX_SCRAPE_CHARS),
                                raw_chars=len(content or ""),
                            ),
                            engine=source_ledger.ENGINE_FIRECRAWL,
                        )
                    _note_thin(content, source_ledger.ENGINE_FIRECRAWL, reason)
                    firecrawl_error = f"Firecrawl returned {reason}."
                except FirecrawlCreditError as exc:
                    mark_firecrawl_exhausted()
                    firecrawl_error = f"{exc}"
                    logger.warning(
                        "Firecrawl exhausted (credits/auth) on %s — falling back "
                        "to Crawl4AI for the rest of this run: %s",
                        item.url,
                        exc,
                    )
                except FirecrawlBudgetExceededError as exc:
                    firecrawl_error = f"{exc}"
                    logger.info("%s", exc)
                except Exception as exc:
                    firecrawl_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Firecrawl scrape failed for %s: %s — falling back to Crawl4AI.",
                        item.url,
                        firecrawl_error,
                    )

        crawl_error = ""
        try:
            content = await basic_crawl_markdown(item.url)
            record_scrape_content(item.url, content)
            adequate, reason = scrape_adequacy(content)
            if adequate:
                return _finish(
                    Scrape(
                        cycle=0,
                        group=group.group,
                        group_purpose=group.group_purpose,
                        url=item.url,
                        purpose=item.purpose,
                        ok=True,
                        content=_truncate_text(content, _MAX_SCRAPE_CHARS),
                        raw_chars=len(content or ""),
                    ),
                    engine=source_ledger.ENGINE_CRAWL4AI_BASIC,
                )
            _note_thin(content, source_ledger.ENGINE_CRAWL4AI_BASIC, reason)
            crawl_error = f"Crawl4AI returned {reason}."
        except HardLimitExceededError:
            raise
        except Exception as exc:
            crawl_error = f"{type(exc).__name__}: {exc}"

        # Render-wait rung: only reached when the ordinary engines came back
        # inadequate, which is the signature of a client-side-rendered page.
        # Costs one Firecrawl credit and ~8s, so it is never on the fast path —
        # and the per-question credit cap makes the worst case bounded (44875
        # spent 1 of 25 credits while its resolution source failed five times).
        render_error = ""
        if ENABLE_FIRECRAWL_GENERAL_SCRAPE and best_thin_content:
            from bot.scrapers.firecrawl_scrape import (
                FirecrawlBudgetExceededError,
                FirecrawlCreditError,
                firecrawl_exhausted,
                firecrawl_scrape_markdown,
                mark_firecrawl_exhausted,
            )

            if not firecrawl_exhausted():
                try:
                    content = await firecrawl_scrape_markdown(
                        item.url,
                        _FIRECRAWL_RENDER_TIMEOUT_SECONDS,
                        max_age_ms=0,
                        wait_for_ms=_RENDER_WAIT_MS,
                        only_main_content=False,
                    )
                    record_scrape_content(item.url, content)
                    adequate, reason = scrape_adequacy(content)
                    if adequate:
                        return _finish(
                            Scrape(
                                cycle=0,
                                group=group.group,
                                group_purpose=group.group_purpose,
                                url=item.url,
                                purpose=item.purpose,
                                ok=True,
                                content=_truncate_text(content, _MAX_SCRAPE_CHARS),
                                raw_chars=len(content or ""),
                            ),
                            engine=_ENGINE_FIRECRAWL_RENDER,
                        )
                    _note_thin(content, _ENGINE_FIRECRAWL_RENDER, reason)
                    render_error = f"Firecrawl render-wait returned {reason}."
                except FirecrawlCreditError as exc:
                    mark_firecrawl_exhausted()
                    render_error = f"{exc}"
                except FirecrawlBudgetExceededError as exc:
                    render_error = f"{exc}"
                except HardLimitExceededError:
                    raise
                except Exception as exc:
                    render_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Firecrawl render-wait scrape failed for %s: %s", item.url, render_error
                    )

        # Wayback snapshot fallback: pages the live web will not serve
        # (paywalls, bot walls — the 44773 NYT case, where one failed live
        # fetch tombstoned the URL for the whole run). Free (archive.org),
        # never for market/quote pages (stale prices, the 44267 class), and
        # the capture date is stamped in-band by snapshot_fallback_text.
        try:
            from bot.scrapers.adapters import Wayback as wayback_module

            snapshot_text = await wayback_module.snapshot_fallback_text(item.url)
        except Exception as exc:
            logger.warning(
                "Wayback snapshot fallback failed for %s: %s", item.url, exc
            )
            snapshot_text = ""
        wayback_adequate, wayback_reason = scrape_adequacy(snapshot_text)
        if wayback_adequate:
            record_scrape_content(item.url, snapshot_text)
            return _finish(
                Scrape(
                    cycle=0,
                    group=group.group,
                    group_purpose=group.group_purpose,
                    url=item.url,
                    purpose=item.purpose,
                    ok=True,
                    content=_truncate_text(snapshot_text, _MAX_SCRAPE_CHARS),
                    raw_chars=len(snapshot_text or ""),
                ),
                engine="wayback-snapshot",
            )
        if snapshot_text.strip():
            _note_thin(snapshot_text, "wayback-snapshot", wayback_reason)

        # Every rung came back thin. Returning the best attempt banner-marked
        # beats dropping it: the text is still weak evidence, and the banner is
        # what tells the artifact-check and the compiler that this page's
        # silence is a retrieval failure rather than a published absence.
        if best_thin_content:
            record_scrape_content(item.url, best_thin_content)
            banner = thin_content_banner(best_thin_reason, engines_tried)
            return _finish(
                Scrape(
                    cycle=0,
                    group=group.group,
                    group_purpose=group.group_purpose,
                    url=item.url,
                    purpose=item.purpose,
                    ok=True,
                    content=banner + _truncate_text(
                        best_thin_content, _MAX_SCRAPE_CHARS - len(banner)
                    ),
                    error=f"inadequate content: {best_thin_reason}",
                ),
                engine=f"{best_thin_engine}+thin",
            )

        combined_error = "; ".join(
            part
            for part in (
                f"firecrawl: {firecrawl_error}" if firecrawl_error else "",
                f"crawl4ai: {crawl_error}" if crawl_error else "",
                f"firecrawl-render-wait: {render_error}" if render_error else "",
            )
            if part
        )
        return _finish(
            Scrape(
                cycle=0,
                group=group.group,
                group_purpose=group.group_purpose,
                url=item.url,
                purpose=item.purpose,
                ok=False,
                error=combined_error or "scrape returned no content",
            ),
            engine=source_ledger.ENGINE_CRAWL4AI_BASIC,
        )

    scrapes = await _limited_gather(
        [scrape_one(group, item) for group, item in targets],
        limit=2,
    )
    return [
        Scrape(
            cycle=cycle_no,
            group=scrape.group,
            group_purpose=scrape.group_purpose,
            url=scrape.url,
            purpose=scrape.purpose,
            ok=scrape.ok,
            content=scrape.content,
            error=scrape.error,
        )
        for scrape in scrapes
    ]


def _build_extract_prompt(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    groups: list[RankedUrlGroup],
    cycles: list[Cycle],
    previous_report: str,
    url_dates: dict[str, str] | None = None,
    focus_groups: set[str] | None = None,
) -> str:
    # After cycle 1 only the categories the previous cycle reported lacking were
    # fetched for, so those are the only ones worth listing: a settled category
    # cannot be improved by pages that were never scraped for it.
    listed_groups = [
        group for group in groups
        if focus_groups is None or group.group in focus_groups
    ] or groups
    group_lines = "\n".join(
        f"- {group.group}: {group.group_purpose}"
        for group in listed_groups
    )
    prompt_cycles = cycles[-1:] if previous_report else cycles
    scrape_text = _format_scrapes_for_prompt(prompt_cycles, url_dates)
    scrape_text = _truncate_text(scrape_text, _MAX_EXTRACT_INPUT_CHARS)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if previous_report:
        report_task = (
            "Report ONLY what the NEW scrape packets below add. The previous "
            "compiled report is shown for context so you do not repeat it — it "
            "is already kept in full and will be joined with your output "
            "automatically.\n"
            "- Do NOT restate, rewrite, reorganise, or re-quote anything already "
            "in the previous report. Do not reproduce its heading.\n"
            "- Write only \"## \" category sections for categories that the new "
            "packets actually add something to.\n"
            "- If a new packet CONTRADICTS or CORRECTS something in the previous "
            "report, say so explicitly and name the superseded claim — that is "
            "new information and must be reported.\n"
            "- If the new packets add nothing usable, write exactly: "
            "\"No new usable findings from this cycle.\""
        )
        part_one_shape = (
            'ONLY the new findings, as "## " category sections. No "# " '
            "heading — this text is appended to the previous report."
        )
    else:
        report_task = (
            "Compile the useful facts from all scrape packets into a clean "
            "research report by category."
        )
        part_one_shape = (
            'The research report as plain markdown. Start it with a "# " '
            'heading and organise it with "## " category sections.'
        )

    return f"""
You are a research assistant compiling scraped web evidence for a forecasting question.
Do not make a prediction, estimate probabilities, or recommend an answer.

Today's date is {today}.

Forecasting question:
{title}

Resolution criteria:
{resolution_criteria or "Not provided."}

Background:
{background or "Not provided."}

Fine print:
{fine_print or "Not provided."}

Research categories you may use when requesting more scraping:
{group_lines or "- None"}

Task:
- {report_task}
- State the actual extracted contents: facts, numbers, dates, names, rules, and quoted/near-quoted source claims.

Ground every statement in the scraped content — never fabricate or add information:
- Record ONLY what the scraped page content actually states. Do not infer, assume, complete, or "fill in" any fact, value, date, or attribution that is not literally present in that page's content.
- The "URL purpose" lines are pre-scrape guesses, not evidence. Never lift a fact, number, or date from them.
- Specifically for dates: never attach a year (or any date) that does not appear in the source itself. To resolve the year of an undated figure, use ONLY the page's "Source publish date" line (shown with each scrape packet) or an explicit in-text date — a figure published in July 2025 describes July 2025, regardless of what the question asks about. If a figure has a month but no year and the Source publish date is "not provided", record it exactly as stated and add "(year not stated in source)". Do NOT assume it refers to {today}'s year or the year the question asks about.
- If you are tempted to write something the source does not actually say, omit it instead. Missing information must be reported as missing, not inferred.
- Do not write vague placeholders such as "this link contains information", "the article discusses", or "can be found at this URL" unless you also state the concrete information.
- Use any relevant information from a URL, even if it goes beyond that URL's intended purpose.
- Preserve source URLs next to important facts.
- Include dates, vote counts, thresholds, named stakeholders, procedural rules, and concrete evidence when present.
- Note failed or thin scrapes only when they affect coverage.
- Do not forecast or state whether the event will happen.
- If a category still lacks enough useful information, request more scraping for that category.
- Pick lacking categories only from the exact category names listed above.
- If no more scraping is needed, use an empty lacking_groups list.

Output format — exactly two parts, in this order:

PART 1: {part_one_shape} Write it directly — do NOT wrap it in
JSON, quotes, or a code fence.

PART 2: After the report, one fenced JSON block containing ONLY the lacking
categories, in exactly this shape:

```json
{{"lacking_groups": [{{"group": "Exact category name", "reason": "What is still missing or unusable."}}]}}
```

If no more scraping is needed, end with:

```json
{{"lacking_groups": []}}
```

Previous compiled report:
{previous_report or "No previous report yet."}

New scrape packets:
{scrape_text or "No scrape packets available."}
""".strip()


def _norm_url(url: str) -> str:
    """Normalise a URL for date-map matching (drop trailing slash, fragment, case)."""
    url = (url or "").strip()
    url = url.split("#", 1)[0]
    return url.rstrip("/").lower()


def build_url_date_map(url_date_pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Build a normalised {url: publish_date} map from a provider's search results.

    Only results that actually carry a date are included; callers pass whatever
    their organic-result objects expose (e.g. SerpAPI ``date``, Tavily
    ``published_date``) so the extractor can date a page from its source instead
    of inferring the year.
    """
    out: dict[str, str] = {}
    for url, date in url_date_pairs:
        date = (date or "").strip()
        if date:
            out[_norm_url(url)] = date
    return out


def _url_date_lookup(url_dates: dict[str, str] | None, url: str) -> str:
    if url_dates:
        provider_date = url_dates.get(url) or url_dates.get(_norm_url(url), "")
        if provider_date:
            return provider_date
    # X/Twitter status URLs: the snowflake ID encodes the authoritative post
    # date; scraped X pages show year-less dates that otherwise get misread.
    decoded = tweet_url_date(url)
    if decoded:
        return f"{decoded} (decoded from the tweet ID; authoritative post date)"
    return ""


def _format_scrapes_for_prompt(
    cycles: list[Cycle], url_dates: dict[str, str] | None = None
) -> str:
    parts: list[str] = []
    for cycle in cycles:
        parts.append(f"## Cycle {cycle.cycle}")
        for scrape in cycle.scrapes:
            publish_date = _url_date_lookup(url_dates, scrape.url)
            parts.extend(
                [
                    f"### Group: {scrape.group}",
                    f"Group purpose: {scrape.group_purpose}",
                    f"URL: {scrape.url}",
                    # Real publish date from the search provider's metadata, when
                    # available — this is the authoritative way to date a page's
                    # facts (e.g. an article that says "45.2 in July" with no year).
                    f"Source publish date (from search metadata; use it to date this "
                    f"page's facts): {publish_date or 'not provided'}",
                    # `purpose` is the ranker's PRE-SCRAPE guess about what this page
                    # might contain, written from the search snippet alone — not
                    # verified against the page. Label it so the extractor never
                    # promotes its guesses (e.g. an inferred year) to stated facts.
                    f"URL purpose (pre-scrape guess only — NOT evidence; do not extract "
                    f"any fact, value, or date from this line): {scrape.purpose}",
                    f"Scrape status: {'ok' if scrape.ok else 'failed'}",
                ]
            )
            if scrape.error:
                parts.append(f"Error: {scrape.error}")
            if not scrape.ok:
                parts.append("Content omitted.")
            elif scrape.content:
                content = _compact_scrape_content_for_prompt(scrape.content)
                if content:
                    parts.extend(["Extracted content:", content])
                else:
                    parts.append("No usable content extracted.")
            parts.append("")
    return "\n".join(parts).strip()


def _compact_scrape_content_for_prompt(content: str) -> str:
    content = str(content or "").strip()
    if not content:
        return ""

    if content.startswith("Crawl4AI duplicate scrape skipped"):
        return ""

    return content


def _crawl_query(
    title: str,
    resolution_criteria: str,
    background: str,
    fine_print: str,
    group: RankedUrlGroup,
    item: RankedUrl,
) -> str:
    return "\n".join(
        part
        for part in [
            f"Forecasting question: {title}",
            f"Background: {background}" if background else "",
            f"Resolution criteria: {resolution_criteria}" if resolution_criteria else "",
            f"Fine print: {fine_print}" if fine_print else "",
            f"Research category: {group.group}",
            f"Category purpose: {group.group_purpose}",
            f"URL purpose hint: {item.purpose}",
            "Find concrete facts, dates, numbers, rules, and source text relevant to the forecast question. Do not forecast.",
        ]
        if part
    )
