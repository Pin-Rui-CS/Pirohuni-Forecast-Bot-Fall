"""Data tables hidden in a page's source. Plain parsing: no LLM, no site rules.

Motivating failure (question 46422, 2026-10-22): the Swiss drought platform
draws its map and charts from data that ships inside the HTML (Next.js
streaming chunks). The bot scraped the platform eight times; the markdown
scraper kept only the visible text, and series_discovery found no >= 100-row
time series, so the brief said "no current count was retrieved". The page
source held the class of all 38 regions (17 at "Extremely dry") and each
region page held a 28-week series ending in the platform's own forecast to the
resolution week.

  1. collect the page's script payloads: Next.js streaming chunks
     (``self.__next_f.push([1,"..."])``), ``__NEXT_DATA__``, and
     ``<script type="application/json">`` / ``ld+json`` blocks;
  2. find every JSON array of objects inside them (and one level of arrays
     nested in their rows);
  3. keep arrays of >= 3 objects sharing >= 2 keys -- a table -- however short;
  4. describe each compactly: rows, columns, value counts of low-cardinality
     columns, first and last rows, with rows dated after the fetch marked as
     future (a forecast or a schedule, never a measurement).

Ordinary HTML <table> elements are read too. Question 46519 (2026-10-31): WHO's
list of medicines under prequalification assessment is one, and the explorer
reported "0 embedded tables" for it, so no forecaster learned that moxidectin
was not under assessment.

A data file (CSV, TSV, JSON) is itself the table. Question 46516 (2026-10-30):
the explorer fetched C3S's November series CSV, 1940-2025, holding the record
to beat, and got "0 embedded tables" plus a 700-char excerpt of the file's
header comments.
"""
from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass, field
from datetime import date
import hashlib
import html as html_module
from html.parser import HTMLParser
import io
import json
import re

NEXT_F = re.compile(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)')
JSON_SCRIPT = re.compile(
    r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>'
    r'|<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
TAG = re.compile(r"<[^>]+>")
# A day or a month ("1940-11" in monthly series), optionally Next.js-marked.
ISO_DATE = re.compile(r"^(?:\$D)?(\d{4})-(\d{2})(?:-(\d{2}))?(?!\d)")
MARKUP = re.compile(r"<(?:!doctype|html|head|body|div|script|p|table)\b", re.I)
DELIMITERS = ("\t", ",", ";", "|")
MAX_NOTES_CHARS = 600
WORD = re.compile(r"[a-z]{4,}")
# Rendering plumbing, not data: map-style layers and bare shape lists. Kept,
# but ranked below tables of values. A table that merely HAS a shape column
# (regions with their outlines and their drought class) is not penalised.
STYLE_KEYS = {"paint", "layout", "minzoom", "maxzoom", "source-layer"}
SHAPE_ONLY = {"type", "id", "properties", "geometry"}
MIN_ROWS = 3
MAX_CELL = 80
MAX_SCAN_CHARS = 8_000_000


@dataclass
class Table:
    id: str
    url: str
    rows: list[dict]
    columns: list[str]
    context: str = ""
    value_counts: dict[str, dict] = field(default_factory=dict)

    def future_rows(self, today: date) -> int:
        return sum(1 for row in self.rows if (_row_date(row) or date.min) > today)

    def summary(self, today: date | None = None, head: int = 2, tail: int = 6) -> str:
        today = today or date.today()
        n = len(self.rows)
        future = self.future_rows(today)
        lines = [f"[{self.id}] {n} rows; columns: {', '.join(self.columns)}"
                 + (f"; {future} row(s) dated after {today.isoformat()} (marked FUTURE)" if future else "")]
        if self.context:
            lines.append(f"  context: {self.context}")
        for col, counts in self.value_counts.items():
            lines.append(f"  {col} value counts: {json.dumps(counts, ensure_ascii=False)}")
        shown = self.rows if n <= head + tail else self.rows[:head] + [None] + self.rows[-tail:]
        for row in shown:
            if row is None:
                lines.append("  ...")
                continue
            when = _row_date(row)
            lines.append(("  FUTURE " if when and when > today else "  ") + compact(row))
        return "\n".join(lines)

    def matching_rows(self, word: str, max_chars: int | None = None) -> str:
        """The rows holding ``word`` (any case), with the count: "is X listed?"."""
        needle = word.strip().lower()
        hits = [compact(r) for r in self.rows
                if needle in json.dumps(r, ensure_ascii=False, default=str).lower()]
        head = f"[{self.id}] from {self.url}\n{len(hits)} of {len(self.rows)} rows contain {word.strip()!r}"
        text = head + ("\n" + "\n".join(hits) if hits else "")
        return text if max_chars is None else text[:max_chars]

    @property
    def digest(self) -> str:
        return hashlib.sha1(json.dumps(self.rows, sort_keys=True, default=str).encode()).hexdigest()

    def all_rows(self, max_chars: int | None = None) -> str:
        """Every row. Past max_chars the newest end of a dated series is kept:
        a cut from the top showed a daily file's 1940s and lost 2026."""
        head = f"[{self.id}] from {self.url}"
        lines = [compact(r) for r in self.rows]
        text = head + "\n" + "\n".join(lines)
        if max_chars is None or len(text) <= max_chars:
            return text
        first, last = (_row_date(self.rows[0]), _row_date(self.rows[-1])) if self.rows else (None, None)
        newest_first = bool(first and last and first > last)
        ordered = lines if newest_first else lines[::-1]
        kept, used = [], len(head) + 100
        for line in ordered:
            if used + len(line) + 1 > max_chars:
                break
            kept.append(line)
            used += len(line) + 1
        if not newest_first:
            kept.reverse()
        where = "last" if newest_first else "first"
        return (f"{head}\n[{where} {len(lines) - len(kept):,} of {len(lines):,} rows omitted for length]\n"
                + "\n".join(kept))


def compact(value) -> str:
    """One-line JSON with long or deeply nested values cut down."""
    def shrink(v, depth):
        if isinstance(v, str):
            v = v[2:] if v.startswith("$D") else v  # Next.js date marker
            return v if len(v) <= MAX_CELL else v[:MAX_CELL] + "..."
        if isinstance(v, dict):
            return {k: shrink(x, depth + 1) for k, x in v.items()} if depth < 2 else "{...}"
        if isinstance(v, list):
            if depth >= 2:
                return "[...]"
            return [shrink(x, depth + 1) for x in v[:4]] + (["..."] if len(v) > 4 else [])
        return v
    return json.dumps(shrink(value, 0), ensure_ascii=False)


def _row_date(row: dict) -> date | None:
    for value in row.values():
        if isinstance(value, str):
            match = ISO_DATE.match(value)
            if match:
                year, month, day = match.groups()
                try:
                    return date(int(year), int(month), int(day or 1))
                except ValueError:
                    return None
    return None


def _payloads(page_html: str) -> list[str]:
    out = []
    for raw in NEXT_F.findall(page_html):
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    for a, b in JSON_SCRIPT.findall(page_html):
        out.append(html_module.unescape(a or b))
    return out


def _arrays_of_objects(text: str) -> list[tuple[list, str]]:
    """Every JSON array of objects in ``text``, with ~200 chars of text before it."""
    decoder = json.JSONDecoder()
    found, i = [], 0
    text = text[:MAX_SCAN_CHARS]
    while True:
        i = text.find("[{", i)
        if i < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 2
            continue
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            found.append((value, text[max(0, i - 200):i]))
            i = end  # nested arrays are reached through their parent
        else:
            i += 2


def _nested_arrays(rows: list) -> list[list]:
    out = []
    for row in rows:
        for value in row.values():
            if isinstance(value, list) and len(value) >= MIN_ROWS and all(isinstance(x, dict) for x in value):
                out.append(value)
    return out


def _context(prefix: str) -> str:
    words = re.findall(r'"([A-Za-z][A-Za-z ]{2,40})"', prefix)
    return " / ".join(words[-4:])


def _columns(rows: list) -> list[str] | None:
    if len(rows) < MIN_ROWS:
        return None
    if len(set.intersection(*(set(r) for r in rows))) < 2:
        return None
    return list(dict.fromkeys(k for r in rows for k in r))


def _value_counts(rows: list[dict], columns: list[str]) -> dict[str, dict]:
    out = {}
    for col in columns:
        values = [r.get(col) for r in rows]
        if not all(isinstance(v, (int, float, str, bool, type(None))) for v in values):
            continue
        counts = Counter(values)
        # Identifier-like columns (every value distinct) say nothing as counts.
        if 1 < len(counts) <= 8 and len(rows) >= 5 and max(counts.values()) > 1:
            out[col] = dict(sorted((str(k), v) for k, v in counts.items()))
    return out


def _delimited_table(text: str) -> tuple[list[dict], str] | None:
    """(rows, notes) of a CSV/TSV body, or None if it is not one. Leading
    "#" lines are notes (units, column meanings); the first other line must be
    a header of distinct names, and nearly every line must have its width."""
    lines, notes = text[:MAX_SCAN_CHARS].splitlines(), []
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        notes.append(lines.pop(0).strip().lstrip("#").strip())
    if len(lines) <= MIN_ROWS:
        return None
    delimiter = max(DELIMITERS, key=lines[0].count)
    if not lines[0].count(delimiter):
        return None
    # Parsed as one text so a quoted cell may span lines.
    parsed = [cells for cells in csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
              if any(c.strip() for c in cells)]
    header = [h.strip() for h in parsed[0]]
    if len(header) < 2 or not all(header) or len(set(header)) < len(header):
        return None
    if sum(bool(_NUMERIC.match(h)) for h in header) > len(header) // 2:
        return None  # the first line is data, not column names
    body = [cells for cells in parsed[1:] if len(cells) == len(header)]
    # A footnote or two is tolerated; more ragged lines mean this is prose.
    if len(body) < MIN_ROWS or len(body) < 0.95 * (len(parsed) - 1):
        return None
    rows = [{h: c.strip() for h, c in zip(header, cells)} for cells in body]
    # Column meanings and units first: they survive the cut, a long
    # description of the file need not.
    defining = [n for n in notes if n.split(":")[0].strip() in header or n.lower().startswith("units")]
    notes = defining + [n for n in notes if n and n not in defining]
    return rows, " / ".join(notes)[:MAX_NOTES_CHARS]


class _HtmlTables(HTMLParser):
    """Cell text of every <table>, row by row, each cell flagged if it is a
    <th>. A nested table is read as a table of its own. Omitted </td>/</tr>
    (legal HTML) close at the next cell or row."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[list[list[tuple[str, bool]]], str]] = []
        self._open: list[dict] = []
        self._skip = 0  # inside <script>/<style>

    def _close_cell(self, t: dict) -> None:
        if t["cell"] is not None and t["row"] is not None:
            t["row"].append((" ".join("".join(t["cell"]).split()), t["th"]))
        t["cell"] = None

    def _close_row(self, t: dict) -> None:
        self._close_cell(t)
        if t["row"]:
            t["rows"].append(t["row"])
        t["row"] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._open.append({"rows": [], "row": None, "cell": None, "th": False, "caption": None})
        elif self._open:
            t = self._open[-1]
            if tag == "tr":
                self._close_row(t)
                t["row"] = []
            elif tag in ("td", "th"):
                self._close_cell(t)
                if t["row"] is None:
                    t["row"] = []
                t["cell"], t["th"] = [], tag == "th"
            elif tag == "caption":
                t["caption"] = []
            elif tag == "br" and t["cell"] is not None:
                t["cell"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif self._open:
            t = self._open[-1]
            if tag in ("td", "th"):
                self._close_cell(t)
            elif tag == "tr":
                self._close_row(t)
            elif tag == "table":
                self._close_row(t)
                self._open.pop()
                caption = " ".join("".join(t["caption"] or []).split())
                self.tables.append((t["rows"], caption))

    def handle_data(self, data):
        if self._skip or not self._open:
            return
        t = self._open[-1]
        if t["cell"] is not None:
            t["cell"].append(data)
        elif t["caption"] is not None and t["row"] is None:
            t["caption"].append(data)


def _html_tables(page_html: str) -> list[tuple[list[dict], str]]:
    """(rows, caption) of each <table> with a header and >= MIN_ROWS rows of
    its width. The header is the first all-<th> row among the first three, else
    the first row; rows of another width (section headings) are left out."""
    parser = _HtmlTables()
    try:
        parser.feed(page_html[:MAX_SCAN_CHARS])
        parser.close()
    except Exception:  # malformed markup: keep whatever was read
        pass
    out = []
    for rows, caption in parser.tables:
        rows = [r for r in rows if any(text for text, _ in r)]
        if len(rows) <= MIN_ROWS:
            continue
        at = next((i for i, r in enumerate(rows[:3]) if all(th for _, th in r)), 0)
        names, seen = [], Counter()
        for i, (text, _) in enumerate(rows[at]):
            name = text or f"column {i + 1}"
            seen[name] += 1
            names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
        if len(names) < 2:
            continue
        body = [r for r in rows[at + 1:] if len(r) == len(names)]
        if len(body) >= MIN_ROWS:
            out.append(([{n: text for n, (text, _) in zip(names, r)} for r in body], caption))
    return out


def extract_tables(url: str, page_html: str, id_prefix: str = "T") -> list[Table]:
    """Every table in one page, in page order, de-duplicated: the page's HTML
    tables, then the JSON arrays in its scripts; or a data file (CSV, TSV,
    JSON) itself."""
    tables, seen, candidates = [], set(), []
    markup = bool(MARKUP.search(page_html[:5000]))
    if markup:
        candidates.extend(_html_tables(page_html))
    else:
        delimited = _delimited_table(page_html)
        if delimited:
            candidates.append(delimited)
    for text in _payloads(page_html) if markup else [page_html]:
        for rows, prefix in _arrays_of_objects(text):
            candidates.append((rows, _context(prefix)))
            candidates.extend((inner, _context(prefix)) for inner in _nested_arrays(rows))
    for rows, context in candidates:
        columns = _columns(rows)
        if not columns:
            continue
        table = Table(f"{id_prefix}{len(tables) + 1}", url, rows, columns, context)
        if table.digest in seen:
            continue
        seen.add(table.digest)
        table.value_counts = _value_counts(rows, columns)
        tables.append(table)
    return tables


def visible_text(page_html: str, limit: int = 800) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page_html, flags=re.S)
    return re.sub(r"\s+", " ", html_module.unescape(TAG.sub(" ", body))).strip()[:limit]


def relevance(table: Table, question_words: set[str], today: date) -> float:
    """Rank for a size-capped section: on-topic, value-bearing, dated tables first."""
    text = (" ".join(table.columns) + " " + table.context + " "
            + " ".join(compact(r) for r in table.rows[:3])).lower()
    score = len(question_words & set(WORD.findall(text)))
    # Value counts are where a snapshot's answer shows ("cdi": {"5": 17, ...}).
    score += 3 * bool(table.value_counts) + 2 * any(_row_date(r) for r in table.rows[:3])
    score += 3 * bool(table.future_rows(today))
    columns = {c.lower() for c in table.columns}
    if STYLE_KEYS & columns:
        score -= 8
    if columns <= SHAPE_ONLY:
        score -= 4
    return score


# Words too common in question titles to say anything about a table.
STOP_WORDS = {
    "will", "what", "which", "when", "many", "much", "more", "most", "least", "than", "that",
    "this", "with", "from", "have", "been", "after", "before", "between", "according", "during",
    "total", "number", "there", "their", "they", "into", "over", "under", "above", "below",
    "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december",
}
_NUMERIC = re.compile(r"^-?\d+(?:[.,]\d+)?%?$")


def _is_id_or_order_column(name: str) -> bool:
    # Ids and display order (Nate Silver's menu carries "sibling_rank") are
    # numbers that measure nothing.
    lowered = name.lower()
    return (lowered == "id" or lowered.endswith("id")
            or lowered.endswith(("rank", "order", "position", "index", "sort")))


def _measure_columns(table: Table) -> list[str]:
    """Columns that hold measurements: mostly numbers, more than one value, not an id."""
    out = []
    for col in table.columns:
        if _is_id_or_order_column(col):
            continue
        values = [r.get(col) for r in table.rows if r.get(col) not in (None, "")]
        if len(values) < MIN_ROWS:
            continue
        numeric = [v for v in values if (isinstance(v, (int, float)) and not isinstance(v, bool))
                   or (isinstance(v, str) and _NUMERIC.match(v.strip()))]
        if len(numeric) >= 0.8 * len(values) and len({str(v) for v in numeric}) > 1:
            out.append(col)
    return out


def _is_link_list(table: Table) -> bool:
    """A menu or list of links: some column is mostly URLs or site paths."""
    for col in table.columns:
        values = [r.get(col) for r in table.rows if isinstance(r.get(col), str)]
        links = [v for v in values if v.startswith(("http://", "https://", "/"))]
        if len(values) >= MIN_ROWS and len(links) >= 0.8 * len(values):
            return True
    return False


def keep_tables(url: str, tables: list[Table], question_text: str,
                today: date | None = None) -> list[Table]:
    """The tables worth showing; site plumbing is dropped, not just ranked low.

    The 2026-10-26 sweep of 101 past questions found 20 sections, one of them
    useful: the rest were menus, link lists, schema.org page metadata and
    Yahoo's trending-ticker widget. A table survives only if it holds
    measurements (a numeric column that is not an id or a display order) or
    rows after today, and either carries dates, category counts of a
    measurement, or rows after today, or shares a word with the question. Market and quote sites are skipped: their adapters read the data,
    and their pages are ticker widgets.
    """
    from bot.scrapers.adapters.Wayback import is_market_data_url

    if is_market_data_url(url):
        return []
    today = today or date.today()
    words = set(WORD.findall(question_text.lower())) - STOP_WORDS
    kept = []
    for table in tables:
        columns = {c.lower() for c in table.columns}
        if any(c.startswith("@") for c in columns) or STYLE_KEYS & columns or columns <= SHAPE_ONLY:
            continue
        measures = _measure_columns(table)
        future = table.future_rows(today) > 0
        # Rows after today are a schedule or the source's own forecast, worth
        # keeping even without a number (a fixture list of kick-off times).
        if not measures and not future:
            continue
        dated = any(_row_date(r) for r in table.rows[:3])
        strong = dated or future or any(col in table.value_counts for col in measures)
        if _is_link_list(table) and not strong:
            continue
        text = (" ".join(table.columns) + " " + table.context + " "
                + " ".join(compact(r) for r in table.rows[:3])).lower()
        if strong or words & set(WORD.findall(text)):
            kept.append(table)
    return kept


def render_section(pages: list[tuple[str, list[Table]]], question_text: str,
                   max_chars: int = 9000, today: date | None = None) -> str:
    """The research-section body for tables found on the resolution source."""
    today = today or date.today()
    words = set(WORD.findall(question_text.lower()))
    # The first page is the source the question names; its tables break ties.
    ranked = sorted(((relevance(t, words, today) + 2 * (p == 0), p, i, t)
                     for p, (_, ts) in enumerate(pages) for i, t in enumerate(ts)),
                    key=lambda x: (-x[0], x[1], x[2]))
    out, used, shown = [], 0, set()
    for *_, table in ranked:
        # A site-wide table (cookie settings, a menu) repeats on every page.
        if table.digest in shown:
            continue
        shown.add(table.digest)
        block = f"From {table.url}\n{table.summary(today)}"
        if used + len(block) > max_chars:
            continue
        out.append(block)
        used += len(block)
    omitted = sum(len(ts) for _, ts in pages) - len(out)
    if omitted > 0:
        out.append(f"_{omitted} further embedded table(s) omitted for length (ranked less relevant)._")
    return "\n\n".join(out)
