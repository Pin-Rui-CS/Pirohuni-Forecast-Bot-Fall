"""URL-ranking and search-result helpers shared by every web-search provider."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bot.trace import source_ledger

logger = logging.getLogger(__name__)

DEFAULT_URL_RANKING_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_MAX_RANKED_URLS = 20


@dataclass(frozen=True)
class SearchHit:
    """One search result, in the same shape whichever engine returned it.

    ``snippet`` is the engine's text about the page (SerpAPI snippet, Tavily
    content, Firecrawl description). ``extras`` are engine-only fields shown
    as extra lines, e.g. Firecrawl's ("Source", "news").
    """

    title: str
    url: str
    snippet: str = ""
    date: str = ""
    query: str = ""
    score: float | None = None
    extras: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SearchEngine:
    """A search API the web-search step can use to find URLs.

    Only the search differs between engines; ranking, scraping and extraction
    are shared (see web_search). ``label`` names the research section and the
    trace lane, ``slug`` prefixes the ranking call's label (``serp`` ->
    ``serp-url-ranking``), and ``digest`` asks the ranking call to also write
    a digest that replaces the raw result list in the section — on for
    Firecrawl, whose descriptions can run to whole pages (44879).
    """

    label: str
    slug: str
    report_name: str
    search: Callable[[list[str], str], Awaitable[list[SearchHit]]]
    settings: Callable[[], list[str]] = field(default=lambda: [])
    digest: bool = False


@dataclass(frozen=True)
class RankedUrl:
    url: str
    purpose: str


@dataclass(frozen=True)
class RankedUrlGroup:
    group: str
    group_purpose: str
    urls: list[RankedUrl]


# Hosts that essentially never yield scrapeable content (login walls, JS video
# shells). Their search snippets STAY in the research output — a social post is
# sometimes the only trace of a fact — but they are excluded from the ranking
# payload, so ranking tokens and scrape-cycle slots are never spent on them
# (2026-07-07 finding: 14 of 89 ranking candidates were social URLs; none ever
# scraped successfully).
SOCIAL_MEDIA_HOSTS = (
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "x.com",
    "twitter.com",
    "reddit.com",
    "linkedin.com",
    "tiktok.com",
    "threads.net",
)


def is_social_url(url: str) -> bool:
    host = urlsplit((url or "").strip()).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == h or host.endswith("." + h) for h in SOCIAL_MEDIA_HOSTS)


def exclude_social_results(results: list) -> list:
    """Drop social-media results from a ranking payload (works for SerpAPI,
    Tavily, and Firecrawl result objects — whichever of .link/.url exists)."""
    return [
        item
        for item in results
        if not is_social_url(getattr(item, "link", "") or getattr(item, "url", ""))
    ]


# Replaces a raw search snippet's body when the same URL was scraped in full:
# the scraped extract is a strict superset of the snippet, so repeating the
# snippet only spends compiler-input budget twice on the same page. Snippets
# for URLs that were NOT scraped are never dropped — for an unscraped page the
# snippet is the only copy of that evidence in the entire pipeline (the 44619
# miss: the decisive items lived only in snippets of unscraped URLs).
SNIPPET_OMITTED_NOTE = (
    "(snippet omitted — this page was scraped in full; see 'Compiled scraped "
    "research' above)"
)


def _record_ranked_url_groups(groups: list[RankedUrlGroup]) -> None:
    """Record every URL an LLM selected for scraping, grouped by purpose."""
    for group in groups:
        for item in group.urls:
            source_ledger.record_url_event(
                item.url,
                source_ledger.ROLE_RANKED,
                round_label=group.group,
                detail=item.purpose,
            )


def dedupe_hits(hits: Any) -> list[SearchHit]:
    """Keep the first hit per canonical URL (engines repeat pages across queries)."""
    seen: set[str] = set()
    deduped: list[SearchHit] = []
    for hit in hits:
        key = _canonical_link(hit.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


async def _limited_gather(coros: list[Any], limit: int) -> list[Any]:
    import asyncio

    semaphore = asyncio.Semaphore(limit)

    async def run(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return await asyncio.gather(*(run(coro) for coro in coros))


def _parse_ranked_url_groups(parsed: Any) -> list[RankedUrlGroup]:
    if isinstance(parsed, dict):
        raw_groups = parsed.get("ranked_url_groups")
    else:
        raise ValueError(f"URL-ranking JSON must be an object, got {type(parsed).__name__}")

    if not isinstance(raw_groups, list):
        raise ValueError("URL-ranking JSON missing a list field named 'ranked_url_groups'")

    groups: list[RankedUrlGroup] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue

        raw_urls = raw_group.get("urls")
        if not isinstance(raw_urls, list):
            continue

        urls: list[RankedUrl] = []
        for raw_url in raw_urls:
            if not isinstance(raw_url, dict):
                continue
            url = str(raw_url.get("url") or raw_url.get("link") or "").strip()
            purpose = str(raw_url.get("purpose") or raw_url.get("reason") or "").strip()
            if url:
                urls.append(
                    RankedUrl(
                        url=url,
                        purpose=purpose or "Potentially useful source to scrape.",
                    )
                )

        if urls:
            group = str(raw_group.get("group") or raw_group.get("category") or "").strip()
            group_purpose = str(raw_group.get("group_purpose") or raw_group.get("purpose") or "").strip()
            groups.append(
                RankedUrlGroup(
                    group=group or "Research purpose",
                    group_purpose=group_purpose or "Sources serving a shared research purpose.",
                    urls=urls,
                )
            )
    return groups


def _dedupe_ranked_url_groups(
    ranked_url_groups: list[RankedUrlGroup],
    max_ranked_urls: int,
    exclude_already_scraped: bool = False,
) -> list[RankedUrlGroup]:
    """Collapse duplicate URLs and cap the ranked pool.

    ``exclude_already_scraped`` additionally drops URLs this run has already
    fetched. The focused artifact retry sets it: re-ranking a URL that is
    already in the scrape cache cannot add evidence, it only consumes one of
    the retry's few slots and then re-enters the compiler as a duplicate. In
    44875 two of the retry's four scrapes were cache hits on that path.
    """
    already_scraped = _already_scraped_predicate() if exclude_already_scraped else None
    seen: set[str] = set()
    skipped_scraped: list[str] = []
    total_urls = 0
    deduped_groups: list[RankedUrlGroup] = []
    for group in ranked_url_groups:
        deduped_urls: list[RankedUrl] = []
        for item in group.urls:
            key = _canonical_link(item.url)
            if not key or key in seen:
                continue
            if already_scraped is not None and already_scraped(item.url):
                skipped_scraped.append(item.url)
                seen.add(key)
                continue
            seen.add(key)
            deduped_urls.append(item)
            total_urls += 1
            if total_urls >= max_ranked_urls:
                break
        if deduped_urls:
            deduped_groups.append(
                RankedUrlGroup(
                    group=group.group,
                    group_purpose=group.group_purpose,
                    urls=deduped_urls,
                )
            )
        if total_urls >= max_ranked_urls:
            break
    if skipped_scraped:
        logger.info(
            "[rank] excluded %d already-scraped URL(s) from the retry pool: %s",
            len(skipped_scraped),
            ", ".join(skipped_scraped[:5]),
        )
    return deduped_groups


def _already_scraped_predicate():
    """Return a callable ``(url) -> bool``, or None when the registry is absent."""
    try:
        from bot.scrapers.crawler.crawl import scrape_already_attempted
    except Exception as exc:  # noqa: BLE001 - filtering is best-effort
        logger.warning(
            "scrape registry unavailable (%s: %s); retry ranking will not exclude "
            "already-scraped URLs",
            type(exc).__name__,
            exc,
        )
        return None
    return scrape_already_attempted


# Query parameters that identify the referrer or campaign, never the document.
# Left in the canonical key they split one page into several "distinct" URLs:
# 44875 ranked and separately fetched both bsky.jazco.dev/stats and
# bsky.jazco.dev/stats?ref=little-flying-robots.ghost.io, then recorded the
# second as "0 new bytes (identical to an earlier scrape)" — after paying.
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = frozenset({
    "ref", "referrer", "source", "src", "fbclid", "gclid", "gbraid", "wbraid",
    "msclkid", "mc_cid", "mc_eid", "igshid", "ncid", "cmpid", "spm", "_hsenc",
    "_hsmi", "vero_id", "yclid", "s_kwcid",
})


def _strip_tracking_params(query: str) -> str:
    if not query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    return urlencode(kept)


def _canonical_link(link: str) -> str:
    parts = urlsplit(link.strip())
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            _strip_tracking_params(parts.query),
            "",
        )
    )


def _normalise_query(query: str) -> str:
    return " ".join(str(query).strip().split())


def _extract_json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue

    raise ValueError(f"Could not extract JSON from Serp URL-ranking response: {text[:500]}")
