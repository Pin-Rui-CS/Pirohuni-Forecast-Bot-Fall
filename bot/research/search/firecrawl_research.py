"""Firecrawl Search: one search engine of the web-search step (see web_search)."""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx

from bot.config import FIRECRAWL_API_KEY, FIRECRAWL_SEARCH_TBS
from bot.research.search.search_common import (
    SearchEngine,
    SearchHit,
    _limited_gather,
    dedupe_hits,
)


logger = logging.getLogger(__name__)

DEFAULT_FIRECRAWL_TOTAL_RESULTS_PER_QUERY = 10
DEFAULT_FIRECRAWL_SEARCH_SOURCES = ("web", "news")
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
# Firecrawl bills 2 credits per 10 results returned, rounded up.
_FIRECRAWL_CREDITS_PER_RESULT_BLOCK = 2
_FIRECRAWL_RESULTS_PER_BLOCK = 10


async def search_firecrawl(queries: list[str], reason: str = "firecrawl research") -> list[SearchHit]:
    return await fetch_firecrawl_search_results(
        queries=queries, tbs=_search_tbs(), reason=reason or "firecrawl research"
    )


def _search_tbs() -> str:
    return str(FIRECRAWL_SEARCH_TBS or "").strip()


async def fetch_firecrawl_search_results(
    queries: list[str],
    total_results_per_query: int = DEFAULT_FIRECRAWL_TOTAL_RESULTS_PER_QUERY,
    sources: tuple[str, ...] | list[str] | None = None,
    tbs: str = "",
    api_key: str | None = None,
    reason: str = "firecrawl research",
) -> list[SearchHit]:
    """Fetch and deduplicate search results from Firecrawl without scraping content."""
    api_key = api_key or FIRECRAWL_API_KEY
    if not api_key:
        raise ValueError("Missing FIRECRAWL_API_KEY for Firecrawl search research.")

    firecrawl_sources = tuple(sources or DEFAULT_FIRECRAWL_SEARCH_SOURCES)
    per_source_limit = _per_source_limit(total_results_per_query, firecrawl_sources)
    # Debug accounting: one Firecrawl /v2/search request fires per query. Log the
    # batch size, why it ran, and an upper-bound credit estimate so credit burn
    # can be traced back to a question and a trigger (main pass vs. retry).
    requested_per_call = per_source_limit * len(firecrawl_sources)
    estimated_credits = len(queries) * _estimate_firecrawl_credits(requested_per_call)
    logger.info(
        "[firecrawl] firing %d search call(s) | reason=%s | sources=%s | "
        "results/call≈%d | est. credits≤%d",
        len(queries),
        reason,
        ",".join(firecrawl_sources),
        requested_per_call,
        estimated_credits,
    )
    async with httpx.AsyncClient(timeout=45) as client:
        responses = await _gather_firecrawl_queries(
            client=client,
            queries=queries,
            api_key=api_key,
            per_source_limit=per_source_limit,
            sources=firecrawl_sources,
            tbs=tbs,
        )

    return dedupe_hits(
        result
        for query, payload in responses
        for result in _parse_firecrawl_results(query, payload, firecrawl_sources)
    )


async def _gather_firecrawl_queries(
    client: httpx.AsyncClient,
    queries: list[str],
    api_key: str,
    per_source_limit: int,
    sources: tuple[str, ...],
    tbs: str,
) -> list[tuple[str, dict[str, Any]]]:
    async def fetch(query: str) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "query": query,
            "sources": list(sources),
            "limit": per_source_limit,
        }
        if tbs:
            payload["tbs"] = tbs

        response = await client.post(
            FIRECRAWL_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise ValueError(f"Firecrawl response for query {query!r} was not a JSON object.")
        if response_payload.get("success") is False:
            error = response_payload.get("error") or response_payload.get("message") or response_payload
            raise ValueError(f"Firecrawl error for query {query!r}: {error}")
        return query, response_payload

    return await _limited_gather([fetch(query) for query in queries], limit=3)


def _parse_firecrawl_results(
    query: str,
    payload: dict[str, Any],
    sources: tuple[str, ...],
) -> list[SearchHit]:
    data = payload.get("data", payload)
    parsed: list[SearchHit] = []
    if isinstance(data, list):
        source_items = [("web", data)]
    elif isinstance(data, dict):
        source_items = [
            (source, data.get(source, []))
            for source in sources
        ]
    else:
        return []

    for source, raw_results in source_items:
        if not isinstance(raw_results, list):
            continue
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url", "")).strip()
            title = str(raw.get("title", "")).strip()
            if not url or not title:
                continue
            parsed.append(
                SearchHit(
                    title=title,
                    url=url,
                    snippet=str(raw.get("description") or raw.get("snippet") or "").strip(),
                    date=str(raw.get("date", "")).strip(),
                    query=query,
                    extras=(
                        ("Source", source or "Not provided."),
                        ("Category", str(raw.get("category", "")).strip() or "Not provided."),
                    ),
                )
            )
    return parsed


def _per_source_limit(total_results_per_query: int, sources: tuple[str, ...]) -> int:
    total = max(1, int(total_results_per_query))
    return max(1, math.ceil(total / max(1, len(sources))))


def _estimate_firecrawl_credits(results_per_call: int) -> int:
    """Upper-bound credits for one search call (2 credits per 10 results, rounded up)."""
    blocks = math.ceil(max(1, results_per_call) / _FIRECRAWL_RESULTS_PER_BLOCK)
    return blocks * _FIRECRAWL_CREDITS_PER_RESULT_BLOCK


ENGINE = SearchEngine(
    label="Firecrawl Search",
    slug="firecrawl",
    report_name="Firecrawl",
    search=search_firecrawl,
    settings=lambda: [
        f"Firecrawl sources: {', '.join(DEFAULT_FIRECRAWL_SEARCH_SOURCES)}",
        f"Firecrawl tbs filter: {_search_tbs() or 'None'}",
    ],
    # Firecrawl descriptions can be near-whole pages (44879: ~4,300 chars per
    # result on the retry, 66K for the block), so the ranking call digests them.
    digest=True,
)
