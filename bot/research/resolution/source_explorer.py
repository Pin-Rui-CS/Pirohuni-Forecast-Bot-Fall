"""Resolution-source explorer: a SoCLaaS model browses the resolution source's
own site for the artifact the artifact check found missing.

Motivating failure (question 46422, 2026-10-22): the bot scraped the Swiss
drought platform eight times as markdown and never saw the per-region drought
classes or the platform's own weekly forecast -- both ship inside the page
source. The focused retry then searched the open web (Tavily), which is the
right tool for a figure reported in the news and the wrong one for a figure
that lives on the source itself. In a replay
(eval_tools/embedded_data_demo) DeepSeek with this loop recovered the current
count (17), all 38 regional series and the source's forecast to the resolution
week in 9 turns, at no cost.

The model gets one generic tool and replies with one JSON action per turn:

  fetch  up to MAX_URLS_PER_FETCH pages on the resolution source's host(s):
         visible-text excerpt + every table embedded in each page's source
         (embedded_tables), optionally only tables having given columns.
         Documents (PDF, Word, Excel, PowerPoint) are read through the
         Fetcher (bot.scrapers.fetch) and come back as text.
  text   the full visible text of one page or document, TEXT_CHARS at a time,
         for a value written in the text rather than held in a table
  rows   every row of one table
  final  the evidence report

Runs only on SoCLaaS (the SOCLAAS_MODEL_CHAIN, DeepSeek first): there is never
a paid fallback, and when SoCLaaS is unavailable the explorer returns "" and
the ordinary retry carries on. Bounded by turns, pages and wall time.
"""
from __future__ import annotations

import asyncio
from datetime import date
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from bot.config import llm_rate_limiter
from bot.llm import provider as llm_provider
from bot.llm import qwen_ladder, soclaas_models
from bot.llm.cost_manager import HardLimitExceededError, MonetaryCostManager
from bot.research.resolution import series_discovery
from bot.research.resolution.embedded_tables import Table, extract_tables, visible_text
from bot.trace import research_trace, source_ledger

logger = logging.getLogger(__name__)

SECTION_NAME = "Focused Artifact Retry (Resolution Source Explorer)"
LABEL = "source-explorer"
MAX_TURNS = int(os.getenv("SOURCE_EXPLORER_MAX_TURNS", "8"))
MAX_PAGES = int(os.getenv("SOURCE_EXPLORER_MAX_PAGES", "60"))
MAX_URLS_PER_FETCH = 40
WALL_SECONDS = float(os.getenv("SOURCE_EXPLORER_TIMEOUT_SECONDS", "1500"))
# One tool result, and the whole conversation, stay inside the smallest
# context in the chain (Qwen is the fallback). Older results are cut first.
MAX_RESULT_CHARS = 60_000
MAX_CONVERSATION_CHARS = 300_000
TRIMMED_RESULT_CHARS = 3_000
MAX_START_URLS = 25
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


def enabled() -> bool:
    """SOURCE_EXPLORER = auto (default: on whenever research runs on SoCLaaS),
    on, or off. Always off without a SoCLaaS key: it never falls back to paid."""
    mode = os.getenv("SOURCE_EXPLORER", "auto").strip().lower()
    if mode in {"0", "off", "false", "no"}:
        return False
    if not llm_provider.api_key_for(llm_provider.ENDPOINT_SOCLAAS):
        return False
    if mode in {"1", "on", "true", "yes"}:
        return True
    return bool(llm_provider.qwen_research_labels())


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


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
        host = _host(url)
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


def start_points(resolution_urls: list[str]) -> tuple[list[str], set[str]]:
    """(URLs to show first, hosts the explorer may fetch).

    The hosts are those of the URLs the question resolves off. The start list
    adds every page on those hosts that any lane scraped OR any search returned
    this run: deeper pages are the way in, and on 2026-10-24 (Q46422) the
    regional pages holding the source's forecast had only been returned by the
    search, never scraped, so the explorer never saw them.
    """
    hosts = {_host(u) for u in resolution_urls if _host(u)}
    rank = {source_ledger.ROLE_SCRAPED: 0, source_ledger.ROLE_RANKED: 1, source_ledger.ROLE_CANDIDATE: 2}
    found = [(rank[e.role], i, e.url) for i, e in enumerate(source_ledger.get_events())
             if e.role in rank and _host(e.url) in hosts]
    urls = list(resolution_urls) + [url for *_, url in sorted(found)]
    return list(dict.fromkeys(urls))[:MAX_START_URLS], hosts


def page_links(page_url: str, page: str, hosts: set[str]) -> list[str]:
    """Same-site page links: <a href> targets and site URLs quoted inside the
    page's data, minus scripts, styles and images."""
    unescaped = page.replace('\\"', '"').replace("\\/", "/")
    raw = [urljoin(page_url, h) for h in _HREF.findall(page)] + _ABS_URL.findall(unescaped)
    links = []
    for url in raw:
        url = url.rstrip(".,;")
        if url.startswith("http") and _host(url) in hosts and not _ASSET.search(url):
            links.append(url)
    return list(dict.fromkeys(links))


def _system_prompt(*, title: str, resolution_criteria: str, fine_print: str,
                   artifact_check: dict | None, start_urls: list[str], hosts: set[str],
                   today: str) -> str:
    check = artifact_check or {}
    missing = check.get("what_is_missing") or "the value the question resolves on"
    found = check.get("what_was_found") or ""
    return f"""You are the research step of a forecasting bot. Today is {today}.

Question: {title}
Resolution criteria: {resolution_criteria}
{('Fine print: ' + fine_print) if fine_print.strip() else ''}

The bot's research so far could not retrieve: {missing}
{('What it did find: ' + found) if found else ''}

The bot already saw these pages on the site (scraped as markdown text, or returned by a search).
The value may be written in a page's text, in a document the site links to (a PDF report, a
spreadsheet), or kept in the data behind the site's maps and charts inside the page source, where
a text scraper never sees it. Your tools read all three:
{chr(10).join('- ' + u for u in start_urls)}

Reply with exactly ONE JSON object per turn, nothing else:

  {{"action": "fetch", "urls": ["...", ...], "columns": ["optional", "column", "names"]}}
      Fetches up to {MAX_URLS_PER_FETCH} pages or documents ({MAX_PAGES} in total) from the source's sites
      ({', '.join(sorted(hosts))}) and from any other site their pages' code calls -- a map or
      tracker page often loads its data from another site's API (JSON) after the page opens; such a
      page lists those addresses, the settings in its code (base addresses, ids) and its address
      templates. For each page you get a short visible-text excerpt, its full text
      length, and a summary of every data table embedded in its source: rows, columns, value
      counts, first and last rows. Rows dated after today are marked FUTURE (the source's own
      forecast or schedule). Tables are named P<page>.T<n>. Each page also lists the same-site
      links it contains that you have not been shown yet. With "columns", only tables having ALL
      those columns are shown -- use it when fetching many similar pages. A document (PDF,
      Word, Excel, PowerPoint) comes back as an excerpt of its text; a data file (CSV, TSV,
      JSON) as a table.
  {{"action": "text", "url": "...", "from": 0}}
      The full visible text of one page or document, {TEXT_CHARS:,} characters at a time ("from" is
      the character to start at; the reply says where the next part starts). Fetches the page
      first if you have not yet. Use it when the excerpt suggests the value is written in the
      text rather than held in a table.
  {{"action": "source", "url": "...", "from": 0}}
      The page's raw source as downloaded (HTML, XML, JSON), {TEXT_CHARS:,} characters at a time.
      The visible text is made by code and can lose things: use this when it looks incomplete --
      leftover markup, dates or values you expected are missing, or far fewer characters of text
      than the page has bytes. Not available for documents (PDF, Word, Excel).
  {{"action": "find", "url": "...", "word": "..."}}
      Every place a word occurs in a page's raw source (a document's text), with
      {FIND_CONTEXT} characters either side. Use it on large pages and scripts (a site's .js file can be
      millions of characters) to find an API address, an id or a field name instead of paging
      through "source".
  {{"action": "rows", "table": "P1.T2", "contains": "optional word"}}
      Every row of one table (of a very long one, its most recent rows). With "contains", only
      the rows holding that word (any case) and how many there are -- use it to check whether
      an item is listed; zero matching rows in a complete list is itself a finding.
  {{"action": "final", "report": "..."}}
      Ends the research with your report (markdown).

You have at most {MAX_TURNS} turns. Work out where the resolution value is read from (a table's
field, or a sentence or figure in a page or document), find its most recent value, and anything
the source says about where it is heading by the resolution date (history, its own forecasts). Fetch addresses from the start list, the links
pages list, or a pattern those addresses follow (e.g. one region page's address with another
region's id), or a template in the site's code; do not guess site maps or page names. When you
fill in a pattern, take each part from an address you have seen, from a setting in the site's
code, or from a table field that holds the same value (an id or key column whose values match the
ones in seen addresses) -- never from a row's position in a table.
If an address you built fails, fetch a page that links to it and use the link as listed.
A date, time or slot belongs to an item only if the item's own text or field states it. Never
infer it from the order of items, from navigation or date-picker links, or from the page's other
dates; if the item carries none, report "date not stated" and look for one (the raw "source"
view, or a dated document) before using it.

The report records what the source CONTAINS -- quote values exactly as the tables or text give
them, with their dates and the table or page and URL they come from. Do NOT estimate probabilities or give a forecast;
the forecasters do that. Include:
1. Where the resolution value is read from (page, table and field, or the passage) and how you
   verified it.
2. Its most recent value and date. If it is a count over items, give the breakdown behind it.
3. Recent dated values and how much the quantity moves from period to period.
4. Every forecast or schedule the source itself publishes up to the resolution date, with numbers,
   clearly labelled as the source's forecast.
5. Which reading the resolver will most likely see on the resolution date (update cadence, data
   lags), and the evidence for that.
6. What you could not find."""


class _Pages:
    def __init__(self, hosts: set[str]):
        # The resolution source's own sites. ``hosts`` (what may be fetched)
        # grows by the sites their pages' code calls -- one step only: a page
        # on such a site never adds further sites.
        self.site_hosts = set(hosts)
        self.hosts = set(hosts)
        self.tables: dict[str, Table] = {}
        self.fetched: list[str] = []
        self.links_shown: set[str] = set()
        # Full visible text of every page and document read, for the "text" action.
        self.texts: dict[str, str] = {}
        # Raw source of every page (not documents), for the "source" action.
        self.sources: dict[str, str] = {}
        # Table digest -> id of the first table with those rows.
        self.first_seen: dict[str, str] = {}

    async def _document_text(self, url: str) -> tuple[str, str]:
        """(text, error) for a document, read through the Fetcher."""
        from bot.scrapers.fetch import fetch_page, search_policy

        page = await fetch_page(url, search_policy())
        if page.ok and page.content.strip():
            return page.content, ""
        return "", page.error or "the document returned no text"

    async def fetch(self, client: httpx.AsyncClient, urls: list[str], columns: list[str],
                    today: date) -> str:
        out = []
        for url in [str(u) for u in urls][:MAX_URLS_PER_FETCH]:
            if _host(url) not in self.hosts:
                out.append(f"## {url}\nrefused: only {', '.join(sorted(self.hosts))} may be fetched")
                continue
            if len(self.fetched) >= MAX_PAGES:
                out.append(f"## {url}\nrefused: the {MAX_PAGES}-page budget is spent")
                continue
            self.fetched.append(url)
            prefix = f"P{len(self.fetched)}"
            fetched, error, document = None, "", urlsplit(url).path.lower().endswith(DOCUMENT_SUFFIXES)
            if not document:
                # The embedded-data reader's fetch: raw HTML, redirect stubs
                # followed, and a size cap (a 165 MB export once sat behind a link).
                try:
                    fetched = await series_discovery._fetch(client, url)
                except httpx.HTTPError as exc:
                    fetched, error = None, f"{type(exc).__name__}: {exc}"
                else:
                    error = "HTTP error, or the page is over the size cap"
                # A document behind an address with no file extension.
                document = (fetched is not None and bool(fetched[1]) and not fetched[1].startswith(_TEXT_TYPES)
                            and not urlsplit(url).path.lower().endswith(DATA_SUFFIXES))
            if document:
                text, error = await self._document_text(url)
                source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine="source-explorer",
                                               ok=bool(text), chars=len(text), error=error)
                if not text:
                    out.append(f"## {prefix} {url}\ndocument could not be read: {error}")
                    continue
                self.texts[url] = text
                out.append(f"## {prefix} {url} (document, {len(text):,} chars of text; "
                           f"\"text\" action for all of it)\ntext: {text[:700]}")
                continue
            if fetched is None:
                source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine="source-explorer",
                                               ok=False, error=error)
                out.append(f"## {prefix} {url}\nfetch failed: {error}")
                continue
            page = fetched[0]
            source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine="source-explorer",
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
            links =[u for u in page_links(url, page, self.hosts)
                     if u not in self.links_shown and u not in self.fetched]
            if links:
                self.links_shown.update(links[:MAX_LINKS_PER_PAGE])
                more = f", first {MAX_LINKS_PER_PAGE}" if len(links) > MAX_LINKS_PER_PAGE else ""
                lines.append(f"links ({len(links)} new same-site{more}): "
                             + " ".join(links[:MAX_LINKS_PER_PAGE]))
            if _host(url) in self.site_hosts:
                external, settings, templates = code_pointers(page, self.site_hosts)
                new_hosts = sorted({_host(u) for u in external} - self.hosts)
                self.hosts.update(_host(u) for u in external)
                if external:
                    lines.append("other sites this page's code calls (now fetchable"
                                 + (f"; new: {', '.join(new_hosts)}" if new_hosts else "") + "): "
                                 + " ".join(external))
                if settings:
                    lines.append("settings in its code: " + "; ".join(settings))
                if templates:
                    lines.append("address templates in its code: " + " | ".join(templates))
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
        if contains.strip():
            return table.matching_rows(contains, MAX_RESULT_CHARS)
        return table.all_rows(MAX_RESULT_CHARS)


def _parse_action(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("the reply is not a JSON object")
    return value


def _trim(messages: list[dict]) -> None:
    """Cut the oldest tool results until the conversation fits."""
    total = sum(len(m["content"]) for m in messages)
    for message in messages[2:-1]:
        if total <= MAX_CONVERSATION_CHARS:
            return
        if message["role"] == "user" and len(message["content"]) > TRIMMED_RESULT_CHARS:
            total -= len(message["content"]) - TRIMMED_RESULT_CHARS
            message["content"] = (message["content"][:TRIMMED_RESULT_CHARS]
                                  + "\n[older tool result trimmed to save space]")


async def _ask(route: llm_provider.Route, messages: list[dict]) -> Any:
    payload = {"model": route.model_id, "messages": messages, "max_tokens": 32000}
    await llm_provider.gate_for(route).wait_async()
    async with llm_rate_limiter:
        handle = MonetaryCostManager.start_openrouter_call(LABEL, route.model_id, payload)
        try:
            response = await qwen_ladder.run_async(route, payload)
        except BaseException as exc:
            handle.record_failure(exc)
            raise
        handle.record_response(response)
    return response


async def explore(*, title: str, resolution_criteria: str, fine_print: str = "",
                  artifact_check: dict | None, resolution_urls: list[str],
                  today: date | None = None) -> str:
    """The explorer's report as a research section, or "" if it found nothing."""
    today = today or date.today()
    start_urls, hosts = start_points(resolution_urls)
    if not start_urls:
        return ""
    route = llm_provider.route_for_model(soclaas_models.SOCLAAS_ROLE)
    source_ledger.set_source_context("Resolution Source Explorer", "artifact retry")
    system = _system_prompt(title=title, resolution_criteria=resolution_criteria, fine_print=fine_print,
                            artifact_check=artifact_check, start_urls=start_urls, hosts=hosts,
                            today=today.isoformat())
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Begin. Reply with your first JSON action."}]
    pages = _Pages(hosts)
    report = ""
    started = time.monotonic()
    try:
        async with asyncio.timeout(WALL_SECONDS), httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
            for turn in range(1, MAX_TURNS + 1):
                response = await _ask(route, messages)
                reply = response.choices[0].message.content or ""
                messages.append({"role": "assistant", "content": reply})
                try:
                    action = _parse_action(reply)
                except ValueError as exc:
                    action, result = {"action": "invalid"}, f"Could not read your reply ({exc}). Reply with one JSON object."
                else:
                    kind = action.get("action")
                    if kind == "fetch":
                        result = await pages.fetch(client, list(action.get("urls") or []),
                                                   [str(c) for c in action.get("columns") or []], today)
                    elif kind == "text":
                        try:
                            start = int(action.get("from") or 0)
                        except (TypeError, ValueError):
                            start = 0
                        result = await pages.text(client, str(action.get("url") or ""), start, today)
                    elif kind == "source":
                        try:
                            start = int(action.get("from") or 0)
                        except (TypeError, ValueError):
                            start = 0
                        result = await pages.text(client, str(action.get("url") or ""), start, today,
                                                  source=True)
                    elif kind == "find":
                        result = await pages.find(client, str(action.get("url") or ""),
                                                  str(action.get("word") or ""), today)
                    elif kind == "rows":
                        result = pages.rows(str(action.get("table")), str(action.get("contains") or ""))
                    elif kind == "final":
                        report = str(action.get("report") or "").strip()
                        result = ""
                    else:
                        result = f"Unknown action {kind!r}; use fetch, text, source, find, rows or final."
                if not report and turn == MAX_TURNS - 1:
                    result += "\n\nOne turn left: your next reply must be the final action."
                research_trace.emit(
                    "explore", f"source explorer turn {turn} ({action.get('action')})",
                    reply + (f"\n\n--- tool result ({len(result):,} chars) ---\n{result}" if result else ""),
                    meta={"turn": turn, "model": getattr(response, "soclaas_model", route.model_id),
                          "pages_fetched": len(pages.fetched)},
                    kind="model")
                logger.info("[source-explorer] turn %d: %s -> %d chars", turn, action.get("action"), len(result))
                if report:
                    break
                messages.append({"role": "user", "content": result})
                _trim(messages)
    except HardLimitExceededError:
        raise
    except (TimeoutError, qwen_ladder.QwenLadderFailed) as exc:
        logger.warning("[source-explorer] stopped after %.0fs: %s: %s",
                       time.monotonic() - started, type(exc).__name__, exc)
    if not report:
        return ""
    page_lines = "\n".join(f"- {u}" for u in dict.fromkeys(pages.fetched))
    return (
        f"_A SoCLaaS model browsed the resolution source's own pages and documents and read their "
        f"text and the data embedded in their source ({len(pages.fetched)} pages). Values below "
        f"are quoted from those pages and tables; anything labelled as the source's forecast is "
        f"the source's own projection, not a measurement._\n\n{report}\n\n### Pages read\n{page_lines}"
    )
