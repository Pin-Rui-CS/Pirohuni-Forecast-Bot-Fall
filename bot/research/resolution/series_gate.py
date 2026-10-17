"""Deterministic detection and parsing of a historical numeric series.

No LLM calls, no network, no browser: this is pure text/JSON parsing, so it can
run on every resolution-source fetch without touching the OpenRouter or
Firecrawl budgets.

ONE parser serves both jobs -- the retrieval gate ("did we actually end up
holding a series?") and the reducer ("compute statistics from it"). They must
never disagree about what counts as data. During the 2026-07-29 measurements a
separate, narrower gate rejected three payloads that the discovery step had
retrieved *correctly*:

  * Our World in Data      -- periods are bare years (``1950``), not ISO dates
  * Silver Bulletin        -- dates are ``1/21/2025``, not ``01/21/2025``
  * Silver Bulletin (CSV)  -- the payload was CSV, which wasn't parsed at all

Every one of those looked identical to a genuine retrieval failure from the
outside. Hence the deliberately permissive period recognition below, and
``rejection_reason`` on the result: a bare "no series here" is never enough
information to tell a real miss from a parser that is too narrow.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

# A "historical trend" needs enough points to measure dispersion, not just a
# direction. 100 keeps daily trackers (>3 months) and long annual series while
# rejecting the handful of summary counters a rendered page exposes -- the
# 44875 failure mode, where a JS render returned three aggregate numbers and
# the pipeline treated that as success.
MIN_SERIES_ROWS = 100

_ISO = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$")
_SLASH_YMD = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_SLASH_MDY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_YEAR = re.compile(r"^(1[5-9]\d{2}|2[01]\d{2})(?:\.0)?$")
_QUARTER = re.compile(r"^(1[5-9]\d{2}|2[01]\d{2})[-\s]?Q([1-4])$", re.I)
_MONTH_NAME = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2,4})$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Column names that identify the period axis even when the values themselves
# are ambiguous (a column of bare integers could be years or counts).
_PERIOD_KEYS = {
    "date", "dates", "day", "time", "timestamp", "period", "month", "year",
    "years", "modeldate", "t", "x", "week", "quarter", "asof", "as_of",
}
# Columns that are numeric but are never the measured quantity.
_NON_METRIC_KEYS = {"id", "index", "rank", "code", "iso", "fips", "gid"}

DAY, MONTH, QUARTER, YEAR = "day", "month", "quarter", "year"


@dataclass
class SeriesTable:
    """A parsed historical series, sorted ascending by period."""

    periods: list[str]
    columns: dict[str, list[float | None]]
    granularity: str
    kind: str
    group_column: str | None = None
    groups: list[str] = field(default_factory=list)
    rejection_reason: str = ""

    def __len__(self) -> int:
        return len(self.periods)

    @property
    def is_panel(self) -> bool:
        """True when rows are split across entities (countries, tickers, ...).

        Panel data must NOT be reduced as if it were one series: the "latest
        value" of an unmerged OWID export is whichever country sorts last.
        """
        return bool(self.group_column)


def parse_period(value: object) -> tuple[str, str] | None:
    """Normalise a period label to (sortable_key, granularity), or None.

    Accepts ISO dates, US M/D/Y, Y/M/D, bare years (as int, float or str),
    quarters, and D-Mon-Y. Deliberately permissive -- see the module docstring.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if float(value).is_integer() and 1500 <= float(value) <= 2200:
            return f"{int(value):04d}", YEAR
        return None

    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None

    m = _ISO.match(text)
    if m:
        year, month, day = m.group(1), int(m.group(2)), m.group(3)
        if not 1 <= month <= 12:
            return None
        if day is None:
            return f"{year}-{month:02d}", MONTH
        if not 1 <= int(day) <= 31:
            return None
        return f"{year}-{month:02d}-{int(day):02d}", DAY

    m = _SLASH_YMD.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}", DAY
        return None

    m = _SLASH_MDY.match(text)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000 if year < 70 else 1900
        # Ambiguous D/M/Y vs M/D/Y: only the impossible case is decidable, and
        # guessing wrong shifts a day, not a magnitude, so prefer US order and
        # swap only when it cannot be a month.
        if month > 12 and day <= 12:
            month, day = day, month
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}", DAY
        return None

    m = _MONTH_NAME.match(text)
    if m:
        day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        if year < 100:
            year += 2000 if year < 70 else 1900
        if mon in _MONTHS and 1 <= day <= 31:
            return f"{year:04d}-{_MONTHS[mon]:02d}-{day:02d}", DAY
        return None

    m = _QUARTER.match(text)
    if m:
        return f"{int(m.group(1)):04d}-Q{m.group(2)}", QUARTER

    m = _YEAR.match(text)
    if m:
        return f"{int(m.group(1)):04d}", YEAR

    return None


def is_period(value: object) -> bool:
    return parse_period(value) is not None


def to_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "").strip('"')
    if not text or text.lower() in {"na", "n/a", "null", "none", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def extract_series(payload: str, content_type: str = "") -> SeriesTable | None:
    """Return the longest historical series in ``payload``, or None.

    Tries JSON (row-oriented and column-oriented) then delimited text. The
    result is not length-filtered; callers apply ``MIN_SERIES_ROWS`` so they can
    log a near-miss ("found 40 rows") differently from a total miss.
    """
    if not payload or not payload.strip():
        return None

    text = payload.lstrip()
    best: SeriesTable | None = None

    if text[:1] in "{[":
        try:
            best = _from_json(json.loads(payload))
        except (ValueError, RecursionError):
            best = None

    if best is None and "html" not in content_type.lower():
        best = _from_delimited(payload)

    return best


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def _from_json(obj: object) -> SeriesTable | None:
    best: SeriesTable | None = None
    stack: list[object] = [obj]
    visited = 0
    while stack and visited < 20_000:
        cur = stack.pop()
        visited += 1
        if isinstance(cur, dict):
            table = _columns_from_dict(cur)
            if table and (best is None or len(table) > len(best)):
                best = table
            stack.extend(cur.values())
        elif isinstance(cur, list) and cur:
            table = _rows_from_list(cur)
            if table and (best is None or len(table) > len(best)):
                best = table
            if table is None:
                stack.extend(cur[:200])
    return best


def _columns_from_dict(node: dict) -> SeriesTable | None:
    """Column-oriented JSON: {"years": [...], "values": [...]}."""
    arrays = {k: v for k, v in node.items()
              if isinstance(v, list) and len(v) >= MIN_SERIES_ROWS}
    if len(arrays) < 2:
        return None

    period_key = None
    for key, values in arrays.items():
        sample = values[:40]
        if key.lower() in _PERIOD_KEYS or sum(is_period(v) for v in sample) >= len(sample) * 0.8:
            period_key = key
            break
    if period_key is None:
        return None

    parsed = [parse_period(v) for v in arrays[period_key]]
    if sum(p is not None for p in parsed) < MIN_SERIES_ROWS:
        return None
    granularity = _dominant([p[1] for p in parsed if p])

    columns: dict[str, list[float | None]] = {}
    for key, values in arrays.items():
        if key == period_key or key.lower() in _NON_METRIC_KEYS:
            continue
        nums = [to_number(v) for v in values[:len(parsed)]]
        if sum(n is not None for n in nums) >= len(nums) * 0.5:
            columns[key] = nums
    if not columns:
        return None

    keep = [i for i, p in enumerate(parsed) if p is not None]
    return _sorted_table(
        periods=[parsed[i][0] for i in keep],
        columns={k: [v[i] if i < len(v) else None for i in keep] for k, v in columns.items()},
        granularity=granularity,
        kind="json-columns",
    )


def _rows_from_list(items: list) -> SeriesTable | None:
    """Row-oriented JSON: [{"date": ..., "num_likers": ...}, ...]."""
    dict_rows = [i for i in items if isinstance(i, dict)]
    if len(dict_rows) >= MIN_SERIES_ROWS:
        return _rows_from_dicts(dict_rows)

    # [["2026-01-01", 12.3], ...]
    pairs: list[tuple[str, float]] = []
    granularities: list[str] = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            period = parse_period(item[0])
            value = to_number(item[1])
            if period and value is not None:
                pairs.append((period[0], value))
                granularities.append(period[1])
    if len(pairs) >= MIN_SERIES_ROWS:
        return _sorted_table(
            periods=[p for p, _ in pairs],
            columns={"value": [v for _, v in pairs]},
            granularity=_dominant(granularities),
            kind="json-pairs",
        )
    return None


def _rows_from_dicts(rows: list[dict]) -> SeriesTable | None:
    sample = rows[:60]
    # Insertion order, not set order: which column ends up chosen as "the
    # metric" must not vary between runs of the same input.
    keys = list(dict.fromkeys(k for row in sample for k in row))

    period_key = None
    for key in keys:
        if key.lower() in _PERIOD_KEYS and sum(is_period(r.get(key)) for r in sample) >= 1:
            period_key = key
            break
    if period_key is None:
        for key in keys:
            hits = sum(is_period(r.get(key)) for r in sample)
            if hits >= len(sample) * 0.8:
                period_key = key
                break
    if period_key is None:
        return None

    numeric_keys = [
        k for k in keys
        if k != period_key and k.lower() not in _NON_METRIC_KEYS
        and sum(to_number(r.get(k)) is not None for r in sample) >= len(sample) * 0.5
    ]
    if not numeric_keys:
        return None

    group_key, groups = _find_group_column(rows, keys, period_key, numeric_keys)

    periods: list[str] = []
    granularities: list[str] = []
    columns: dict[str, list[float | None]] = {k: [] for k in numeric_keys}
    for row in rows:
        period = parse_period(row.get(period_key))
        if period is None:
            continue
        periods.append(period[0])
        granularities.append(period[1])
        for key in numeric_keys:
            columns[key].append(to_number(row.get(key)))
    if len(periods) < MIN_SERIES_ROWS:
        return None

    return _sorted_table(
        periods=periods,
        columns=columns,
        granularity=_dominant(granularities),
        kind="json-rows",
        group_column=group_key,
        groups=groups,
        skip_sort=bool(group_key),
    )


# ---------------------------------------------------------------------------
# Delimited text
# ---------------------------------------------------------------------------
def _from_delimited(payload: str) -> SeriesTable | None:
    head = payload[:4_000_000]
    if head.lstrip()[:1] == "<":
        return None

    best: SeriesTable | None = None
    for delimiter in (",", "\t", ";", "|"):
        try:
            rows = list(csv.reader(io.StringIO(head), delimiter=delimiter))
        except csv.Error:
            continue
        rows = [r for r in rows if any(c.strip() for c in r)]
        if len(rows) < MIN_SERIES_ROWS + 1:
            continue

        width = max((len(r) for r in rows[:200]), default=0)
        if width < 2:
            continue
        header, body = rows[0], [r for r in rows[1:] if len(r) == width]
        if len(body) < MIN_SERIES_ROWS:
            continue
        if len(header) != width:
            header = [f"col{i}" for i in range(width)]

        table = _table_from_rows(header, body)
        if table and (best is None or len(table) > len(best)):
            best = table
    return best


def _table_from_rows(header: list[str], body: list[list[str]]) -> SeriesTable | None:
    sample = body[:200]
    period_index = None
    for i, name in enumerate(header):
        if name.strip().lower() in _PERIOD_KEYS:
            if sum(is_period(r[i]) for r in sample) >= len(sample) * 0.5:
                period_index = i
                break
    if period_index is None:
        for i in range(len(header)):
            if sum(is_period(r[i]) for r in sample) >= len(sample) * 0.8:
                period_index = i
                break
    if period_index is None:
        return None

    numeric_indexes = [
        i for i in range(len(header))
        if i != period_index
        and header[i].strip().lower() not in _NON_METRIC_KEYS
        and sum(to_number(r[i]) is not None for r in sample) >= len(sample) * 0.8
    ]
    if not numeric_indexes:
        return None

    rows_as_dicts = [
        {header[i] or f"col{i}": r[i] for i in [period_index, *numeric_indexes]}
        for r in body
    ]
    period_name = header[period_index] or f"col{period_index}"
    group_key, groups = _find_group_column(
        [dict(zip(header, r)) for r in body],
        list(header),
        period_name,
        [header[i] for i in numeric_indexes],
    )

    periods: list[str] = []
    granularities: list[str] = []
    columns: dict[str, list[float | None]] = {
        header[i] or f"col{i}": [] for i in numeric_indexes
    }
    for row in rows_as_dicts:
        period = parse_period(row.get(period_name))
        if period is None:
            continue
        periods.append(period[0])
        granularities.append(period[1])
        for name in columns:
            columns[name].append(to_number(row.get(name)))
    if len(periods) < MIN_SERIES_ROWS:
        return None

    return _sorted_table(
        periods=periods,
        columns=columns,
        granularity=_dominant(granularities),
        kind="csv",
        group_column=group_key,
        groups=groups,
        skip_sort=bool(group_key),
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _find_group_column(
    rows: list[dict],
    keys: list[str],
    period_key: str,
    numeric_keys: list[str],
) -> tuple[str | None, list[str]]:
    """Detect a panel/entity column (country, ticker, candidate, ...).

    A repeated period is the tell: one series cannot have 250 rows for 1950.
    Getting this wrong is not cosmetic -- reducing an unmerged OWID export as a
    single series reports Zimbabwe's 2023 life expectancy as "the latest value".
    """
    sample = rows[:2000]
    period_values = [str(r.get(period_key)) for r in sample]
    if len(set(period_values)) >= len(period_values) * 0.9:
        return None, []

    for key in keys:
        if key == period_key or key in numeric_keys:
            continue
        values = [str(r.get(key)) for r in sample if r.get(key) is not None]
        if not values:
            continue
        distinct = len(set(values))
        if 1 < distinct <= max(2, len(values) // 3) and not is_period(values[0]):
            # Count groups across ALL rows, not the detection sample: an
            # alphabetically sorted export puts only the first slice of
            # entities in the first 2000 rows, which understated OWID's ~250
            # countries as 25.
            ordered: list[str] = []
            seen: set[str] = set()
            for row in rows:
                value = row.get(key)
                if value is None:
                    continue
                text = str(value)
                if text not in seen:
                    seen.add(text)
                    ordered.append(text)
            return key, ordered
    return None, []


def _dominant(values: list[str]) -> str:
    if not values:
        return DAY
    return max(set(values), key=values.count)


def _sorted_table(
    *,
    periods: list[str],
    columns: dict[str, list[float | None]],
    granularity: str,
    kind: str,
    group_column: str | None = None,
    groups: list[str] | None = None,
    skip_sort: bool = False,
) -> SeriesTable:
    if not skip_sort:
        order = sorted(range(len(periods)), key=lambda i: periods[i])
        periods = [periods[i] for i in order]
        columns = {k: [v[i] for i in order] for k, v in columns.items()}
    return SeriesTable(
        periods=periods,
        columns=columns,
        granularity=granularity,
        kind=kind,
        group_column=group_column,
        groups=list(groups or []),
    )


def count_series_rows(payload: str, content_type: str = "") -> int:
    """Gate helper: how many usable (period, value) rows does this payload hold?"""
    table = extract_series(payload, content_type)
    return len(table) if table else 0


def satisfies_gate(payload: str, content_type: str = "") -> bool:
    return count_series_rows(payload, content_type) >= MIN_SERIES_ROWS
