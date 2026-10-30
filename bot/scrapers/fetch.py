"""One page fetch for every research section: ``fetch_page(url, policy)``.

Web search and the resolution-source scraper used to run their own ladders,
and the resolution page -- the one page that holds the answer -- got the
weaker one: two of the six adapters, no render-wait rung and no thin-content
check, so a login wall counted as success. Both now take the same ladder:

    run cache -> site adapter -> Firecrawl -> Crawl4AI -> Firecrawl render-wait
    -> Wayback snapshot -> best thin attempt, banner-marked

``FetchPolicy`` holds only what legitimately differs by caller (Firecrawl
priority, cache freshness, timeouts). The caller records the fetch in its own
ledger/trace and applies its own length budget; ``FetchedPage.content`` is the
full text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from bot.config import ENABLE_FIRECRAWL_GENERAL_SCRAPE
from bot.llm.cost_manager import HardLimitExceededError
from bot.trace import source_ledger

logger = logging.getLogger(__name__)

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
# client gets +15 s headroom on top; see scrapers.firecrawl_scrape).
_FIRECRAWL_GENERAL_TIMEOUT_SECONDS = 30
# Render-wait rung: held open long enough for client-side JS to populate the
# DOM. 8 s was the value verified against bsky.jazco.dev/stats; the longer
# page timeout gives the render room to finish inside the request.
_RENDER_WAIT_MS = 8_000
_FIRECRAWL_RENDER_TIMEOUT_SECONDS = 45
ENGINE_FIRECRAWL_RENDER = "firecrawl-render-wait"
ENGINE_WAYBACK = "wayback-snapshot"


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
    the page holds the specific value we came for is the reader's job; doing
    it here would need the question in scope and would turn every scrape into
    an LLM call.
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


@dataclass(frozen=True)
class FetchPolicy:
    """What differs by caller. The ladder itself never does."""

    name: str
    firecrawl_timeout: int
    firecrawl_max_age_ms: int
    # Resolution pages hold first claim on the per-question Firecrawl credits.
    firecrawl_priority: bool = False
    # Obey ENABLE_FIRECRAWL_GENERAL_SCRAPE (the general research switch).
    # Resolution pages always try Firecrawl while credits last.
    firecrawl_switchable: bool = True
    crawl_timeout: int | None = None
    adapter_timeout: float = 30.0
    # Keep the page's raw HTML (script blocks included) for the embedded-data
    # reader, so it need not download the page a second time.
    raw_html: bool = False


def search_policy() -> FetchPolicy:
    from bot.scrapers.firecrawl_scrape import GENERAL_MAX_AGE_MS

    return FetchPolicy("search", _FIRECRAWL_GENERAL_TIMEOUT_SECONDS, GENERAL_MAX_AGE_MS)


def resolution_policy(timeout: int = 30) -> FetchPolicy:
    from bot.scrapers.firecrawl_scrape import RESOLUTION_MAX_AGE_MS

    return FetchPolicy("resolution", timeout, RESOLUTION_MAX_AGE_MS, firecrawl_priority=True,
                       firecrawl_switchable=False, crawl_timeout=timeout,
                       adapter_timeout=float(timeout), raw_html=True)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    ok: bool
    engine: str
    content: str = ""   # full text, before any length budget; banner not included
    banner: str = ""    # set when only a thin attempt came back (ok, with a warning)
    error: str = ""     # why it failed; "inadequate content: ..." when thin
    # Raw HTML when the policy asks for it and a browser rung returned some;
    # kept from a thin attempt too (a JS shell is where embedded data lives).
    raw_html: str = ""

    @property
    def thin(self) -> bool:
        return self.ok and bool(self.banner)


async def fetch_page(url: str, policy: FetchPolicy, *, query: str = "") -> FetchedPage:
    """Fetch one page through the full ladder (see the module docstring)."""
    from dataclasses import replace

    from bot.scrapers.crawler.crawl import (
        basic_crawl_page,
        claim_scrape_url,
        get_cached_scrape_content,
        record_scrape_content,
    )

    # One claim per URL, before the adapter and the generic rungs: a failed
    # adapter falls through to the rungs below, which must not then see their
    # own URL as an earlier duplicate.
    if claim_scrape_url(url) is not None:
        cached = get_cached_scrape_content(url)
        if not cached:
            return FetchedPage(url, False, source_ledger.ENGINE_SKIPPED_DUPLICATE,
                               error="skipped duplicate: URL has already been scraped in this process")
        # A URL fetched twice in one run (main pass, then artifact retry) keeps
        # its thin-content warning on the second copy too (44875).
        adequate, reason = scrape_adequacy(cached)
        if adequate:
            return FetchedPage(url, True, source_ledger.ENGINE_CACHE, content=cached)
        return FetchedPage(url, True, source_ledger.ENGINE_CACHE, content=cached,
                           banner=thin_content_banner(reason, ["cache (earlier attempt this run)"]),
                           error=f"inadequate content: {reason}")

    # Best inadequate attempt seen so far. Adequacy decides whether to keep
    # escalating; it never decides whether to keep the text. If every rung
    # comes back thin we still return the longest thing we got, banner-marked,
    # rather than discarding it as a failure.
    best = {"content": "", "engine": "", "reason": "", "html": ""}
    engines_tried: list[str] = []
    errors: list[str] = []

    def with_html(page: FetchedPage) -> FetchedPage:
        return replace(page, raw_html=best["html"]) if best["html"] and policy.raw_html else page

    def accept(content: str, engine: str, html: str = "") -> FetchedPage | None:
        """The page if this rung's text is adequate; else note it as thin."""
        if len(html) > len(best["html"]):
            best["html"] = html
        record_scrape_content(url, content)
        adequate, reason = scrape_adequacy(content)
        if adequate:
            return with_html(FetchedPage(url, True, engine, content=content))
        engines_tried.append(engine)
        if content.strip() and len(content.strip()) > len(best["content"]):
            best.update(content=content.strip(), engine=engine, reason=reason)
        errors.append(f"{engine}: returned {reason}")
        return None

    # 1. Site adapter: reads the page through the site's own API or file
    # format. When it errors or returns nothing, the URL falls through to the
    # generic rungs instead of being lost for the run (Firecrawl renders PDFs;
    # Wikipedia, Metaculus and Sheets pages are plain HTML). A short but
    # non-empty adapter result is kept: adapters return compact structured
    # data (a quote, a question, a Trends table) that is often under the thin
    # floor and still the answer.
    adapter = None
    try:
        from bot.scrapers.adapters import find_adapter

        adapter = find_adapter(url)
    except Exception as exc:  # noqa: BLE001 - adapters are optional
        logger.warning("[adapter] registry unavailable for %s: %s: %s", url, type(exc).__name__, exc)
    if adapter is not None:
        try:
            logger.info("[adapter] %s handling %s", adapter.name, url)
            result = await adapter.extract(url, query=query, timeout=policy.adapter_timeout)
            if result.content.strip():
                record_scrape_content(url, result.content)
                return FetchedPage(url, True, f"adapter:{adapter.name}", content=result.content)
            adapter_error = f"{adapter.name} returned no content"
        except HardLimitExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall through to the generic rungs
            adapter_error = f"{type(exc).__name__}: {exc}"
        logger.warning("[adapter] %s failed for %s (%s); falling back to generic scraping.",
                       adapter.name, url, adapter_error)
        engines_tried.append(f"adapter:{adapter.name}")
        errors.append(f"adapter:{adapter.name}: {adapter_error}")

    firecrawl_on = not policy.firecrawl_switchable or ENABLE_FIRECRAWL_GENERAL_SCRAPE

    async def firecrawl(engine: str, timeout: int, **kwargs) -> FetchedPage | None:
        from bot.scrapers.firecrawl_scrape import (
            FirecrawlBudgetExceededError,
            FirecrawlCreditError,
            firecrawl_exhausted,
            firecrawl_scrape_page,
            mark_firecrawl_exhausted,
        )

        if firecrawl_exhausted():
            return None
        try:
            content, html = await firecrawl_scrape_page(
                url, timeout, priority=policy.firecrawl_priority, raw_html=policy.raw_html, **kwargs)
        except FirecrawlCreditError as exc:
            mark_firecrawl_exhausted()
            errors.append(f"{engine}: {exc}")
            logger.warning("Firecrawl exhausted (credits/auth) on %s — falling back to "
                           "Crawl4AI for the rest of this run: %s", url, exc)
            return None
        except FirecrawlBudgetExceededError as exc:
            errors.append(f"{engine}: {exc}")
            logger.info("%s", exc)
            return None
        except HardLimitExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - next rung
            errors.append(f"{engine}: {type(exc).__name__}: {exc}")
            logger.warning("%s failed for %s: %s: %s", engine, url, type(exc).__name__, exc)
            return None
        return accept(content, engine, html)

    # 2. Firecrawl first (main-content markdown, JS/PDF rendering, bot-wall
    # bypass). The shared per-question credit budget in scrapers.firecrawl_scrape
    # hard-caps spend and reserves first claim for the resolution-path URLs.
    if firecrawl_on:
        page = await firecrawl(source_ledger.ENGINE_FIRECRAWL, policy.firecrawl_timeout,
                               max_age_ms=policy.firecrawl_max_age_ms)
        if page is not None:
            return page

    # 3. Crawl4AI, the free fallback.
    try:
        crawl_kwargs = {"timeout": policy.crawl_timeout} if policy.crawl_timeout else {}
        content, html = await basic_crawl_page(url, **crawl_kwargs)
        page = accept(content, source_ledger.ENGINE_CRAWL4AI_BASIC, html)
        if page is not None:
            return page
    except HardLimitExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 - next rung
        errors.append(f"{source_ledger.ENGINE_CRAWL4AI_BASIC}: {type(exc).__name__}: {exc}")

    # 4. Render-wait: only reached when the ordinary engines came back thin,
    # which is the signature of a client-side-rendered page. Costs one
    # Firecrawl credit and ~8 s, so it is never on the fast path, and the
    # per-question credit cap bounds the worst case (44875 spent 1 of 25
    # credits while its resolution source failed five times).
    if firecrawl_on and best["content"]:
        page = await firecrawl(ENGINE_FIRECRAWL_RENDER, _FIRECRAWL_RENDER_TIMEOUT_SECONDS,
                               max_age_ms=0, wait_for_ms=_RENDER_WAIT_MS, only_main_content=False)
        if page is not None:
            return page

    # 5. Wayback snapshot: pages the live web will not serve (paywalls, bot
    # walls -- the 44773 NYT case, where one failed live fetch tombstoned the
    # URL for the whole run). Free (archive.org), never for market/quote pages
    # (stale prices, the 44267 class), and the capture date is stamped in-band
    # by snapshot_fallback_text.
    try:
        from bot.scrapers.adapters import Wayback as wayback_module

        snapshot_text = await wayback_module.snapshot_fallback_text(url)
    except Exception as exc:  # noqa: BLE001 - last rung
        logger.warning("Wayback snapshot fallback failed for %s: %s", url, exc)
        snapshot_text = ""
    if snapshot_text.strip():
        page = accept(snapshot_text, ENGINE_WAYBACK)
        if page is not None:
            return page

    # 6. Every rung came back thin. Returning the best attempt banner-marked
    # beats dropping it: the text is still weak evidence, and the banner is
    # what tells the readers that this page's silence is a retrieval failure
    # rather than a published absence.
    if best["content"]:
        record_scrape_content(url, best["content"])
        return with_html(FetchedPage(url, True, f"{best['engine']}+thin", content=best["content"],
                                     banner=thin_content_banner(best["reason"], engines_tried),
                                     error=f"inadequate content: {best['reason']}"))
    return with_html(FetchedPage(url, False, source_ledger.ENGINE_CRAWL4AI_BASIC,
                                 error="; ".join(errors) or "scrape returned no content"))
