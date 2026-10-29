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
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import hashlib
import html as html_module
import json
import re

NEXT_F = re.compile(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)')
JSON_SCRIPT = re.compile(
    r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>'
    r'|<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
TAG = re.compile(r"<[^>]+>")
ISO_DATE = re.compile(r"^(?:\$D)?(\d{4}-\d{2}-\d{2})")
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

    def all_rows(self) -> str:
        return f"[{self.id}] from {self.url}\n" + "\n".join(compact(r) for r in self.rows)


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
                try:
                    return date.fromisoformat(match.group(1))
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


def extract_tables(url: str, page_html: str, id_prefix: str = "T") -> list[Table]:
    """Every table embedded in one page, in page order, de-duplicated."""
    tables, seen, candidates = [], set(), []
    for text in _payloads(page_html):
        for rows, prefix in _arrays_of_objects(text):
            candidates.append((rows, prefix))
            candidates.extend((inner, prefix) for inner in _nested_arrays(rows))
    for rows, prefix in candidates:
        columns = _columns(rows)
        if not columns:
            continue
        digest = hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        tables.append(Table(f"{id_prefix}{len(tables) + 1}", url, rows, columns,
                            _context(prefix), _value_counts(rows, columns)))
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
    out, used = [], 0
    for *_, table in ranked:
        block = f"From {table.url}\n{table.summary(today)}"
        if used + len(block) > max_chars:
            continue
        out.append(block)
        used += len(block)
    omitted = sum(len(ts) for _, ts in pages) - len(out)
    if omitted > 0:
        out.append(f"_{omitted} further embedded table(s) omitted for length (ranked less relevant)._")
    return "\n\n".join(out)
