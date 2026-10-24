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
         (embedded_tables), optionally only tables having given columns
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
# Same-site links listed per fetched page, so the model follows real addresses
# instead of guessing them (2026-10-24: 5 of 8 turns went on guessed 404s).
MAX_LINKS_PER_PAGE = 40
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
Many sites keep the data behind their maps and charts inside the page source, where a text
scraper never sees it. You have a tool that reads it:
{chr(10).join('- ' + u for u in start_urls)}

Reply with exactly ONE JSON object per turn, nothing else:

  {{"action": "fetch", "urls": ["...", ...], "columns": ["optional", "column", "names"]}}
      Fetches up to {MAX_URLS_PER_FETCH} pages (hosts: {', '.join(sorted(hosts))}; {MAX_PAGES} pages in
      total). For each page you get a short visible-text excerpt and a summary of every data table
      embedded in its source: rows, columns, value counts, first and last rows. Rows dated after
      today are marked FUTURE (the source's own forecast or schedule). Tables are named
      P<page>.T<n>. Each page also lists the same-site links it contains that you have not been
      shown yet. With "columns", only tables having ALL those columns are shown -- use it
      when fetching many similar pages.
  {{"action": "rows", "table": "P1.T2"}}
      Every row of one table.
  {{"action": "final", "report": "..."}}
      Ends the research with your report (markdown).

You have at most {MAX_TURNS} turns. Work out which field of which table the resolution value is
read from, find its most recent value, and anything the source says about where it is heading by
the resolution date (history, its own forecasts). Fetch addresses from the start list, the links
pages list, or a pattern those addresses follow (e.g. one region page's address with another
region's id); do not guess site maps or page names.

The report records what the source CONTAINS -- quote values exactly as the tables give them, with
their dates and the table and URL they come from. Do NOT estimate probabilities or give a forecast;
the forecasters do that. Include:
1. Where the resolution value is read from (page, table, field) and how you verified it.
2. Its most recent value and date. If it is a count over items, give the breakdown behind it.
3. Recent dated values and how much the quantity moves from period to period.
4. Every forecast or schedule the source itself publishes up to the resolution date, with numbers,
   clearly labelled as the source's forecast.
5. Which reading the resolver will most likely see on the resolution date (update cadence, data
   lags), and the evidence for that.
6. What you could not find."""


class _Pages:
    def __init__(self, hosts: set[str]):
        self.hosts = hosts
        self.tables: dict[str, Table] = {}
        self.fetched: list[str] = []
        self.links_shown: set[str] = set()

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
            try:
                response = await client.get(url)
                page = response.text
                ok = response.status_code < 400
            except httpx.HTTPError as exc:
                source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine="source-explorer",
                                               ok=False, error=f"{type(exc).__name__}: {exc}")
                out.append(f"## {prefix} {url}\nfetch failed: {type(exc).__name__}: {exc}")
                continue
            source_ledger.record_url_event(url, source_ledger.ROLE_SCRAPED, engine="source-explorer",
                                           ok=ok, chars=len(page))
            tables = extract_tables(url, page, id_prefix=f"{prefix}.T")
            self.tables.update((t.id, t) for t in tables)
            shown = [t for t in tables if all(c in t.columns for c in columns)] if columns else tables
            lines = [f"## {prefix} {url} (HTTP {response.status_code}, {len(page):,} bytes, "
                     f"{len(tables)} embedded tables"
                     + (f", {len(shown)} with columns {columns}" if columns else "") + ")",
                     "visible text: " + visible_text(page, 300 if columns else 700)]
            lines += [t.summary(today) for t in shown]
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
            out.append("\n".join(lines))
        result = "\n\n".join(out)
        if len(result) > MAX_RESULT_CHARS:
            result = (result[:MAX_RESULT_CHARS]
                      + f"\n\n[cut at {MAX_RESULT_CHARS:,} chars -- fetch fewer pages or pass \"columns\"]")
        return result

    def rows(self, table_id: str) -> str:
        table = self.tables.get(table_id)
        if table is None:
            return f"no table {table_id!r}; tables so far: {', '.join(list(self.tables)[:40])}"
        return table.all_rows()[:MAX_RESULT_CHARS]


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
                    elif kind == "rows":
                        result = pages.rows(str(action.get("table")))
                    elif kind == "final":
                        report = str(action.get("report") or "").strip()
                        result = ""
                    else:
                        result = f"Unknown action {kind!r}; use fetch, rows or final."
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
        f"_A SoCLaaS model browsed the resolution source's own pages and read the data embedded "
        f"in their source ({len(pages.fetched)} pages). Values below are quoted from those tables; "
        f"anything labelled as the source's forecast is the source's own projection, not a "
        f"measurement._\n\n{report}\n\n### Pages read\n{page_lines}"
    )
