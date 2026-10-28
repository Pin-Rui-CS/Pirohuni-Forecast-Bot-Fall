"""SerpAPI Google: one search engine of the web-search step (see web_search)."""

from __future__ import annotations

from typing import Any

import httpx

from bot.config import SERPAPI_API_KEY
from bot.research.search.search_common import (
    SearchEngine,
    SearchHit,
    _limited_gather,
    dedupe_hits,
)


DEFAULT_SERP_NUM_RESULTS = 10
SERPAPI_SEARCH_URL = "https://serpapi.com/search"


async def search_serpapi(queries: list[str], reason: str = "") -> list[SearchHit]:
    return await fetch_serpapi_organic_results(queries=queries)


async def fetch_serpapi_organic_results(
    queries: list[str],
    num_results_per_query: int = DEFAULT_SERP_NUM_RESULTS,
    api_key: str | None = None,
    gl: str = "us",
    hl: str = "en",
) -> list[SearchHit]:
    """Fetch and deduplicate Google organic results from SerpAPI."""
    api_key = api_key or SERPAPI_API_KEY
    if not api_key:
        raise ValueError("Missing SERPAPI_API_KEY for SerpAPI Google research.")

    num_results = max(1, min(100, int(num_results_per_query)))
    async with httpx.AsyncClient(timeout=30) as client:
        responses = await _gather_serpapi_queries(
            client=client,
            queries=queries,
            api_key=api_key,
            num_results=num_results,
            gl=gl,
            hl=hl,
        )

    return dedupe_hits(
        result
        for query, payload in responses
        for result in _parse_organic_results(query, payload)
    )


async def _gather_serpapi_queries(
    client: httpx.AsyncClient,
    queries: list[str],
    api_key: str,
    num_results: int,
    gl: str,
    hl: str,
) -> list[tuple[str, dict[str, Any]]]:
    async def fetch(query: str) -> tuple[str, dict[str, Any]]:
        response = await client.get(
            SERPAPI_SEARCH_URL,
            params={
                "engine": "google",
                "q": query,
                "api_key": api_key,
                "num": num_results,
                "gl": gl,
                "hl": hl,
                "output": "json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"SerpAPI response for query {query!r} was not a JSON object.")
        error = payload.get("error")
        if error:
            raise ValueError(f"SerpAPI error for query {query!r}: {error}")
        return query, payload

    return await _limited_gather([fetch(query) for query in queries], limit=3)


def _parse_organic_results(query: str, payload: dict[str, Any]) -> list[SearchHit]:
    raw_results = payload.get("organic_results", [])
    if not isinstance(raw_results, list):
        return []

    parsed: list[SearchHit] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        link = str(raw.get("link", "")).strip()
        title = str(raw.get("title", "")).strip()
        if not link or not title:
            continue
        parsed.append(
            SearchHit(
                title=title,
                url=link,
                date=str(raw.get("date", "")).strip(),
                snippet=str(raw.get("snippet", "")).strip(),
                query=query,
            )
        )
    return parsed


ENGINE = SearchEngine(
    label="SerpAPI Google",
    slug="serp",
    report_name="SerpAPI",
    search=search_serpapi,
)
