"""Tavily: one search engine of the web-search step (see web_search)."""

from __future__ import annotations

from typing import Any

import httpx

from bot.config import TAVILY_API_KEY, TAVILY_SEARCH_DEPTH
from bot.research.search.search_common import (
    SearchEngine,
    SearchHit,
    _limited_gather,
    dedupe_hits,
)


DEFAULT_TAVILY_MAX_RESULTS_PER_QUERY = 10
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


async def search_tavily(queries: list[str], reason: str = "") -> list[SearchHit]:
    return await fetch_tavily_search_results(queries=queries, search_depth=_search_depth())


def _search_depth() -> str:
    return str(TAVILY_SEARCH_DEPTH or "").strip()


async def fetch_tavily_search_results(
    queries: list[str],
    max_results_per_query: int = DEFAULT_TAVILY_MAX_RESULTS_PER_QUERY,
    search_depth: str = "basic",
    api_key: str | None = None,
) -> list[SearchHit]:
    """Fetch and deduplicate search results from Tavily without scraping content."""
    api_key = api_key or TAVILY_API_KEY
    if not api_key:
        raise ValueError("Missing TAVILY_API_KEY for Tavily search research.")

    max_results = max(1, min(20, int(max_results_per_query)))
    async with httpx.AsyncClient(timeout=45) as client:
        responses = await _gather_tavily_queries(
            client=client,
            queries=queries,
            api_key=api_key,
            max_results=max_results,
            search_depth=search_depth,
        )

    return dedupe_hits(
        result
        for query, payload in responses
        for result in _parse_tavily_results(query, payload)
    )


async def _gather_tavily_queries(
    client: httpx.AsyncClient,
    queries: list[str],
    api_key: str,
    max_results: int,
    search_depth: str,
) -> list[tuple[str, dict[str, Any]]]:
    async def fetch(query: str) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": search_depth or "basic",
            "topic": "general",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        response = await client.post(
            TAVILY_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        response_payload = response.json()
        if not isinstance(response_payload, dict):
            raise ValueError(f"Tavily response for query {query!r} was not a JSON object.")
        return query, response_payload

    return await _limited_gather([fetch(query) for query in queries], limit=3)


def _parse_tavily_results(query: str, payload: dict[str, Any]) -> list[SearchHit]:
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        return []

    parsed: list[SearchHit] = []
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
                snippet=str(raw.get("content") or "").strip(),
                score=_coerce_optional_float(raw.get("score")),
                date=str(raw.get("published_date") or "").strip(),
                query=query,
            )
        )
    return parsed


def _coerce_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


ENGINE = SearchEngine(
    label="Tavily Search",
    slug="tavily",
    report_name="Tavily",
    search=search_tavily,
    settings=lambda: [f"Tavily search depth: {_search_depth() or 'None'}"],
)
