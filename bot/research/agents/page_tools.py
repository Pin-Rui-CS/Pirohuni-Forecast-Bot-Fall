"""Page-reading tools for a research agent: fetch pages and documents, read
their text or raw source, find a word in it, and list the tables they hold.

One ``PageReader`` serves one agent run. It decides which sites may be fetched
(``hosts``; None = any site), counts pages against a budget, records every
fetch in the source ledger, and keeps what it read so later actions are free.

Fetch order per page: the plain download first (fast, free, and the only way
to get the raw source the embedded-data reader needs); when that fails -- a
block, an HTTP error, a page over the size cap -- the bot's Fetcher
(bot.scrapers.fetch: run cache, site adapters, Firecrawl, Crawl4AI), which is
what the web search already uses, so a page it scraped comes from the cache.
Documents (PDF, Word, Excel, PowerPoint) always go to the Fetcher.
"""
from __future__ import annotations

from datetime import date
import logging
import re
from urllib.parse import urljoin, urlsplit

import httpx

from bot.research.resolution import series_discovery
from bot.research.resolution.embedded_tables import Table, extract_tables, visible_text
from bot.trace import source_ledger

logger = logging.getLogger(__name__)

MAX_URLS_PER_FETCH = 40
# One tool result stays inside the smallest context in the SoCLaaS chain.
MAX_RESULT_CHARS = 60_000
# One "text" action returns at most this much of a page's or document's visible
# text; a longer one is read in further calls with "from".
TEXT_CHARS = 30_000
# Documents the plain download cannot read as text. They go through the
# Fetcher instead, whose first rung (the PDF adapter) is free.
DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
               "application/javascript", "application/ld+json")
# Data files read as tables (embedded_tables), whatever content type the
# server sends: CSVs often come as application/octet-stream or vnd.ms-excel.
DATA_SUFFIXES = (".csv", ".tsv", ".txt", ".json")
# Same-site links listed per fetched page, so the model follows real addresses
# instead of guessing them (2026-10-24: 5 of 8 turns went on guessed 404s).
MAX_LINKS_PER_PAGE = 40
MAX_FIND_HITS = 15
FIND_CONTEXT = 200
_HREF = re.compile(r'href="([^"#]+)', re.IGNORECASE)
_ABS_URL = re.compile(r"https?://[^\s\"'<>\\)]+")
_ASSET = re.compile(r"(/_next/|/static/|\.(?:js|css|png|jpe?g|svg|gif|ico|woff2?|map|webp)(?:\?|$))",
                    re.IGNORECASE)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Sites a page's code may name that never hold the page's data: analytics,
# ads, social embeds, fonts, script CDNs, schema namespaces.
_NOT_DATA_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "google.com", "googleapis.com", "gstatic.com", "facebook.com", "facebook.net", "twitter.com",
    "x.com", "instagram.com", "youtube.com", "linkedin.com", "tiktok.com", "pinterest.com",
    "hotjar.com", "sentry.io", "cloudflare.com", "cloudflareinsights.com", "jsdelivr.net", "unpkg.com",
    "w3.org", "schema.org", "gravatar.com", "wp.com", "wordpress.org", "addtoany.com", "recaptcha.net",
)
_SETTING = re.compile(
    # The suffix must follow a lower-case letter or digit (camelCase), so
    # constants like HYBRID or ANDROID are not read as ids.
    r"""\b([A-Za-z_$][\w$]*[a-z0-9](?:Url|URL|Uri|URI|Id|ID|Endpoint|Path|Base|Host))\s*[:=]\s*("[^"\n]{1,160}"|'[^'\n]{1,160}'|\d{1,15})""")
_API_HINT = re.compile(r"/api\b|/v\d+/|\.json\b|geojson|graphql|/data\b|/feeds?\b", re.IGNORECASE)
_SECRET = re.compile(r"key|secret|token|passw|auth|credential", re.IGNORECASE)
_TEMPLATE = re.compile(r"`([^`\n]{0,160}\$\{[^`\n]{0,200})`")
MAX_CODE_POINTERS = 25


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _data_host(host: str) -> bool:
    return bool(host) and not any(host == h or host.endswith("." + h) for h in _NOT_DATA_HOSTS)


def code_pointers(page: str, site_hosts: set[str]) -> tuple[list[str], list[str], list[str]]:
    """(addresses on OTHER sites, settings, address templates) a page's code
    names. A map or tracker page often draws its data from another site's API
    at run time: on 2026-11-08 (Q46580) OCEARCH's tracker named
    baseApiUrl "https://www.mapotic.com/api/v1/", ocearchMapoticMapId 3413 and
    `${this.mapsApiUrl}${x}/pois/${D}/motion/with-meta/` inside a 2 MB script,
    and the shark's latest ping sat behind them. Settings that look like keys
    or passwords are never shown."""
    unescaped = page.replace('\\"', '"').replace("\\/", "/")
    external = []
    for url in _ABS_URL.findall(unescaped):
        url = url.rstrip(".,;'\"`")
        host = host_of(url)
        if host and host not in site_hosts and _data_host(host) and not _ASSET.search(url):
            external.append(url)
    settings = [f"{name}: {value}" for name, value in _SETTING.findall(unescaped)
                if not _SECRET.search(name)]
    templates = [t for t in _TEMPLATE.findall(unescaped) if "/" in t]
    # Data addresses first: a page's code also names sponsors, app stores, video.
    external = sorted(dict.fromkeys(external), key=lambda u: not _API_HINT.search(u))
    settings.sort(key=lambda s: "http" not in s and not s.split(": ", 1)[1].isdigit())
    return (external[:MAX_CODE_POINTERS],
            list(dict.fromkeys(settings))[:MAX_CODE_POINTERS],
            list(dict.fromkeys(templates))[:MAX_CODE_POINTERS])


def page_links(page_url: str, page: str, hosts: set[str]) -> list[str]:
    """Page links on ``hosts``: <a href> targets and site URLs quoted inside the
    page's data, minus scripts, styles and images."""
    unescaped = page.replace('\\"', '"').replace("\\/", "/")
    raw = [urljoin(page_url, h) for h in _HREF.findall(page)] + _ABS_URL.findall(unescaped)
    links = []
    for url in raw:
        url = url.rstrip(".,;")
        if url.startswith("http") and host_of(url) in hosts and not _ASSET.search(url):
            links.append(url)
    return list(dict.fromkeys(links))


class PageReader:
    """The fetch / text / source / find / rows tools of one agent run.

    ``hosts``: the sites that may be fetched, or None for any site. With a set,
    the sites those pages' own code calls are added as they are seen -- one
    step only: a page on an added site never adds further sites.
    ``engine`` names the agent in the source ledger ("source-explorer", ...).
    """

    def __init__(self, hosts: set[str] | None, *, engine: str, max_pages: int,
                 max_paid_fetches: int | None = None):
        self.site_hosts = set(hosts) if hosts is not None else None
        self.hosts = set(hosts) if hosts is not None else None
        self.engine = engine
        self.max_pages = max_pages
        # Pages that may go through the Fetcher's paid rung (None = no limit
        # of its own; the question's shared Firecrawl cap still applies).
        self.max_paid_fetches = max_paid_fetches
        self.paid_fetches = 0
        self.tables: dict[str, Table] = {}
        self.fetched: list[str] = []
        self.links_shown: set[str] = set()
        # Full visible text of every page and document read, for the "text" action.
        self.texts: dict[str, str] = {}
        # Raw source of every page (not documents), for the "source" action.
        self.sources: dict[str, str] = {}
        # Table digest -> id of the first table with those rows.
        self.first_seen: dict[str, str] = {}
        # What the model was shown per page beyond its text (table summaries
        # and rows), so a value quoted from a table can be checked too.
        self.shown: dict[str, list[str]] = {}
        self._squashed: dict[tuple[str, str, int], str] = {}

    def allowed(self, url: str) -> bool:
        return url.startswith("http") and (self.hosts is None or host_of(url) in self.hosts)

    async def _plain(self, client: httpx.AsyncClient, url: str):
        """(fetched, error) from the plain download: raw HTML, redirect stubs
        followed, and a size cap (a 165 MB export once sat behind a link).

        When that is refused and the site has an adapter that can hand over
        the page as the site serves it (UrlAdapter.raw_page), that page is
        used: on 2026-11-12 Wikipedia answered 403 to every plain download,
        while its adapter's API path returns the article's HTML, tables intact.
        The adapter's usual output will not do here: it is a model's summary
        of the page, and a row's quote must be checked against the page itself.
        """
        try:
            fetched = await series_discovery._fetch(client, url)
            error = "HTTP error, or the page is over the size cap"
        except httpx.HTTPError as exc:
            fetched, error = None, f"{type(exc).__name__}: {exc}"
        if fetched is not None:
            return fetched, ""
        try:
            from bot.scrapers.adapters import find_adapter

            adapter = find_adapter(url)
            raw = await adapter.raw_page(url) if adapter is not None else None
        except Exception as exc:  # noqa: BLE001 - an adapter failure leaves the Fetcher to try
            logger.info("[page-tools] adapter raw page failed for %s: %s: %s", url, type(exc).__name__, exc)
            raw = None
        if raw:
            return (raw, "text/html", url), ""
        return None, error

    async def _fetcher_text(self, url: str) -> tuple[str, str]:
        """(text, error) for a document or a page the plain download could not
        get, read through the bot's Fetcher.

        The Fetcher's Firecrawl rung draws on the question's shared credits,
        which web search needs too. A reader with ``max_paid_fetches`` stops
        using that rung after that many pages went past the free first rungs;
        later pages still get the free rungs (adapters, Crawl4AI, Wayback).
        """
        from dataclasses import replace

        from bot.scrapers.fetch import fetch_page, search_policy

        policy = search_policy()
        capped = self.max_paid_fetches is not None and self.paid_fetches >= self.max_paid_fetches
        if capped:
            policy = replace(policy, firecrawl_off=True)
        page = await fetch_page(url, policy)
        free = (page.engine in (source_ledger.ENGINE_CACHE, source_ledger.ENGINE_SKIPPED_DUPLICATE)
                or page.engine.startswith("adapter:"))
        if not capped and not free:
            self.paid_fetches += 1
        if page.ok and page.content.strip():
            return page.content, ""
        return "", page.error or "the page returned no text"

    async def fetch(self, client: httpx.AsyncClient, urls: list[str], columns: list[str],
                    today: date) -> str:
        out = []
        for url in [str(u) for u in urls][:MAX_URLS_PER_FETCH]:
            if not self.allowed(url):
                out.append(f"## {url}\nrefused: only {', '.join(sorted(self.hosts or []))} may be fetched")
                continue
            if len(self.fetched) >= self.max_pages:
                out.append(f"## {url}\nrefused: the {self.max_pages}-page budget is spent")
                continue
            self.fetched.append(url)
            prefix = f"P{len(self.fetched)}"
            fetched, error, document = None, "", urlsplit(url).path.lower().endswith(DOCUMENT_SUFFIXES)
            if not document:
                fetched, error = await self._plain(client, url)
                # A document behind an address with no file extension.
                document = (fetched is not None and bool(fetched[1]) and not fetched[1].startswith(_TEXT_TYPES)
                            and not urlsplit(url).path.lower().endswith(DATA_SUFFIXES))
            if document or fetched is None:
                # A document, or a page the plain download could not get (on
                # Q46722 pnc.com refused every plain request): the Fetcher.
                kind = "document" if document else "page, read through the fetcher after the plain download failed"
                text, fetcher_error = await self._fetcher_text(url)
                source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine=self.engine,
                                               ok=bool(text), chars=len(text), error=fetcher_error or error)
                if not text:
                    what = "document could not be read" if document else "fetch failed"
                    out.append(f"## {prefix} {url}\n{what}: {fetcher_error or error}")
                    continue
                self.texts[url] = text
                out.append(f"## {prefix} {url} ({kind}, {len(text):,} chars of text; "
                           f"\"text\" action for all of it)\ntext: {text[:700]}")
                continue
            page = fetched[0]
            source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine=self.engine,
                                           ok=True, chars=len(page))
            self.texts[url] = visible_text(page, len(page))
            self.sources[url] = page
            tables = extract_tables(url, page, id_prefix=f"{prefix}.T")
            self.tables.update((t.id, t) for t in tables)
            shown = [t for t in tables if all(c in t.columns for c in columns)] if columns else tables
            lines = [f"## {prefix} {url} ({len(page):,} bytes, "
                     f"{len(self.texts[url]):,} chars of visible text, {len(tables)} embedded tables"
                     + (f", {len(shown)} with columns {columns}" if columns else "") + ")",
                     "visible text: " + self.texts[url][:300 if columns else 700]]
            # A site-wide table is summarised once: on 2026-10-30 (Q46516) the
            # same cookie-settings table filled every one of 30 pages.
            for t in shown:
                first = self.first_seen.setdefault(t.digest, t.id)
                lines.append(t.summary(today) if first == t.id
                             else f"[{t.id}] {len(t.rows)} rows; same table as {first}")
            # A filter must never hide a table silently: on 2026-10-24 (Q46422)
            # "columns": ["cdi"] hid the regional weekly table (date, level,
            # counts) that held the source's forecast, and the model concluded
            # none existed. Hidden tables keep a one-line entry.
            for t in tables:
                if t not in shown:
                    future = t.future_rows(today)
                    lines.append(f"  (hidden by filter) [{t.id}] {len(t.rows)} rows; columns: "
                                 + ", ".join(t.columns[:8])
                                 + (f"; {future} FUTURE row(s)" if future else ""))
            link_hosts = self.hosts if self.hosts is not None else {host_of(url)}
            links = [u for u in page_links(url, page, link_hosts)
                     if u not in self.links_shown and u not in self.fetched]
            if links:
                self.links_shown.update(links[:MAX_LINKS_PER_PAGE])
                more = f", first {MAX_LINKS_PER_PAGE}" if len(links) > MAX_LINKS_PER_PAGE else ""
                lines.append(f"links ({len(links)} new same-site{more}): "
                             + " ".join(links[:MAX_LINKS_PER_PAGE]))
            if self.site_hosts is not None and host_of(url) in self.site_hosts:
                external, settings, templates = code_pointers(page, self.site_hosts)
                new_hosts = sorted({host_of(u) for u in external} - self.hosts)
                self.hosts.update(host_of(u) for u in external)
                if external:
                    lines.append("other sites this page's code calls (now fetchable"
                                 + (f"; new: {', '.join(new_hosts)}" if new_hosts else "") + "): "
                                 + " ".join(external))
                if settings:
                    lines.append("settings in its code: " + "; ".join(settings))
                if templates:
                    lines.append("address templates in its code: " + " | ".join(templates))
            self.shown.setdefault(url, []).append("\n".join(lines))
            out.append("\n".join(lines))
        result = "\n\n".join(out)
        if len(result) > MAX_RESULT_CHARS:
            result = (result[:MAX_RESULT_CHARS]
                      + f"\n\n[cut at {MAX_RESULT_CHARS:,} chars -- fetch fewer pages or pass \"columns\"]")
        return result

    async def text(self, client: httpx.AsyncClient, url: str, start: int, today: date,
                   source: bool = False) -> str:
        """The visible text of one page or document -- or, with ``source``, a
        page's raw source -- TEXT_CHARS at a time. The raw source is the check
        on visible_text: on Q46606 the cleaned text of an RSS feed had lost
        every item's date, and the model filled them in from navigation links."""
        store, what = (self.sources, "raw source") if source else (self.texts, "text")
        prelude = ""
        if url not in self.texts:
            prelude = await self.fetch(client, [url], [], today)
            if url not in self.texts:
                return prelude
        if url not in store:
            return (f"{prelude}\n\n" if prelude else "") + (
                f"## {url} -- no raw source: it was read as a document; use \"text\".")
        body = store[url]
        start = min(max(0, start), len(body))
        end = min(start + TEXT_CHARS, len(body))
        tail = (f"\n[continues: {len(body) - end:,} more chars; ask again with \"from\": {end}]"
                if end < len(body) else f"\n[end of {what}]")
        result = (f"{prelude}\n\n" if prelude else "") + (
            f"## {url} -- {what}, chars {start:,}-{end:,} of {len(body):,}\n{body[start:end]}{tail}")
        return result[:MAX_RESULT_CHARS]

    async def find(self, client: httpx.AsyncClient, url: str, word: str, today: date) -> str:
        """Every place a word occurs in a page's raw source (or a document's
        text), with the text around it -- the way into a large script, where
        the "source" action would need dozens of turns."""
        word = word.strip()
        if not word:
            return 'give a "word" to look for'
        prelude = ""
        if url not in self.texts:
            prelude = await self.fetch(client, [url], [], today)
            if url not in self.texts:
                return prelude
        body = self.sources.get(url) or self.texts[url]
        lowered, needle = body.lower(), word.lower()
        hits, at = [], lowered.find(needle)
        while at >= 0 and len(hits) < MAX_FIND_HITS:
            hits.append(f"[char {at:,}] ...{body[max(0, at - FIND_CONTEXT):at + len(word) + FIND_CONTEXT]}...")
            at = lowered.find(needle, at + len(needle))
        total = lowered.count(needle)
        head = f"## {url} -- {total} occurrence(s) of {word!r}" + (
            f", first {MAX_FIND_HITS} shown" if total > len(hits) else "")
        result = (f"{prelude}\n\n" if prelude else "") + head + "\n" + "\n".join(hits)
        return result[:MAX_RESULT_CHARS]

    def rows(self, table_id: str, contains: str = "") -> str:
        table = self.tables.get(table_id)
        if table is None:
            return f"no table {table_id!r}; tables so far: {', '.join(list(self.tables)[:40])}"
        result = (table.matching_rows(contains, MAX_RESULT_CHARS) if contains.strip()
                  else table.all_rows(MAX_RESULT_CHARS))
        self.shown.setdefault(table.url, []).append(result)
        return result

    def has_text(self, url: str, quote: str) -> bool:
        """Whether ``quote`` appears on a page this run fetched -- in its text,
        its raw source, or a table of it the model was shown -- ignoring case
        and runs of whitespace."""
        needle = _squash(quote)
        if not needle:
            return False
        bodies = {"text": self.texts.get(url, ""), "source": self.sources.get(url, ""),
                  "shown": "\n".join(self.shown.get(url, []))}
        for name, body in bodies.items():
            if not body:
                continue
            key = (url, name, len(body))
            if key not in self._squashed:
                self._squashed[key] = _squash(body)
            if needle in self._squashed[key]:
                return True
        return False


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()
