"""Resolution-source explorer: a SoCLaaS model browses the resolution source's
own site for the value the question resolves on, its history and the source's
own forecasts.

Motivating failure (question 46422, 2026-10-22): the bot scraped the Swiss
drought platform eight times as markdown and never saw the per-region drought
classes or the platform's own weekly forecast -- both ship inside the page
source. The focused retry then searched the open web (Tavily), which is the
right tool for a figure reported in the news and the wrong one for a figure
that lives on the source itself. In a replay
(eval_tools/embedded_data_demo) DeepSeek with this loop recovered the current
count (17), all 38 regional series and the source's forecast to the resolution
week in 9 turns, at no cost.

Scope: the sites the question resolves off, plus the sites their pages' own
code calls. It never searches the web and never leaves those sites; history
that lives elsewhere is the history builder's job (bot.research.history).

The page tools (fetch / text / source / find / rows) and the turn loop are the
shared ones in bot.research.agents; this module holds what is the explorer's
own: where it starts, its prompt, its limits and its report.

Runs only on SoCLaaS (the SOCLAAS_MODEL_CHAIN, DeepSeek first): there is never
a paid fallback, and when SoCLaaS is unavailable the explorer returns "".
Bounded by turns, pages and wall time.
"""
from __future__ import annotations

from datetime import date
import logging
import os

import httpx

from bot.llm import provider as llm_provider
from bot.research.agents import loop
from bot.research.agents.page_tools import (
    FIND_CONTEXT,
    MAX_URLS_PER_FETCH,
    TEXT_CHARS,
    USER_AGENT,
    PageReader,
    host_of,
)
from bot.trace import source_ledger

logger = logging.getLogger(__name__)

SECTION_NAME = "Focused Artifact Retry (Resolution Source Explorer)"
LABEL = "source-explorer"
MAX_TURNS = int(os.getenv("SOURCE_EXPLORER_MAX_TURNS", "8"))
MAX_PAGES = int(os.getenv("SOURCE_EXPLORER_MAX_PAGES", "60"))
WALL_SECONDS = float(os.getenv("SOURCE_EXPLORER_TIMEOUT_SECONDS", "1500"))
MAX_START_URLS = 25
# Pages that may use the Fetcher's Firecrawl rung. Since 2026-11-13 a page the
# plain download cannot get goes to the Fetcher; on Q46726 that day every
# sec.gov page did, and with the history builder the question's whole Firecrawl
# budget (25 of 25) was spent. Later pages still get the free rungs.
MAX_PAID_FETCHES = int(os.getenv("SOURCE_EXPLORER_MAX_PAID_FETCHES", "5"))


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


def start_points(resolution_urls: list[str]) -> tuple[list[str], set[str]]:
    """(URLs to show first, hosts the explorer may fetch).

    The hosts are those of the URLs the question resolves off. The start list
    adds every page on those hosts that any lane scraped OR any search returned
    this run: deeper pages are the way in, and on 2026-10-24 (Q46422) the
    regional pages holding the source's forecast had only been returned by the
    search, never scraped, so the explorer never saw them.
    """
    hosts = {host_of(u) for u in resolution_urls if host_of(u)}
    rank = {source_ledger.ROLE_SCRAPED: 0, source_ledger.ROLE_RANKED: 1, source_ledger.ROLE_CANDIDATE: 2}
    found = [(rank[e.role], i, e.url) for i, e in enumerate(source_ledger.get_events())
             if e.role in rank and host_of(e.url) in hosts]
    urls = list(resolution_urls) + [url for *_, url in sorted(found)]
    return list(dict.fromkeys(urls))[:MAX_START_URLS], hosts


def _task_lines(gap: dict | None) -> str:
    """What the explorer is told the bot still lacks. ``gap`` comes from the
    resolution summary's VALUE ON PAGE line (status found / partial / missing
    / unknown) or, in a replay, from an artifact check."""
    gap = gap or {}
    found = gap.get("what_was_found") or ""
    if gap.get("status") == "found":
        return ("The bot's scraper read the resolution page as text, and its summary says the value the "
                "question resolves on is on that page. Your job is what a text scrape cannot see: confirm "
                "the value from the source's own data, and collect its dated history and any forecast or "
                "schedule the source publishes.")
    missing = gap.get("what_is_missing") or "the value the question resolves on"
    return (f"The bot's research so far could not retrieve: {missing}"
            + (f"\nWhat it did find: {found}" if found else ""))


def _system_prompt(*, title: str, resolution_criteria: str, fine_print: str,
                   artifact_check: dict | None, start_urls: list[str], hosts: set[str],
                   today: str) -> str:
    return f"""You are the research step of a forecasting bot. Today is {today}.

Question: {title}
Resolution criteria: {resolution_criteria}
{('Fine print: ' + fine_print) if fine_print.strip() else ''}

{_task_lines(artifact_check)}

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


async def explore(*, title: str, resolution_criteria: str, fine_print: str = "",
                  artifact_check: dict | None, resolution_urls: list[str],
                  today: date | None = None) -> str:
    """The explorer's report as a research section, or "" if it found nothing."""
    today = today or date.today()
    start_urls, hosts = start_points(resolution_urls)
    if not start_urls:
        return ""
    source_ledger.set_source_context("Resolution Source Explorer", "main pass")
    system = _system_prompt(title=title, resolution_criteria=resolution_criteria, fine_print=fine_print,
                            artifact_check=artifact_check, start_urls=start_urls, hosts=hosts,
                            today=today.isoformat())
    pages = PageReader(hosts, engine=LABEL, max_pages=MAX_PAGES, max_paid_fetches=MAX_PAID_FETCHES)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30,
                                 follow_redirects=True) as client:

        async def fetch(action: dict) -> str:
            return await pages.fetch(client, list(action.get("urls") or []),
                                     [str(c) for c in action.get("columns") or []], today)

        async def text(action: dict) -> str:
            return await pages.text(client, str(action.get("url") or ""), loop.int_arg(action, "from"), today)

        async def source(action: dict) -> str:
            return await pages.text(client, str(action.get("url") or ""), loop.int_arg(action, "from"),
                                    today, source=True)

        async def find(action: dict) -> str:
            return await pages.find(client, str(action.get("url") or ""), str(action.get("word") or ""), today)

        async def rows(action: dict) -> str:
            return pages.rows(str(action.get("table")), str(action.get("contains") or ""))

        outcome = await loop.run(
            system=system,
            handlers={"fetch": fetch, "text": text, "source": source, "find": find, "rows": rows},
            label=LABEL, trace_stage="explore", trace_name="source explorer",
            max_turns=MAX_TURNS, wall_seconds=WALL_SECONDS,
            meta=lambda: {"pages_fetched": len(pages.fetched)})
    report = str((outcome.final or {}).get("report") or "").strip()
    if not report:
        return ""
    page_lines = "\n".join(f"- {u}" for u in dict.fromkeys(pages.fetched))
    return (
        f"_A SoCLaaS model browsed the resolution source's own pages and documents and read their "
        f"text and the data embedded in their source ({len(pages.fetched)} pages). Values below "
        f"are quoted from those pages and tables; anything labelled as the source's forecast is "
        f"the source's own projection, not a measurement._\n\n{report}\n\n### Pages read\n{page_lines}"
    )
