"""The tables a history-builder run fills, and the code checks on them.

The model proposes; this module decides what is kept:

  - a row is kept only if its source page was fetched in this run, its quote
    is on that page, and the quote holds at least one of the row's values;
  - two rows with the same first column and different values are both kept
    and marked as a conflict, and left out of every count;
  - the counts and summary lines are computed here, never by the model.

Nothing here judges what the rows mean for the forecast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import statistics
from typing import Callable

MAX_TABLES = 2
MAX_COLUMNS = 6
MAX_ROWS = 40
MAX_COUNTS = 6       # counts the model may ask for, per table
MAX_NOTES = 8
MAX_NOTE_CHARS = 300
MAX_CELL_CHARS = 120
MIN_QUOTE_CHARS = 4
_OPS = (">=", "<=", "!=", "==", ">", "<", "contains")
_DATE_KEY = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


def _clean(value) -> str:
    return " ".join(str(value if value is not None else "").split())[:MAX_CELL_CHARS]


def _loose(text: str) -> str:
    """For matching a value against a quote: lower case, single spaces, and no
    thousands separators ("1,234" and "1 234" both read 1234)."""
    text = " ".join(str(text or "").lower().split())
    return re.sub(r"(?<=\d)[,   ](?=\d{3}\b)", "", text)


def _plain_number(value: str) -> str | None:
    """A figure as plain digits with a decimal point, or None when the cell is
    not a single number. Reads both "1,234.5" and the continental "1.234,5",
    and takes a lone comma followed by one or two digits ("68,04 %") as a
    decimal comma: on 2026-11-13 Spanish turnout figures written that way were
    read as 6,804."""
    text = re.sub(r"[   ]", "", str(value or "").strip())
    text = re.sub(r"^([-+]?)[$€£]", r"\1", text).rstrip("%")
    if not re.fullmatch(r"[-+]?\d[\d.,]*", text):
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in text:
        whole, _, last = text.rpartition(",")
        text = f"{whole.replace(',', '')}.{last}" if len(last) in (1, 2) and "," not in whole else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    return text if re.fullmatch(r"[-+]?\d+(\.\d+)?", text) else None


def number(value: str) -> float | None:
    plain = _plain_number(value)
    return float(plain) if plain is not None else None


def _same_number(a: str, b: str) -> bool:
    """Two figures that agree once the finer one is rounded to the coarser
    one's decimals: 71.76 and 71.8 are one value reported twice."""
    pa, pb = _plain_number(a), _plain_number(b)
    places = min(len(p.partition(".")[2]) for p in (pa, pb))
    return round(float(pa), places) == round(float(pb), places)


# A text cell longer than this is a description, not a value: it is never
# compared between rows and never tallied.
_SHORT_VALUE_CHARS = 20


def _cells_disagree(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if _plain_number(a) is not None and _plain_number(b) is not None:
        return not _same_number(a, b)
    if len(a) > _SHORT_VALUE_CHARS or len(b) > _SHORT_VALUE_CHARS:
        return False
    return a.lower() != b.lower()


def _fmt(x: float) -> str:
    return f"{x:,.4f}".rstrip("0").rstrip(".") if abs(x) < 1000 else f"{x:,.2f}".rstrip("0").rstrip(".")


@dataclass
class Row:
    values: dict[str, str]
    url: str
    quote: str
    conflict: bool = False
    # Other pages that gave the same row (same values, to rounding).
    also: list[str] = field(default_factory=list)

    def agrees_with(self, values: dict[str, str]) -> bool:
        return not any(_cells_disagree(self.values.get(c, ""), v) for c, v in values.items())


@dataclass
class HistoryTable:
    id: str
    title: str
    row_is: str
    columns: list[str]
    why: str = ""
    # The model says its rows are not on a page as such: it worked each one
    # out from several lines (the highest value in a window, a difference).
    worked_out: bool = False
    rows: list[Row] = field(default_factory=list)
    # The counts the model asked for, as conditions [(column, op, value)]:
    # counted when the report is written, so rows added later are included.
    counts: list[list[tuple[str, str, str]]] = field(default_factory=list)
    coverage: str = ""
    # The model's own statement that the table lists every row of its kind.
    complete: bool = False

    def count_line(self, conditions: list[tuple[str, str, str]]) -> str:
        rows = self.clean_rows()
        matched = sum(1 for r in rows if all(_holds(r.values.get(c, ""), op, v) for c, op, v in conditions))
        return " and ".join(f"{c} {op} {v}" for c, op, v in conditions) + f": {matched} of {len(rows)} rows"

    @property
    def key(self) -> str:
        return self.columns[0]

    def clean_rows(self) -> list[Row]:
        return [r for r in self.rows if not r.conflict]

    def ordered(self) -> list[Row]:
        """Oldest first when every first-column value is a date (YYYY,
        YYYY-MM or YYYY-MM-DD); otherwise in the order added."""
        if self.rows and all(_DATE_KEY.fullmatch(r.values.get(self.key, "")) for r in self.rows):
            return sorted(self.rows, key=lambda r: r.values[self.key])
        return list(self.rows)


class TableStore:
    def __init__(self, has_text: Callable[[str, str], bool]):
        # has_text(url, quote): is the quote on that page, as fetched this run?
        self._has_text = has_text
        self.tables: dict[str, HistoryTable] = {}
        self.notes: list[tuple[str, str]] = []      # (text, url)
        self.rejected = 0
        self.could_not_find = ""

    # -- the model's actions ------------------------------------------------

    def define(self, specs: list) -> str:
        out = []
        for spec in specs if isinstance(specs, list) else []:
            if not isinstance(spec, dict):
                continue
            table_id = _clean(spec.get("id")).upper()[:4] or "ABCD"[len(self.tables) % 4]
            columns = list(dict.fromkeys(_clean(c) for c in spec.get("columns") or [] if _clean(c)))[:MAX_COLUMNS]
            existing = self.tables.get(table_id)
            if existing is not None and existing.rows:
                out.append(f"Table {table_id} already has rows; its definition stays as it is.")
                continue
            if existing is None and len(self.tables) >= MAX_TABLES:
                out.append(f"Table {table_id} not created: at most {MAX_TABLES} tables.")
                continue
            if len(columns) < 2:
                out.append(f"Table {table_id} not created: give at least two columns, the first one "
                           f"naming what identifies a row (usually its date).")
                continue
            self.tables[table_id] = HistoryTable(
                id=table_id, title=_clean(spec.get("title")) or f"Table {table_id}",
                row_is=_clean(spec.get("row_is")), columns=columns, why=_clean(spec.get("why")),
                worked_out=spec.get("worked_out") is True)
            out.append(f"Table {table_id} defined: one row = {self.tables[table_id].row_is or '(not stated)'}; "
                       f"columns: {', '.join(columns)}.")
        return "\n".join(out) or 'No table given. Send "tables": [{"id", "title", "row_is", "columns", "why"}].'

    def add_rows(self, table_id: str, rows: list) -> str:
        table = self.tables.get(str(table_id or "").strip().upper())
        if table is None:
            return (f"No table {table_id!r}. Define it first with the plan action; "
                    f"tables so far: {', '.join(self.tables) or 'none'}.")
        added, problems = 0, []
        for index, raw in enumerate(rows if isinstance(rows, list) else [], start=1):
            if not isinstance(raw, dict) or not isinstance(raw.get("values"), dict):
                problems.append(f"row {index}: not read -- give \"values\", \"url\" and \"quote\"")
                continue
            values = {c: _clean(raw["values"].get(c)) for c in table.columns}
            url, quote = str(raw.get("url") or "").strip(), " ".join(str(raw.get("quote") or "").split())
            label = f"row {index} ({values.get(table.key) or '?'})"
            reason = self._reject_reason(values, table, url, quote)
            if reason:
                self.rejected += 1
                problems.append(f"{label}: REJECTED -- {reason}")
                continue
            same_key = [r for r in table.rows if r.values[table.key].lower() == values[table.key].lower()]
            match = next((r for r in same_key if r.agrees_with(values)), None)
            if match is not None:
                # The same row again, perhaps from another page or with more
                # columns filled: one row, its empty cells filled, both sources.
                filled = [c for c in table.columns if values[c] and not match.values.get(c)]
                for column in filled:
                    match.values[column] = values[column]
                if url != match.url and url not in match.also:
                    match.also.append(url)
                problems.append(f"{label}: already in the table" + (
                    f"; filled in {', '.join(filled)}" if filled else "") + (
                    "; second source recorded" if url != match.url else ""))
                continue
            if len(table.rows) >= MAX_ROWS:
                problems.append(f"{label}: not added -- the table is full ({MAX_ROWS} rows)")
                continue
            # A conflict is two PAGES disagreeing about one row. Two differing
            # rows from one page are two different cases that share a first
            # column (two votes on one day): both stay, unmarked.
            clash = [r for r in same_key if r.url != url]
            row = Row(values=values, url=url, quote=quote, conflict=bool(clash))
            for other in clash:
                other.conflict = True
            table.rows.append(row)
            added += 1
            if clash:
                problems.append(f"{label}: added, but it CONFLICTS with a row from another page for the same "
                                f"{table.key}; both are kept and marked, and left out of the counts")
        head = f"Table {table.id}: {added} row(s) added, {len(table.rows)} of {MAX_ROWS} used."
        return head + ("\n" + "\n".join(problems) if problems else "")

    def _reject_reason(self, values: dict[str, str], table: HistoryTable, url: str, quote: str) -> str:
        if not values.get(table.key):
            return f"no value for the first column ({table.key})"
        if sum(1 for v in values.values() if v) < 2:
            return "only one column filled"
        if not url or len(quote) < MIN_QUOTE_CHARS:
            return "needs the page's url and a quote copied from it"
        if not self._has_text(url, quote):
            return ("the quote was not found on that page as fetched in this run -- copy it exactly "
                    "from a tool result for that page, or fetch the page first")
        loose_quote = _loose(quote)
        present = [v for v in values.values() if v and _loose(v) in loose_quote]
        if not any(len(v) >= 2 for v in present) and not (present and all(len(v) < 2 for v in values.values() if v)):
            return "the quote holds none of this row's values -- quote the text that states them"
        return ""

    def count(self, table_id: str, where: list) -> str:
        table = self.tables.get(str(table_id or "").strip().upper())
        if table is None:
            return f"No table {table_id!r}; tables so far: {', '.join(self.tables) or 'none'}."
        conditions = []
        for cond in where if isinstance(where, list) else []:
            if not isinstance(cond, dict):
                continue
            column, op, value = _clean(cond.get("column")), str(cond.get("op") or "").strip(), _clean(cond.get("value"))
            if column not in table.columns:
                return f"No column {column!r} in table {table.id}; columns: {', '.join(table.columns)}."
            if op not in _OPS:
                return f"Unknown op {op!r}; use one of: {', '.join(_OPS)}."
            conditions.append((column, op, value))
        if not conditions:
            return 'Give "where": [{"column": ..., "op": ..., "value": ...}].'
        line = table.count_line(conditions)
        if conditions not in table.counts:
            if len(table.counts) >= MAX_COUNTS:
                return f"{line}\n(not saved to the report: at most {MAX_COUNTS} counts per table)"
            table.counts.append(conditions)
        return (line + "\n(the report will show this count over the rows the table holds at the end)"
                + ("\n(rows marked as conflicts are left out)" if table.clean_rows() != table.rows else ""))

    def finish(self, final: dict) -> None:
        """Take the coverage statements, the "also noted" list and the gaps
        from the model's final action. A note stays only if its quote is on
        its page."""
        coverage = final.get("coverage")
        if isinstance(coverage, dict):
            for table_id, stated in coverage.items():
                table = self.tables.get(str(table_id).strip().upper())
                if table is None:
                    continue
                # {"complete": true/false, "text": "..."}; a bare string says
                # nothing about completeness, so the table counts as incomplete.
                text = stated.get("text") if isinstance(stated, dict) else stated
                table.complete = isinstance(stated, dict) and stated.get("complete") is True
                table.coverage = " ".join(str(text or "").split())[:400]
        for note in final.get("also_noted") or []:
            if not isinstance(note, dict) or len(self.notes) >= MAX_NOTES:
                continue
            text = " ".join(str(note.get("text") or "").split())[:MAX_NOTE_CHARS]
            url, quote = str(note.get("url") or "").strip(), " ".join(str(note.get("quote") or "").split())
            if text and url and len(quote) >= MIN_QUOTE_CHARS and self._has_text(url, quote):
                self.notes.append((text, url))
            else:
                self.rejected += 1
        self.could_not_find = " ".join(str(final.get("could_not_find") or "").split())[:600]

    # -- what the run reports -------------------------------------------------

    def row_count(self) -> int:
        return sum(len(t.rows) for t in self.tables.values())

    def status(self) -> str:
        return "; ".join(f"table {t.id}: {len(t.rows)} rows" for t in self.tables.values()) or "no table defined yet"

    def render(self) -> str:
        """The tables with rows, as markdown. "" when no row and no note was kept."""
        parts = []
        for table in self.tables.values():
            if not table.rows:
                continue
            sources: dict[str, int] = {}
            lines = [f"### Table {table.id}: {table.title}"]
            if table.row_is:
                lines.append(f"One row = {table.row_is}.")
            if table.why:
                lines.append(f"Why it was built: {table.why}")
            if table.worked_out:
                lines.append("WORKED OUT, NOT COPIED: the research model built each row from several lines of "
                             "the source (for example the highest value in a window). The code check only "
                             "confirms that the figures it started from are on the page, not its working.")
            lines.append(f"Coverage, as the research model states it (not checked): "
                         f"{'COMPLETE list. ' if table.complete else 'NOT a complete list. '}"
                         f"{table.coverage or 'Nothing more stated.'}")
            lines.append("")
            lines.append("| " + " | ".join(table.columns) + " | source |")
            lines.append("|" + " --- |" * (len(table.columns) + 1))
            for row in table.ordered():
                refs = "".join(f"[{sources.setdefault(url, len(sources) + 1)}]" for url in (row.url, *row.also))
                cells = [row.values.get(c, "").replace("|", "/") or "-" for c in table.columns]
                lines.append("| " + " | ".join(cells) + f" | {refs}" + (" CONFLICT" if row.conflict else "") + " |")
            lines.append("")
            conflicts = len(table.rows) - len(table.clean_rows())
            lines.append(f"Rows: {len(table.rows)}" + (
                f", of which {conflicts} are marked CONFLICT (two sources disagree for the same "
                f"{table.key}); those are left out of the lines below." if conflicts else "."))
            caution = ("" if table.complete else
                       " (the table is not a complete list: these describe the rows found, and rows are "
                       "often missing for a reason, so do not read them as how often something happens)")
            computed = _computed_lines(table)
            if computed:
                lines.append(f"Computed by code from the rows above{caution}:")
                lines.extend(f"- {line}" for line in computed)
            if table.counts:
                lines.append(f"Counts the research model asked for, computed by code{caution}:")
                lines.extend(f"- {table.count_line(conditions)}" for conditions in table.counts)
            gaps = _missing_years(table)
            if gaps:
                lines.append(gaps)
            lines.append("Sources: " + "; ".join(f"[{n}] {url}" for url, n in sources.items()))
            parts.append("\n".join(lines))
        if self.notes:
            parts.append("### Also noted while reading\n" + "\n".join(f"- {text} ({url})" for text, url in self.notes))
        if not parts:
            return ""
        if self.could_not_find:
            parts.append(f"### Could not find\n{self.could_not_find}")
        return "\n\n".join(parts)


def _holds(cell: str, op: str, value: str) -> bool:
    if op == "contains":
        return value.lower() in cell.lower()
    a, b = number(cell), number(value)
    if a is None or b is None:
        if not cell:
            return False
        a, b = cell.lower(), value.lower()
    return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b, "!=": a != b}[op]


def _is_year(value: str) -> bool:
    return bool(re.fullmatch(r"(1[89]|20)\d\d", value.strip()))


def _missing_years(table: HistoryTable) -> str:
    """For a table with about one row a year: the years in its span that have
    no row. On 2026-11-13 (Q46584) a budget table held the six late years of
    2014-2026 and none of the on-time ones, because the page it came from
    was about late budgets; the gap list makes that visible."""
    keys = [r.values.get(table.key, "") for r in table.rows]
    years = {int(k[:4]) for k in keys if _DATE_KEY.fullmatch(k)}
    if len(years) < 3 or len(years) < len(keys) * 0.7:
        return ""
    span = range(min(years), max(years) + 1)
    missing = [y for y in span if y not in years]
    # A first column of bare years is a table built one row per year, so any
    # gap counts. Rows keyed by full dates may be events that do not happen
    # every year (elections): gaps are listed only when most years have a row.
    yearly = all(_is_year(k) for k in keys) or len(years) >= 0.5 * len(span)
    if not missing or not yearly or len(missing) > 40:
        return ""
    return (f"Years from {span[0]} to {span[-1]} with no row (found by code): "
            + ", ".join(str(y) for y in missing))


def _computed_lines(table: HistoryTable) -> list[str]:
    """Per column: the range of a number column, or how often each value of a
    short-list column occurs. Plain descriptions of the rows, no rates."""
    rows = [r for r in table.ordered() if not r.conflict]
    lines = []
    for column in table.columns[1:]:
        cells = [(r.values.get(table.key, ""), r.values.get(column, "")) for r in rows if r.values.get(column)]
        if len(cells) < 2 or all(_is_year(v) for _, v in cells):
            continue   # a column of years has no lowest / middle / highest worth stating
        numbers = [(k, number(v)) for k, v in cells]
        numeric = [(k, n) for k, n in numbers if n is not None]
        if len(numeric) >= 3 and len(numeric) >= 0.8 * len(cells):
            values = [n for _, n in numeric]
            lines.append(f"{column}: {len(values)} values; lowest {_fmt(min(values))}, middle (median) "
                         f"{_fmt(statistics.median(values))}, highest {_fmt(max(values))}; "
                         f"last row {_fmt(numeric[-1][1])} ({numeric[-1][0]})")
            continue
        tally: dict[str, int] = {}
        for _, v in cells:
            tally[v.lower()] = tally.get(v.lower(), 0) + 1
        if len(tally) <= 6 and len(cells) > len(tally) and all(len(v) <= _SHORT_VALUE_CHARS for v in tally):
            lines.append(f"{column}: " + ", ".join(f"{v} x{n}" for v, n in sorted(tally.items(), key=lambda i: -i[1]))
                         + f" (of {len(cells)} rows)")
    return lines
