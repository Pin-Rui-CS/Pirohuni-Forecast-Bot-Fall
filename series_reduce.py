"""Turn a retrieved historical series into a compact, forecast-ready table.

Deterministic arithmetic only -- no LLM, so this costs nothing and cannot
hallucinate a number that is not in the data.

WHY THIS EXISTS, and why it is not optional. On question 44875 the score
decomposition was measured as:

    fix the centre only  -> 1.02x density at the outcome
    fix the width only   -> 3.08x
    fix both             -> 4.20x

Retrieval alone buys the centre. The width came from measuring how much the
series had *historically* moved over the question's horizon -- 35-day-ahead
ratios with p5-p95 of 0.949-1.021, implying sigma about 0.045 against the
0.175-0.34 the ensemble actually used. No model produces that by reading 1,247
JSON rows in a prompt, and the compiler keeps roughly one fact in five from any
section regardless of quality, so handing over the raw series destroys exactly
the density the calculation needs.

So the raw payload is reduced HERE, while its structure is still known, and the
~2 KB result travels downstream. That also sidesteps the head-truncation family
of bugs (44382, 44512, 44619, 44875): a 232 KB date-ascending JSON cut to its
first 8,000 characters keeps 2023 and silently discards every row that matters.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from series_gate import DAY, MONTH, QUARTER, YEAR, SeriesTable

# Horizons (in periods) for the ahead-ratio table. The question's own horizon is
# rarely known this deep in the stack, so a spread is emitted and the forecaster
# reads the row nearest its resolution date.
DAILY_HORIZONS = (7, 14, 30, 60, 90)
COARSE_HORIZONS = (1, 2, 3, 5, 10)

# A row below this fraction of its trailing median is treated as an ingestion
# outage, not a real observation. Jaz's Bluesky index broke this way on 14 of
# 1,238 days -- including a 54-liker day -- and a forecaster that reads one of
# those as the current level is badly wrong in a way no widening can cover.
OUTAGE_FRACTION = 0.70
TRAILING_WINDOW = 7

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class ReducedSeries:
    text: str
    metric: str
    latest_period: str
    latest_value: float | None
    rows_used: int
    excluded: list[str]


def reduce_series(
    table: SeriesTable,
    *,
    endpoint: str,
    metric_hint: str = "",
    max_groups_listed: int = 25,
) -> ReducedSeries:
    """Render ``table`` as a small markdown block for the research report."""
    if table.is_panel:
        return _reduce_panel(table, endpoint, max_groups_listed)

    metric = _choose_metric(table, metric_hint)
    values = table.columns[metric]
    periods = table.periods

    pairs = [(p, v) for p, v in zip(periods, values) if v is not None]
    if not pairs:
        return ReducedSeries(
            text=f"No numeric values in column `{metric}`.",
            metric=metric, latest_period="", latest_value=None,
            rows_used=0, excluded=[],
        )

    kept, excluded = _drop_outages(pairs)
    lines: list[str] = []
    lines.append("### Measured series (deterministic extract -- no model involved)")
    lines.append("")
    lines.append(f"- Endpoint: {endpoint}")
    lines.append(f"- Metric column: `{metric}`"
                 + (f"  (other columns: {', '.join(k for k in table.columns if k != metric)})"
                    if len(table.columns) > 1 else ""))
    # Report the range of rows actually USED. Quoting the raw range here while
    # the tables below silently start elsewhere reads as an inconsistency: the
    # Bluesky feed always carries a partial row for the current UTC day (4
    # likers at the time of writing), which is dropped as an outage.
    lines.append(f"- {len(kept):,} usable observations, {kept[0][0]} to {kept[-1][0]}, "
                 f"granularity = {table.granularity}")
    if excluded:
        lines.append(f"- Excluded {len(excluded)} suspected outage/partial row(s): "
                     + ", ".join(f"{p} ({v:,.0f})" for p, v in excluded[:6])
                     + (" ..." if len(excluded) > 6 else ""))
        lines.append(f"  (below {OUTAGE_FRACTION:.0%} of the trailing "
                     f"{TRAILING_WINDOW}-period median; this source genuinely breaks)")
    lines.append("")

    lines.extend(_level_block(kept, table.granularity))
    lines.extend(_calendar_block(kept, table.granularity))
    lines.extend(_weekday_block(kept, table.granularity))
    lines.extend(_ahead_block(kept, table.granularity))
    lines.extend(_recent_rows(kept))

    return ReducedSeries(
        text="\n".join(lines).rstrip() + "\n",
        metric=metric,
        latest_period=kept[-1][0] if kept else "",
        latest_value=kept[-1][1] if kept else None,
        rows_used=len(kept),
        excluded=[p for p, _ in excluded],
    )


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
def _level_block(pairs: list[tuple[str, float]], granularity: str) -> list[str]:
    values = [v for _, v in pairs]
    unit = {DAY: "day", MONTH: "month", QUARTER: "quarter", YEAR: "year"}[granularity]
    lines = ["**Current level**", ""]
    lines.append(f"| measure | value |")
    lines.append("| --- | ---: |")
    lines.append(f"| latest ({pairs[-1][0]}) | {_fmt(pairs[-1][1])} |")
    for window in (7, 14, 28) if granularity == DAY else (3, 6, 12):
        if len(values) >= window:
            mean = statistics.mean(values[-window:])
            lines.append(f"| {window}-{unit} mean | {_fmt(mean)} |")
    if len(values) >= 2:
        prior = values[-2]
        if prior:
            lines.append(f"| change vs previous {unit} | {(values[-1] / prior - 1) * 100:+.2f}% |")
    lines.append("")
    return lines


def _calendar_block(pairs: list[tuple[str, float]], granularity: str) -> list[str]:
    if granularity != DAY:
        return []
    buckets: dict[str, list[float]] = {}
    for period, value in pairs:
        buckets.setdefault(period[:7], []).append(value)
    months = sorted(buckets)[-10:]
    if len(months) < 3:
        return []
    lines = ["**Monthly means** (month-over-month change is the trend signal)", "",
             "| month | mean | MoM |", "| --- | ---: | ---: |"]
    previous: float | None = None
    for month in months:
        mean = statistics.mean(buckets[month])
        change = f"{(mean / previous - 1) * 100:+.1f}%" if previous else "--"
        lines.append(f"| {month} | {_fmt(mean)} | {change} |")
        previous = mean
    lines.append("")
    return lines


def _weekday_block(pairs: list[tuple[str, float]], granularity: str) -> list[str]:
    """Day-of-week factors against a centred 7-day mean.

    Directly measurable from the source and repeatedly guessed at instead: on
    44875 only one of three runs even noticed the resolution date was a Monday,
    and none quantified the weekly amplitude.
    """
    if granularity != DAY or len(pairs) < 60:
        return []
    recent = pairs[-84:]
    values = [v for _, v in recent]
    factors: dict[int, list[float]] = {}
    for i in range(3, len(recent) - 3):
        window = values[i - 3:i + 4]
        centre = statistics.mean(window)
        if not centre:
            continue
        try:
            weekday = dt.date.fromisoformat(recent[i][0]).weekday()
        except ValueError:
            return []
        factors.setdefault(weekday, []).append(values[i] / centre)
    if len(factors) < 7:
        return []
    lines = ["**Day-of-week factors** (last 12 weeks, vs centred 7-day mean)", "",
             "| " + " | ".join(_WEEKDAYS) + " |",
             "| " + " | ".join("---:" for _ in _WEEKDAYS) + " |",
             "| " + " | ".join(f"{statistics.mean(factors[i]):.4f}" for i in range(7)) + " |",
             ""]
    return lines


def _ahead_block(pairs: list[tuple[str, float]], granularity: str) -> list[str]:
    """Empirical h-ahead ratio quantiles -- the forecast-width anchor.

    For each horizon h, every historical window contributes value[t+h] divided
    by the trailing mean at t. The spread of those ratios is how much this
    series actually moves over h periods, which is what a forecast interval
    should be sized from.
    """
    values = [v for _, v in pairs]
    horizons = DAILY_HORIZONS if granularity == DAY else COARSE_HORIZONS
    base_window = 7 if granularity == DAY else 3
    if len(values) < base_window + max(horizons) + 20:
        horizons = tuple(h for h in horizons
                         if len(values) >= base_window + h + 20)
    if not horizons:
        return []

    unit = {DAY: "day", MONTH: "month", QUARTER: "quarter", YEAR: "year"}[granularity]
    lines = [
        f"**How much this series actually moves** -- ratio of value at t+h to the "
        f"trailing {base_window}-{unit} mean at t.",
        "Size the forecast interval from these, not from general uncertainty.",
        "",
        f"| window | horizon | n | p5 | p25 | p50 | p75 | p95 | implied sigma |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    # Two lookbacks, because a series that changed behaviour makes its own
    # distant history misleading. On 44875 the long window carries the January
    # Grok spike and implies sigma ~0.066 at 30 days; the plateau-only window
    # implies ~0.02. Which regime holds is a judgement call, so both are shown
    # rather than one being silently chosen here.
    windows = [("long", 400), ("recent", 120)]
    for label, span in windows:
        if len(values) < span // 2:
            continue
        recent = values[-span:] if len(values) > span else values
        for horizon in horizons:
            ratios: list[float] = []
            for t in range(base_window, len(recent) - horizon):
                base = statistics.mean(recent[t - base_window:t])
                if base:
                    ratios.append(recent[t + horizon] / base)
            if len(ratios) < 20:
                continue
            ratios.sort()
            q = _quantiles(ratios, (0.05, 0.25, 0.50, 0.75, 0.95))
            sigma = (q[4] - q[0]) / 3.29  # a normal's 90% span is 3.29 sigma
            lines.append(
                f"| {label} {min(span, len(values))} | {horizon} {unit}s | {len(ratios)} | "
                + " | ".join(f"{x:.4f}" for x in q)
                + f" | {sigma:.3f} |"
            )
    lines.append("")
    return lines


def _recent_rows(pairs: list[tuple[str, float]], count: int = 14) -> list[str]:
    tail = pairs[-count:]
    lines = [f"**Last {len(tail)} observations** (verbatim from the source)", "",
             "| period | value |", "| --- | ---: |"]
    lines.extend(f"| {p} | {_fmt(v)} |" for p, v in tail)
    lines.append("")
    return lines


def _reduce_panel(table: SeriesTable, endpoint: str, max_groups: int) -> ReducedSeries:
    """Panel data: report the structure, compute nothing.

    Reducing an unmerged multi-entity export as if it were one series reports
    whichever entity sorts last as "the latest value" -- for the OWID life
    expectancy CSV that is Zimbabwe. Better to hand back the shape and let the
    caller narrow it than to emit a confident wrong number.
    """
    shown = table.groups[:max_groups]
    text = "\n".join([
        "### Measured series (deterministic extract -- no model involved)",
        "",
        f"- Endpoint: {endpoint}",
        f"- **This is panel data**, not a single series: {len(table.periods):,} rows "
        f"split across {len(table.groups)} values of `{table.group_column}`.",
        f"- Periods span {min(table.periods)} to {max(table.periods)}, "
        f"granularity = {table.granularity}.",
        f"- Numeric columns: {', '.join(table.columns)}",
        f"- `{table.group_column}` values: {', '.join(shown)}"
        + (f" ... (+{len(table.groups) - len(shown)} more)" if len(table.groups) > len(shown) else ""),
        "",
        "No trend statistics computed: they would be meaningless across mixed "
        "entities. Select the entity the question asks about, then re-reduce.",
        "",
    ])
    return ReducedSeries(text=text, metric=next(iter(table.columns), ""),
                         latest_period="", latest_value=None,
                         rows_used=len(table.periods), excluded=[])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_BOUND_MARKERS = ("_lo", "_hi", "low", "high", "upper", "lower",
                  "_min", "_max", "err", "ci_", "_ci", "margin")


def _is_bound_column(name: str) -> bool:
    return any(marker in name.lower() for marker in _BOUND_MARKERS)


def _choose_metric(table: SeriesTable, hint: str) -> str:
    """Pick the measured column, preferring one the question actually names.

    Confidence-band columns must lose ties: ``approve`` and ``approve_lo`` both
    match a hint mentioning "approve", and reducing the lower bound as though it
    were the estimate reports a systematically low level.
    """
    names = list(table.columns)
    if hint:
        tokens = [t for t in _tokenise(hint) if len(t) > 2]
        best, best_score = None, 0.0
        for name in names:
            name_tokens = set(_tokenise(name))
            score = float(sum(1 for t in tokens if t in name_tokens))
            if not score:
                continue
            if _is_bound_column(name):
                score -= 0.5
            # Prefer the plainer column when both match equally: a bare metric
            # name is more likely the headline series than a decorated variant.
            score -= 0.01 * len(name_tokens)
            if score > best_score:
                best, best_score = name, score
        if best:
            return best
    for name in names:
        if not _is_bound_column(name):
            return name
    return names[0]


def _tokenise(text: str) -> list[str]:
    return [t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split()]


def _drop_outages(
    pairs: list[tuple[str, float]]
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Split off rows that look like ingestion failures or a partial final period."""
    kept: list[tuple[str, float]] = []
    dropped: list[tuple[str, float]] = []
    for index, (period, value) in enumerate(pairs):
        window = [v for _, v in pairs[max(0, index - TRAILING_WINDOW):index]]
        if len(window) >= 3:
            median = statistics.median(window)
            if median and value < median * OUTAGE_FRACTION:
                dropped.append((period, value))
                continue
        kept.append((period, value))
    return kept, dropped


def _quantiles(sorted_values: list[float], points: tuple[float, ...]) -> list[float]:
    out: list[float] = []
    n = len(sorted_values)
    for p in points:
        position = p * (n - 1)
        low = int(position)
        high = min(low + 1, n - 1)
        weight = position - low
        out.append(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)
    return out


def _fmt(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return f"{value:.6g}"
