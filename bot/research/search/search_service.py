"""The one way a research tool searches the web on its own.

Web search's main pass and the focused retry plan their own query lists and
call web_search directly. A tool that searches a few queries at a time while
it works (the history builder) calls ``search`` here instead, which:

  - uses the same engine chain (SerpAPI -> Tavily -> Firecrawl, whichever are
    enabled; an engine out of credits is skipped for the rest of the run);
  - never repeats a search the question has already run, by any tool: a
    near-duplicate query gets the earlier search's results back, free;
  - counts new searches against the tool's own budget;
  - lists what it found in the source ledger, like every other search.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

from bot import config
from bot.research.search import web_search
from bot.research.search.search_common import SearchEngine, SearchHit, _normalise_query, dedupe_hits
from bot.trace import research_trace, source_ledger

_QUERY_FILLER = frozenset(
    "the a an of for and or in on to by with from site de da ve ile mi bir icin".split())
SAME_QUERY_OVERLAP = 0.6
_YEAR = re.compile(r"(19|20)\d\d")


def enabled_engines() -> list[SearchEngine]:
    """The search engines switched on, in fallback order."""
    return [
        web_search.ENGINES[label]
        for label, on in (
            ("SerpAPI Google", config.ENABLE_SERPAPI_RESEARCH),
            ("Tavily Search", config.ENABLE_TAVILY_RESEARCH),
            ("Firecrawl Search", config.ENABLE_FIRECRAWL_RESEARCH),
        )
        if on
    ]


def query_words(query: str) -> set[str]:
    text = unicodedata.normalize("NFKD", query.lower()).encode("ascii", "ignore").decode()
    text = re.sub(r"site:\S+", " ", text)
    return {w for w in re.findall(r"[a-z0-9]+", text)
            if len(w) > 1 and w not in _QUERY_FILLER and not _YEAR.fullmatch(w)}


def query_numbers(query: str) -> set[str]:
    """Numbers other than years: a trailing "2026" separates nothing."""
    return {n for n in re.findall(r"\d+", query) if not _YEAR.fullmatch(n)}


def same_query(a: str, b: str, overlap: float = SAME_QUERY_OVERLAP) -> bool:
    """Near-duplicate searches: most meaningful words shared AND the same
    numbers. Dates are what tell "report for 3-5 November" from "report for
    10-12 November" (word overlap 0.71) -- merging those loses the one that
    mattered (Q46606) -- so differing numbers always keep queries apart."""
    wa, wb = query_words(a), query_words(b)
    if not wa or not wb or query_numbers(a) != query_numbers(b):
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / len(wa | wb) >= overlap


class SearchBudget:
    """How many new searches one tool may still run on this question."""

    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0

    @property
    def left(self) -> int:
        return max(0, self.cap - self.used)


@dataclass
class SearchOutcome:
    ran: list[str] = field(default_factory=list)
    hits: list[SearchHit] = field(default_factory=list)
    # (query asked, the earlier search it repeats, that search's results)
    reused: list[tuple[str, str, list[SearchHit]]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)   # over the budget
    engine: str = ""
    failures: list[str] = field(default_factory=list)


async def search(queries: list[str], *, budget: SearchBudget, reason: str) -> SearchOutcome:
    """Run the queries not yet searched on this question, within ``budget``.

    ``reason`` names the caller in the ledger and trace ("history builder").
    """
    outcome = SearchOutcome()
    earlier_queries = list(dict.fromkeys(q for _, _, q in source_ledger.get_queries()))
    earlier_hits = source_ledger.get_search_hits()
    for raw in queries:
        query = _normalise_query(raw)
        if not query or any(same_query(query, kept) for kept in outcome.ran):
            continue
        earlier = next((q for q in earlier_queries if same_query(query, q)), None)
        if earlier is not None:
            outcome.reused.append((query, earlier, [h for h in earlier_hits if h.query == earlier]))
        elif len(outcome.ran) < budget.left:
            outcome.ran.append(query)
        else:
            outcome.refused.append(query)
    if outcome.ran:
        engine, hits, outcome.failures = await web_search.search_with_fallback(
            enabled_engines(), outcome.ran, reason=reason)
        if engine is not None:
            # Only searches that ran count: a failed search cost nothing.
            budget.used += len(outcome.ran)
            outcome.engine = engine.label
            outcome.hits = dedupe_hits(hits)
            web_search.record_candidates(outcome.hits)
            research_trace.emit(
                "search_results",
                f"{engine.label} results for the {reason} (per-query rank order)",
                research_trace.format_search_results(outcome.hits),
                meta={"queries": outcome.ran, "result_count": len(outcome.hits), "engine": engine.label,
                      "reused": [q for q, _, _ in outcome.reused]},
                reads=research_trace.feeds(), writes=[research_trace.ref_url(h.url) for h in outcome.hits],
                kind="search",
            )
    return outcome


def format_outcome(outcome: SearchOutcome, *, per_query: int = 8, snippet_chars: int = 220) -> str:
    """A search's results as text for a model: per query, title, address,
    date and the start of the engine's snippet."""

    def block(heading: str, hits: list[SearchHit]) -> str:
        lines = [heading]
        for hit in hits[:per_query]:
            date = f" ({hit.date})" if hit.date else ""
            lines.append(f"- {hit.title.strip() or '(no title)'}{date}\n  {hit.url}\n  "
                         f"{' '.join(hit.snippet.split())[:snippet_chars]}")
        if not hits:
            lines.append("- (no results)")
        return "\n".join(lines)

    parts = []
    for query in outcome.ran:
        if outcome.engine:
            parts.append(block(f"## {query}", [h for h in outcome.hits if h.query == query]))
        else:
            parts.append(f"## {query}\nsearch failed: {'; '.join(outcome.failures) or 'no engine enabled'}")
    for query, earlier, hits in outcome.reused:
        parts.append(block(f"## {query}\n(already searched this question as \"{earlier}\"; "
                           f"its results, at no cost to your budget)", hits))
    for query in outcome.refused:
        parts.append(f"## {query}\nnot run: your search budget is spent")
    return "\n\n".join(parts) or "no queries given"
