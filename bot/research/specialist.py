"""Specialist agents: up to two small research agents the artifact check sends
after it has read the research (MPE: Artifact Check V1 -> Specialist Agents,
beside the Focused Retry; their sections then go to Check V2 and the brief).

Why (2026-11-19): the check could only ask for more web searches. A search
returns snippets and a few scraped pages; it cannot build a table from twenty
pages or dig one quoted figure out of a long document. The history builder can,
but it runs once, early, on the evidence plan's idea of what is missing. The
check knows what is still missing after everything ran, so it may now send an
agent with one job:

  - "table": a table it names (one row = ..., columns), built like a history
    table: rows copied from pages, each row's quote checked on its page by code.
  - "quotes": a specific fact, returned as passages copied word for word from
    pages, each checked on its page by code. Readers see only the quotes.

Nothing a specialist reports is in its own words: rows and quotes are text
code found on the page (user rule: a weaker model rewording a sentence can
change its meaning). Its limits are smaller than the history builder's, and it
uses the same tools (search, page tools with the fetch method shown, table
store, turn loop). Runs only on SoCLaaS: never a paid model call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
import os

from bot.llm import provider as llm_provider
from bot.research.agents.page_tools import PageReader
from bot.research.history import history_builder
from bot.research.history.table_store import MAX_COLUMNS, MAX_NOTE_QUOTE_CHARS, MIN_NOTE_QUOTE_CHARS, TableStore
from bot.research.search import search_service
from bot.trace import source_ledger

logger = logging.getLogger(__name__)

SECTION_PREFIX = "Specialist Agent"
LABEL = "specialist-agent"
MAX_AGENTS = 2
KINDS = ("table", "quotes")
MAX_TURNS = int(os.getenv("SPECIALIST_MAX_TURNS", "10"))
MAX_PAGES = int(os.getenv("SPECIALIST_MAX_PAGES", "15"))
MAX_SEARCHES = int(os.getenv("SPECIALIST_MAX_SEARCHES", "4"))
MAX_PAID_FETCHES = int(os.getenv("SPECIALIST_MAX_PAID_FETCHES", "3"))
MAX_QUOTES = 12
WALL_SECONDS = float(os.getenv("SPECIALIST_TIMEOUT_SECONDS", "900"))
_MAX_TASK_CHARS = 400


def enabled() -> bool:
    """SPECIALIST_AGENTS = on / off; unset follows the history builder (whose
    tools it uses), so it needs a SoCLaaS key and never runs on a paid model."""
    mode = os.getenv("SPECIALIST_AGENTS", "").strip().lower()
    if mode in {"0", "off", "false", "no"}:
        return False
    if not mode:
        return history_builder.enabled()
    return bool(llm_provider.api_key_for(llm_provider.ENDPOINT_SOCLAAS))


@dataclass
class Task:
    kind: str           # "table" or "quotes"
    task: str           # what to find, in the check's words
    why: str = ""
    row_is: str = ""    # table only
    columns: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()

    def short(self) -> str:
        return " ".join(self.task.split())[:70]


def parse_tasks(retry_plan: dict | None) -> list[Task]:
    """The check's "agents" list, within limits: at most MAX_AGENTS, a known
    kind, a task, and for a table 2-6 columns (else it is sent as "quotes")."""
    entries = (retry_plan or {}).get("agents")
    tasks: list[Task] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or len(tasks) >= MAX_AGENTS:
            continue
        task = " ".join(str(entry.get("task") or "").split())[:_MAX_TASK_CHARS]
        if not task:
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        columns = tuple(dict.fromkeys(" ".join(str(c).split()) for c in entry.get("columns") or []
                                      if str(c).strip()))[:MAX_COLUMNS]
        if kind == "table" and len(columns) < 2:
            kind = "quotes"
        if kind not in KINDS:
            continue
        queries = tuple(str(q).strip() for q in entry.get("queries") or [] if str(q).strip())[:3]
        tasks.append(Task(kind=kind, task=task, why=" ".join(str(entry.get("why") or "").split())[:300],
                          row_is=" ".join(str(entry.get("row_is") or "").split())[:200] if kind == "table" else "",
                          columns=columns if kind == "table" else (), queries=queries))
    return tasks


def _system_prompt(*, task: Task, title: str, resolution_criteria: str, fine_print: str,
                   found: list[str], already_tried: str, today: str) -> str:
    found_block = "\n".join(f"- {line}" for line in found) or "- (none)"
    if task.kind == "table":
        job = f"""YOUR JOB: build the table already set up for you (Table A), copying rows from pages you
read. One row = {task.row_is or 'one case'}. Rows go in with add_rows (each with the page's address and a
quote copied exactly from that page); code checks every quote on its page. A passage that bears on the
task but is not a row may be kept with "quotes" (below)."""
    else:
        job = f"""YOUR JOB: find the passages on the web that state what the task asks for, and keep them with
"quotes". Each quote is the whole sentence or two holding the fact ({MIN_NOTE_QUOTE_CHARS}-{MAX_NOTE_QUOTE_CHARS} characters), copied
word for word from a page you fetched: do not reword, shorten inside or join separate lines. Code
checks each quote on its page; readers see only the quotes and their pages, never your words.
Keep the date the page gives for the fact inside the quote when it is there. At most {MAX_QUOTES} quotes.
If you find a table of values, you may also define a table ("plan") and copy its rows."""
    return f"""You are a specialist research step of a forecasting bot. Today is {today}.

Question: {title}
Resolution criteria: {resolution_criteria}
{('Fine print: ' + fine_print) if fine_print.strip() else ''}

The bot's research has already run. A check that read all of it sent you to find ONE thing it still lacks:
TASK: {task.task}
{('Why it matters: ' + task.why) if task.why else ''}

{job}

You do NOT forecast. Never write a probability, an estimate or an opinion on the answer.

What research has already tried (do not repeat these searches; a page listed as failed is tried again
only by a plain download, so look for the same fact on another page or in another format):
{already_tried}

Pages the bot's searches found:
{found_block}

Reply with exactly ONE JSON object per turn, nothing else. Actions:
  {{"action": "search", "queries": ["..."]}}   web search, up to {history_builder.MAX_QUERIES_PER_SEARCH} queries a turn, {MAX_SEARCHES} in total.
  {{"action": "fetch", "urls": ["..."]}}       read pages or documents ({MAX_PAGES} in total). Each result says
      "read by:": the method that served the page. A Wayback snapshot is an archived copy that may be
      older than the live page; a site reader's extract is not the page itself.
  {{"action": "text", "url": "...", "from": 0}}   more of one page's text.
  {{"action": "find", "url": "...", "word": "..."}}   every place a word occurs on a page.
  {{"action": "rows", "table": "P1.T2", "contains": "optional word"}}   rows of a table on a page.
  {{"action": "plan", "tables": [{{"id": "A", "title": "...", "row_is": "one ...", "columns": ["..."], "reason": "..."}}]}}
      define or change a table (say why in "reason"; the change is recorded).
  {{"action": "add_rows", "table": "A", "rows": [{{"values": {{"column": "value"}}, "url": "...", "quote": "..."}}]}}
  {{"action": "add_quotes", "quotes": [{{"url": "...", "quote": "..."}}]}}
  Any action may carry "add_rows": [{{"table": "A", "rows": [...]}}] and "quotes": [...] to save a turn.
  {{"action": "final", "coverage": {{"A": {{"complete": false, "text": "..."}}}}, "could_not_find": "..."}}
      ends the work: what the table covers, and what you looked for and did not get.

You have {MAX_TURNS} turns. Rules:
- Rows and quotes come only from pages you fetched in this run, copied exactly. Never from memory.
- Keep what you find as soon as you read it (carry it with your next action); unkept finds are lost.
- Only facts that exist today. Never a forecast or a value for a date after today as if it were a fact.
- Stop when the task is done, or when more searching stops finding anything new."""


async def run(task: Task, *, title: str, resolution_criteria: str = "", fine_print: str = "",
              already_tried: str = "", target_date: str = "", today: date | None = None) -> str:
    """One specialist's section, or "" when it kept nothing."""
    today = today or date.today()
    source_ledger.set_source_context(f"{SECTION_PREFIX} ({task.kind})", "artifact retry")
    pages = PageReader(None, engine=LABEL, max_pages=MAX_PAGES, max_paid_fetches=MAX_PAID_FETCHES)
    store = TableStore(pages.has_text, target_date, max_notes=MAX_QUOTES,
                       notes_heading="Quotes found" if task.kind == "quotes" else "Also noted while reading")
    budget = search_service.SearchBudget(MAX_SEARCHES)
    tables = ([{"id": "A", "title": task.task[:120], "row_is": task.row_is, "columns": list(task.columns),
                "why": task.why}] if task.kind == "table" else [])
    outcome = await history_builder.drive(
        system=_system_prompt(task=task, title=title, resolution_criteria=resolution_criteria,
                              fine_print=fine_print, found=history_builder.start_hits(),
                              already_tried=already_tried or "(not listed)", today=today.isoformat()),
        store=store, pages=pages, budget=budget, today=today, label=LABEL, trace_stage="specialist",
        trace_name=f"specialist agent: {task.short()}", search_reason="specialist agent",
        max_turns=MAX_TURNS, max_pages=MAX_PAGES, wall_seconds=WALL_SECONDS,
        tables=tables, queries=list(task.queries), set_up_by="the artifact check",
        opening_note=f"Your task: {task.task}")
    logger.info("[specialist] %s (%s): %d turns, %d searches, %d pages, %d rows, %d quotes, %d rejected%s",
                task.short(), task.kind, outcome.turns, budget.used, len(pages.fetched), store.row_count(),
                len(store.notes), store.rejected, f"; stopped: {outcome.stopped}" if outcome.stopped else "")
    body = store.render()
    if not body:
        return ""
    stopped = (f" The run stopped before the agent finished ({outcome.stopped})." if outcome.final is None else "")
    what = ("copied rows into the table below" if task.kind == "table"
            else "copied the passages below word for word")
    return (f"_The artifact check, after reading all the research, sent a SoCLaaS agent to find one thing it "
            f"still lacked: \"{task.task}\". The agent {what} from pages it fetched in this run "
            f"({budget.used} searches, {len(pages.fetched)} pages read). Code found every row's quote and "
            f"every quote on its page. Code did NOT check that the agent read a row correctly or that the "
            f"finds are complete. Nothing here is a forecast.{stopped}_\n\n{body}")
