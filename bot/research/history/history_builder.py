"""History builder: a SoCLaaS model finds past values or past cases on the web
and copies them into at most two small tables, which code checks row by row.

Why (survey of 53 questions, 3-12 Nov 2026, and the October base-rate labels):
the evidence plan asked for a history table or series on 31 of 52 questions and
the rows were really missing on about 21; only ~39% of the forecasters'
starting estimates were counted from data in the brief, and those starts
scored better (Brier 0.147 against 0.192 for a start from judgement). The
source explorer cannot fill this gap: it stays on the resolution source's own
site, and the history often lives elsewhere (a statistics office, Wikipedia, a
data file).

What it is and is not:
  - It searches the web (the shared search function: no search the question
    already ran is repeated) and reads any site (the shared page tools).
  - It keeps a row only when code finds the row's quote on the page it cites
    (table_store). Counts are computed by code.
  - It never estimates a probability and never judges the forecast: research
    and judgement stay separate. The forecasters do the counting that matters.
  - It is its own module. It shares the page tools and the turn loop with the
    source explorer by using them, not by inheriting from it.

Runs only on SoCLaaS: there is never a paid model call. Searches use the
search engines' credits, capped at MAX_SEARCHES per question.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import logging
import os

import httpx

from bot.llm import provider as llm_provider
from bot.research.agents import loop
from bot.research.agents.page_tools import (FIND_CONTEXT, MAX_URLS_PER_FETCH, TEXT_CHARS, USER_AGENT, PageReader,
                                            host_of, page_links)
from bot.research.evidence_plan import (ARTIFACT_HEADING, HISTORY_TABLES_HEADING, MAIN_SIGNAL_HEADING, plan_history,
                                        plan_sections)
from bot.research.history import same_field
from bot.research.history.table_store import MAX_COLUMNS, MAX_COUNTS, MAX_NOTES, MAX_ROWS, MAX_TABLES, TableStore
from bot.research.history.table_store import number as table_number
from bot.research.search import search_service
from bot.trace import source_ledger

logger = logging.getLogger(__name__)

SECTION_NAME = "History Tables"
LABEL = "history-builder"
MAX_TURNS = int(os.getenv("HISTORY_BUILDER_MAX_TURNS", "14"))
MAX_PAGES = int(os.getenv("HISTORY_BUILDER_MAX_PAGES", "25"))
MAX_SEARCHES = int(os.getenv("HISTORY_BUILDER_MAX_SEARCHES", "10"))
# Pages that may use the Fetcher's Firecrawl rung. The question's Firecrawl
# credits are shared with web search, which must not be left short; the
# replay (2026-11-13, 13 runs) used 0-5 a question.
MAX_PAID_FETCHES = int(os.getenv("HISTORY_BUILDER_MAX_PAID_FETCHES", "5"))
MAX_QUERIES_PER_SEARCH = 3
# Editions one "same_field" action may read (free routes only; they do not count
# against MAX_PAGES). A table holds MAX_ROWS rows, so more would not fit.
MAX_SAME_FIELD_DOCUMENTS = 40
WALL_SECONDS = float(os.getenv("HISTORY_BUILDER_TIMEOUT_SECONDS", "1200"))
MAX_START_HITS = 30
_MAX_BACKGROUND_CHARS = 3_000


def enabled() -> bool:
    """HISTORY_BUILDER = off (default, until the replay test passes), on, or
    auto (on whenever research runs on SoCLaaS). Always off without a SoCLaaS
    key: it never falls back to a paid model."""
    mode = os.getenv("HISTORY_BUILDER", "off").strip().lower()
    if mode not in {"1", "on", "true", "yes", "auto"}:
        return False
    if not llm_provider.api_key_for(llm_provider.ENDPOINT_SOCLAAS):
        return False
    return mode != "auto" or bool(llm_provider.qwen_research_labels())


def start_hits() -> list[str]:
    """Pages this question's web search has already found, as "title -- url"
    lines: the builder starts from them instead of searching again."""
    seen, lines = set(), []
    for hit in source_ledger.get_search_hits():
        if hit.url in seen:
            continue
        seen.add(hit.url)
        lines.append(f"{' '.join(hit.title.split())[:90] or '(no title)'} -- {hit.url}")
    return lines[:MAX_START_HITS]


def _system_prompt(*, title: str, resolution_criteria: str, fine_print: str, background: str,
                   artifact: str, found: list[str], today: str) -> str:
    found_block = ("\n".join(f"- {line}" for line in found)
                   or "- (none yet: the bot's web search has listed nothing)")
    return f"""You are a research step of a forecasting bot. Today is {today}.

Question: {title}
Resolution criteria: {resolution_criteria}
{('Fine print: ' + fine_print) if fine_print.strip() else ''}
{('Background: ' + background[:_MAX_BACKGROUND_CHARS]) if background.strip() else ''}

What the bot's evidence plan asked research to find:
{artifact.strip() or '(the plan named nothing specific)'}

YOUR JOB. Forecasters start from how often something has happened before, or how a quantity has
moved before. They do this well when they can count it from a table and badly when they must recall
it. Build at most {MAX_TABLES} small tables of PAST values or PAST cases, copied from pages you read:

  Table A -- the history the question is about: past values of the quantity it asks about, or past
      cases of the kind of event it asks about. The plan above usually names it.
  Table B (optional; the evidence plan may already have set it up) -- ONE of these:
      (1) the track record of the main signal the plan names (a poll, guidance, a consensus, a stated
          plan or date): one row per past case, with the signal's value and what actually happened
          (past polls next to the results, past guidance next to the reported figures, past announced
          dates next to the real ones). Prefer this when the plan names a signal: it tells a
          forecaster how far to trust that signal this time.
      (2) a different comparison group: a second, honest way to ask "how often does this kind of
          thing happen", wider or narrower than Table A, if it could give a different starting number.

You do NOT forecast. Never write a probability, a rate you worked out yourself, or an opinion on
the answer. Code computes the counts; the forecasters do the judging.

Pages the bot's web search already found for this question (it will read some of them itself):
{found_block}

Reply with exactly ONE JSON object per turn, nothing else:

  {{"action": "plan", "tables": [{{"id": "A", "title": "...", "row_is": "one ...", "columns": ["date", "..."], "why": "...", "reason": "..."}}], "queries": ["...", "..."]}}
      Defines a table, or changes one already set up (same "id"; say why in "reason" -- the change
      is recorded in the report). When the opening message says the evidence plan has set tables
      up, you start from those and send "plan" only to add or change one; otherwise it is your
      FIRST action. Rows already in a changed table are kept if its first column stays the same.
      Say what one row of each table is before you look for rows: "row_is" (one
      federal election; one month's reading; one earlier case of X), up to {MAX_COLUMNS} columns, and why
      the table helps. The first column identifies the row: a date written YYYY, YYYY-MM or
      YYYY-MM-DD when rows are dated, otherwise the case's name, and it must differ for every
      row (if two cases share a date, add the case's name to it). Do not make columns for the
      source or for notes: code records each row's source. If a table's rows are not written on
      a page as such and you will work each one out from several lines (the highest value in a
      window, a change between two dates), add "worked_out": true to that table; the report
      then says so. Prefer tables you can copy. "queries" (optional, up to
      {MAX_QUERIES_PER_SEARCH}) are searched in the same turn.
  {{"action": "search", "queries": ["...", "..."]}}
      Web search, up to {MAX_QUERIES_PER_SEARCH} queries per turn and {MAX_SEARCHES} in total. A query close to one this
      question already ran is not run again: you get that search's results, free. Search for the
      page that HOLDS the table (a statistics page, a list of past cases, a data file), not for
      news about today's question.
  {{"action": "fetch", "urls": ["...", ...], "columns": ["optional", "column", "names"]}}
      Reads up to {MAX_URLS_PER_FETCH} pages or documents ({MAX_PAGES} in total), on any site. For each you get the
      start of its text, its full length, and a summary of every data table in its source (rows,
      columns, first and last rows), named P<page>.T<n>. A PDF or spreadsheet comes back as text;
      a CSV or JSON file as a table. With "columns", only tables having ALL those columns are
      shown.
  {{"action": "text", "url": "...", "from": 0}}
      The full text of one page or document, {TEXT_CHARS:,} characters at a time ("from" is where to start).
  {{"action": "find", "url": "...", "word": "..."}}
      Every place a word occurs on a page, with {FIND_CONTEXT} characters either side.
  {{"action": "rows", "table": "P1.T2", "contains": "optional word"}}
      Every row of one page table (of a very long one, its most recent rows), or only the rows
      holding a word.
  {{"action": "same_field", "table": "A", "columns": {{"date": "date", "value": "..."}},
    "example": {{"url": "...", "value": "20,552", "date": "11 de noviembre de 2026", "date_iso": "2026-11-11"}},
    "documents": {{"from_page": "...", "links_containing": "...", "max": {MAX_SAME_FIELD_DOCUMENTS}}}}}
      For a table whose rows are editions of ONE recurring report (a daily bulletin, a weekly
      situation report) listed on a page. Read one edition yourself first; in "example" give that
      edition's address, the value exactly as it is written there, and the edition's own date as
      written there plus in YYYY-MM-DD. Code learns the words just before the value and before the
      date, reads up to {MAX_SAME_FIELD_DOCUMENTS} editions linked from "from_page" for free (when the editions are files,
      every link to a file of the example's type, in the page's order; for web pages, the links
      containing "links_containing"; or give "urls"), and adds a row per edition it can read. Editions
      it cannot read are listed back; if the report's wording changed, send same_field again with
      an example from one of those. One turn, and the editions do not use your page budget.
      Copies rows into your table. Each row needs the address of a page you fetched and a "quote":
      a short piece of text copied EXACTLY from a tool result for that page, holding the row's
      value. Code looks for the quote on the page and rejects the row if it is not there, so a row
      from memory cannot get in. Send many rows per turn. Rejected rows come back with the reason.
  {{"action": "count", "table": "A", "where": [{{"column": "...", "op": ">=", "value": "..."}}]}}
      Code counts the rows meeting every condition (ops: >=, <=, >, <, ==, !=, contains) and adds the
      count to the report, up to {MAX_COUNTS} per table. Ask for the counts a forecaster would want: how
      many rows are at or past the question's threshold, how many cases ended the way the question
      asks about.
  SAVE TURNS: adding rows and counting need no turn of their own. Any other action (search,
      fetch, text, find, rows, plan, final) may carry them:
      {{"action": "fetch", "urls": ["..."], "add_rows": [{{"table": "A", "rows": [...]}}], "count": [{{"table": "A", "where": [...]}}]}}
      The rows are added first, and the reply reports them together with the action's result. Use
      add_rows or count as an action by itself only when you have nothing else to do that turn.
  {{"action": "final", "coverage": {{"A": {{"complete": false, "text": "..."}}}}, "also_noted": [{{"text": "...", "url": "...", "quote": "..."}}], "could_not_find": "..."}}
      Ends the work. "coverage": for each table, "complete": true ONLY if the table holds every
      row of its kind for the period it covers (every year, every case, hits and misses alike);
      otherwise false, and the report warns readers not to read its counts as a rate. "text":
      the period covered, what is missing, and whether the missing rows are likely to differ
      from the ones found (a page about failures lists only the failures). "also_noted": up to {MAX_NOTES}
      facts you came across that bear on the question and are not in a table, each with its page
      and an exact quote (checked like a row). "could_not_find": what you looked for and did not get.

You have {MAX_TURNS} turns, {MAX_SEARCHES} searches and {MAX_PAGES} pages; each reply tells you what is left. Rules:
- Rows come only from pages you fetched in this run. If you remember a value, find a page that
  states it; do not add it from memory.
- Add rows as you find them: when a page you just read states a row, send it with your very next
  action (carried as "add_rows"). Rows of one table may come from different pages, one or two per
  page. Do not wait for a single page that lists everything; if you never find it, the rows you
  did not add are lost. A table of three checked rows is useful; an empty one is not.
- A table holds at most {MAX_ROWS} rows. If the history is longer, choose the spacing that suits the
  question's time window (yearly, monthly, the same week each year), keep the most recent rows,
  and say so in "coverage".
- A list of cases must hold the cases where the thing did NOT happen as well as those where it
  did, with a column saying which; a list of only the hits gives no rate. If a page lists only
  the hits, say so in "coverage".
- Only finished periods and settled cases go in. Never add a row for the period or the case the
  question itself asks about, and never a forecast as if it were a past value.
- Copy values as the page gives them (number and unit). Do not convert, round or fill gaps.
- Look first for one page holding the whole series; it saves you pages and turns. If two
  sources disagree, add both rows: code marks the conflict.
- Stop when the tables hold what a forecaster needs to count, or when more searching stops
  finding new rows. An honest small table beats a padded one."""


@dataclass
class BuildResult:
    """One run's section text and what it took (for the trace and replays)."""

    section: str = ""
    turns: int = 0
    searches: int = 0
    pages: int = 0
    rows: dict[str, int] = field(default_factory=dict)   # table id -> rows kept
    rejected: int = 0
    notes: int = 0
    stopped: str = ""
    actions: list[str] = field(default_factory=list)


async def _same_field(action: dict, *, store: TableStore, pages: PageReader, client: httpx.AsyncClient,
                      today: date) -> str:
    """The "same_field" action: one field read by code from many editions of
    one report, learned from an edition the model has read (see same_field.py)."""
    table = store.tables.get(str(action.get("table") or "").strip().upper())
    if table is None:
        return f"No table {action.get('table')!r}. Define it first with the plan action."
    columns = action.get("columns") if isinstance(action.get("columns"), dict) else {}
    date_column = str(columns.get("date") or table.key)
    value_column = str(columns.get("value") or "")
    if date_column not in table.columns or value_column not in table.columns:
        return (f'Give "columns": {{"date": ..., "value": ...}} naming columns of table {table.id}: '
                f"{', '.join(table.columns)}.")
    example = action.get("example") if isinstance(action.get("example"), dict) else {}
    example_url = str(example.get("url") or "")
    if example_url not in pages.texts:
        prelude = await pages.fetch(client, [example_url], [], today)
        if example_url not in pages.texts:
            return f"The example edition could not be read.\n{prelude}"
    learned, why = same_field.learn(pages.texts[example_url], str(example.get("value") or ""),
                                    str(example.get("date") or ""), str(example.get("date_iso") or ""))
    if learned is None:
        return f"Nothing read: {why}."

    documents = action.get("documents") if isinstance(action.get("documents"), dict) else {}
    room = MAX_ROWS - len(table.rows)
    wanted = min(room, MAX_SAME_FIELD_DOCUMENTS, loop.int_arg(documents, "max") or MAX_SAME_FIELD_DOCUMENTS)
    if wanted <= 0:
        return f"Table {table.id} is full ({MAX_ROWS} rows)."
    urls = [str(u) for u in documents.get("urls") or [] if str(u).startswith("http")]
    outside: list[str] = []
    listing = str(documents.get("from_page") or "")
    if not urls and listing:
        if listing not in pages.sources:
            prelude = await pages.fetch(client, [listing], [], today)
            if listing not in pages.sources:
                return f"The page listing the editions could not be read.\n{prelude}"
        contains = str(documents.get("links_containing") or "").lower()
        suffix = "." + example_url.rsplit(".", 1)[-1].lower() if "." in example_url.rsplit("/", 1)[-1] else ""
        links = page_links(listing, pages.sources[listing], {host_of(listing), host_of(example_url)})
        if suffix:
            # Editions are files of the example's type. Every such link is
            # tried, in the page's order: a filter on the name would drop an
            # edition whose file was named differently -- on 2026-11-16 the
            # newest Mexico bulletin was "Informe_diario_12_11_2026.pdf" and
            # every earlier one "INFORME_DIARIO_SARAMPION_<date>.pdf". A file
            # that is not an edition is simply not read.
            urls = [u for u in links if u.lower().split("?")[0].endswith(suffix)]
            outside = [u for u in urls if contains and contains not in u.lower()]
        else:
            urls = [u for u in links if contains in u.lower()] if contains else []
    urls = list(dict.fromkeys([example_url] + urls))[:wanted]
    outside = [u for u in outside if u in urls]
    if len(urls) <= 1:
        return 'No other editions found: give "documents": {"from_page": "...", "links_containing": "..."} or "urls".'

    errors = await pages.read_quietly(client, urls)
    rows, skipped = [], []
    for url in urls:
        if url in errors:
            skipped.append(f"{url}: could not be read ({errors[url][:80]})")
            continue
        reading = same_field.read(pages.texts.get(url, ""), learned)
        if reading.value is None or not reading.date:
            skipped.append(f"{url}: {reading.reason}")
            continue
        rows.append({"values": {date_column: reading.date, value_column: reading.value},
                     "url": url, "quote": reading.quote, "_number": table_number(reading.value)})
    added = store.add_rows(table.id, [{k: v for k, v in r.items() if k != "_number"} for r in rows])
    falls = []
    ordered = sorted(rows, key=lambda r: r["values"][date_column])
    for before, after in zip(ordered, ordered[1:]):
        if None not in (before["_number"], after["_number"]) and after["_number"] < before["_number"]:
            falls.append(f"{before['values'][date_column]} {before['values'][value_column]} -> "
                         f"{after['values'][date_column]} {after['values'][value_column]}")
    lines = [f"same_field: {len(urls)} editions tried, {len(rows)} read by code, {len(skipped)} skipped.",
             added]
    if outside:
        read_outside = [u for u in outside if any(r["url"] == u for r in rows)]
        lines.append(f"{len(outside)} of the files tried do not contain {documents.get('links_containing')!r} "
                     f"in their address (every file of the example's type on the page is tried); "
                     f"{len(read_outside)} of them read as editions: " + " ".join(read_outside[:5]))
    if skipped:
        lines.append("Skipped (read these yourself, or send same_field again with an example from one of "
                     "them if the wording changed):\n" + "\n".join(skipped[:15])
                     + (f"\n... and {len(skipped) - 15} more" if len(skipped) > 15 else ""))
    if falls:
        lines.append("The value FELL between these consecutive editions; check them before trusting the "
                     "table: " + "; ".join(falls[:10]))
    return "\n".join(lines)


def _as_list(value) -> list[dict]:
    """One {...} or a list of them, as a list of dicts."""
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, dict)]


async def build(*, title: str, resolution_criteria: str = "", fine_print: str = "", background: str = "",
                evidence_plan: str = "", target_date: str = "", today: date | None = None) -> str:
    """The history tables as a research section, or "" when no row was kept."""
    result = await run(title=title, resolution_criteria=resolution_criteria, fine_print=fine_print,
                       background=background, evidence_plan=evidence_plan, target_date=target_date, today=today)
    return result.section


async def run(*, title: str, resolution_criteria: str = "", fine_print: str = "", background: str = "",
              evidence_plan: str = "", target_date: str = "", today: date | None = None) -> BuildResult:
    today = today or date.today()
    source_ledger.set_source_context("History Builder", "main pass")
    pages = PageReader(None, engine=LABEL, max_pages=MAX_PAGES, max_paid_fetches=MAX_PAID_FETCHES)
    store = TableStore(pages.has_text, target_date)
    budget = search_service.SearchBudget(MAX_SEARCHES)
    system = _system_prompt(
        title=title, resolution_criteria=resolution_criteria, fine_print=fine_print, background=background,
        artifact=plan_sections(evidence_plan, (ARTIFACT_HEADING, MAIN_SIGNAL_HEADING, HISTORY_TABLES_HEADING)),
        found=start_hits(), today=today.isoformat())
    planned = plan_history(evidence_plan)

    async def run_search(queries) -> str:
        queries = [str(q) for q in (queries if isinstance(queries, list) else [])][:MAX_QUERIES_PER_SEARCH]
        if not queries:
            return 'Give "queries": ["...", "..."].'
        outcome = await search_service.search(queries, budget=budget, reason="history builder")
        return search_service.format_outcome(outcome)

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30,
                                 follow_redirects=True) as client:

        async def plan(action: dict) -> str:
            result = store.define(action.get("tables") or [])
            if action.get("queries"):
                result += "\n\n" + await run_search(action.get("queries"))
            return result

        async def search(action: dict) -> str:
            return await run_search(action.get("queries"))

        async def fetch(action: dict) -> str:
            return await pages.fetch(client, list(action.get("urls") or []),
                                     [str(c) for c in action.get("columns") or []], today)

        async def text(action: dict) -> str:
            return await pages.text(client, str(action.get("url") or ""), loop.int_arg(action, "from"), today)

        async def find(action: dict) -> str:
            return await pages.find(client, str(action.get("url") or ""), str(action.get("word") or ""), today)

        async def rows(action: dict) -> str:
            return pages.rows(str(action.get("table")), str(action.get("contains") or ""))

        async def add_rows(action: dict) -> str:
            return store.add_rows(action.get("table"), action.get("rows") or [])

        async def count(action: dict) -> str:
            return store.count(action.get("table"), action.get("where") or [])

        async def same_field_action(action: dict) -> str:
            return await _same_field(action, store=store, pages=pages, client=client, today=today)

        def carried(action: dict) -> str:
            """Rows and counts sent along with another action. Adding rows used
            to need a turn of its own (1-5 of 14 turns in the 2026-11-13
            replay), though its reply gives the model nothing new to read."""
            out = []
            for item in _as_list(action.get("add_rows")):
                out.append(store.add_rows(item.get("table"), item.get("rows") or []))
            for item in _as_list(action.get("count")):
                out.append(store.count(item.get("table"), item.get("where") or []))
            return "\n".join(out)

        def with_carried(handler):
            async def run_both(action: dict) -> str:
                first = carried(action)
                result = await handler(action)
                return f"{first}\n\n{result}" if first else result
            return run_both

        replies = 0

        def status_line() -> str:
            nonlocal replies
            replies += 1
            line = (f"Left: {MAX_TURNS - replies} turns, {budget.left} searches, "
                    f"{max(0, MAX_PAGES - len(pages.fetched))} pages. So far: {store.status()}.")
            # On 2026-11-12 (Q46584 replay) the model read pages for 13 turns
            # looking for one complete list and ended with an empty table,
            # though two pages it had read each stated a row.
            if replies >= MAX_TURNS // 2 and store.row_count() == 0:
                line += ("\nHalf your turns are used and no row has been added. If any page you have "
                         "read states a row, send add_rows now.")
            return line

        # The evidence plan saw the whole question and set the tables up: code
        # defines them and runs the plan's searches, so the model's first turn
        # starts with a fetch or a search instead of planning. A plan without
        # tables (or a failed plan) leaves the first turn to the model as before.
        opening = ""
        if planned["tables"]:
            defined = store.define(planned["tables"], by="the evidence plan")
            searched = await run_search(planned["queries"]) if planned["queries"] else ""
            opening = ("The evidence plan has set up these tables for you (send \"plan\" to change one or add "
                       f"one; say why):\n{defined}" + (f"\n\nIts searches, already run:\n{searched}" if searched else ""))
        outcome = await loop.run(
            system=system,
            opening=opening,
            handlers={**{name: with_carried(handler) for name, handler in (
                          ("plan", plan), ("search", search), ("fetch", fetch), ("text", text),
                          ("find", find), ("rows", rows), ("same_field", same_field_action))},
                      "add_rows": add_rows, "count": count},
            label=LABEL, trace_stage="history", trace_name="history builder",
            max_turns=MAX_TURNS, wall_seconds=WALL_SECONDS,
            meta=lambda: {"pages_fetched": len(pages.fetched), "searches": budget.used,
                          "rows": store.row_count(), "rejected": store.rejected},
            status_line=status_line)
    if outcome.final is not None:
        carried(outcome.final)   # rows and counts sent with the final action
        store.finish(outcome.final)
    logger.info("[history-builder] %d turns, %d searches, %d pages, %d rows kept, %d rejected%s",
                outcome.turns, budget.used, len(pages.fetched), store.row_count(), store.rejected,
                f"; stopped: {outcome.stopped}" if outcome.stopped else "")
    result = BuildResult(
        turns=outcome.turns, searches=budget.used, pages=len(pages.fetched),
        rows={t.id: len(t.rows) for t in store.tables.values()}, rejected=store.rejected,
        notes=len(store.notes), stopped=outcome.stopped, actions=outcome.actions)
    body = store.render()
    if not body:
        return result
    stopped = (f" The run stopped before the model finished ({outcome.stopped}), so the tables may be "
               f"less complete than it intended." if outcome.final is None else "")
    result.section = (
        f"_A SoCLaaS model searched for pages holding past values or past cases and copied rows into "
        f"the tables below ({budget.used} searches, {len(pages.fetched)} pages read). Code checked "
        f"every row: its source page was fetched in this run and the text the model quoted for the "
        f"row is on that page. Code did NOT check that the model read the row correctly or that a "
        f"table is complete. The counts are computed by code from the rows shown. Nothing here is a "
        f"forecast.{stopped}_\n\n{body}"
    )
    return result
