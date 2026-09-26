"""Measured series from a documented API, before any scraping.

``series_discovery`` reverse-engineers a series out of a page's HTML and
JavaScript. That fails on app-shell pages whose data sits behind a documented
API the page never names in its markup: on 45412 the resolution source was
fiscaldata.treasury.gov's Debt to the Penny page, discovery found nothing, and
the bot forecast from ~20 scattered data points while the full daily series
(8,400 rows) was one keyless GET away.

This module maps a resolution URL to an apiagent-kit adapter and pulls the
whole series through ``apiagent_bridge``. It returns the same ``SeriesArtifact``
the discovery ladder does, so everything downstream (the gate, ``series_reduce``,
the MEASURED SERIES OVERRIDE in the numeric prompt) is unchanged. A URL with no
mapping, an unavailable kit, or a failed call returns None and the caller falls
back to the ladder.

Adding a source: one resolver function below and one line in ``_RESOLVERS``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import apiagent_bridge
from series_discovery import SeriesArtifact
from series_gate import MIN_SERIES_ROWS, extract_series

logger = logging.getLogger(__name__)

# Rows requested. Enough for several years of a daily series; the reducer only
# looks back ~400 periods for spread, but the same-window block wants prior years.
SERIES_LIMIT = 5000

_Fetched = tuple[str, str, list[dict]]  # (api id, retrieval url, rows)
_Resolver = Callable[[str, str], Awaitable[_Fetched | None]]


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


# ---------------------------------------------------------------------------
# fiscaldata.treasury.gov
# ---------------------------------------------------------------------------

_FISCAL_PAGE = re.compile(r"^/datasets/([a-z0-9-]+)(?:/([a-z0-9-]+))?", re.I)
# Calendar-decomposition and bookkeeping columns. Numeric-looking, never the
# measure, and a second numeric column the metric chooser could pick.
_FISCAL_NOISE = re.compile(r"^record_(fiscal|calendar)_|_nbr$")


async def _fiscaldata(url: str, hint: str) -> _Fetched | None:
    match = _FISCAL_PAGE.match(urlsplit(url).path)
    if not match:
        return None
    slug, table_slug = match.group(1).lower(), (match.group(2) or "").lower()

    catalog = await apiagent_bridge.call_api(
        "treasury_fiscal", {"mode": "catalog", "dataset": slug})
    tables = [t for t in catalog.rows if t.get("dateField") == "record_date"]
    if not tables:
        logger.info("[known-series] %s: no dated table in catalog (%s)",
                    slug, catalog.error or "0 rows")
        return None

    # A dataset can hold many tables (the MTS has 18). Pick the one the URL's
    # table segment or the question's wording names; ties keep catalog order.
    wanted = _tokens(table_slug.replace("-", " ") + " " + hint)
    table = max(tables, key=lambda t: len(wanted & _tokens(
        f"{t.get('table', '')} {' '.join((t.get('labels') or {}).values())}")))

    result = await apiagent_bridge.call_api(
        "treasury_fiscal",
        {"mode": "observations", "endpoint": table["endpoint"], "limit": SERIES_LIMIT},
        timeout=90,
    )
    if not result.has_rows:
        logger.info("[known-series] %s: %s", table["endpoint"], result.error or "no rows")
        return None

    # Rename measures to their published labels ("Total Public Debt
    # Outstanding"), so the reducer's metric choice matches the resolution
    # criteria's wording rather than an abbreviated column name.
    labels = table.get("labels") or {}
    rows = [
        {labels.get(k, k): v for k, v in row.items() if not _FISCAL_NOISE.search(k)}
        for row in result.rows
    ]
    return "treasury_fiscal", result.url, rows


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------

_FRED_SERIES = re.compile(r"^/series/([A-Za-z0-9_.-]{2,64})/?$")


async def _fred(url: str, hint: str) -> _Fetched | None:
    parts = urlsplit(url)
    match = _FRED_SERIES.match(parts.path)
    series_id = match.group(1) if match else (parse_qs(parts.query).get("id") or [""])[0]
    if not series_id:
        return None
    result = await apiagent_bridge.call_api(
        "fred", {"mode": "observations", "seriesId": series_id, "limit": SERIES_LIMIT},
        timeout=90,
    )
    if not result.has_rows:
        logger.info("[known-series] FRED %s: %s", series_id, result.error or "no rows")
        return None
    return "fred", result.url, [{"date": r.get("date"), series_id: r.get("value")}
                                for r in result.rows]


_RESOLVERS: dict[str, _Resolver] = {
    "fiscaldata.treasury.gov": _fiscaldata,
    "fred.stlouisfed.org": _fred,
}


def resolver_for(url: str) -> _Resolver | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return _RESOLVERS.get(host)


async def fetch_known_series(url: str, *, metric_hint: str = "") -> SeriesArtifact | None:
    """The full series behind ``url`` via a known API, or None to fall back."""
    resolver = resolver_for(url)
    if resolver is None:
        return None
    try:
        fetched = await resolver(url, metric_hint)
    except Exception as exc:  # noqa: BLE001 - optional enrichment, never fatal
        logger.warning("[known-series] %s failed: %s", url, exc)
        return None
    if fetched is None:
        return None

    api, endpoint, rows = fetched
    payload = json.dumps(rows)
    table = extract_series(payload, "application/json")
    if table is None or len(table) < MIN_SERIES_ROWS:
        logger.info("[known-series] %s: %s rows from %s, below the %d-row gate",
                    url, len(table) if table else 0, api, MIN_SERIES_ROWS)
        return None
    logger.info("[known-series] %s: %d rows via %s", url, len(table), api)
    return SeriesArtifact(
        source_url=url,
        endpoint=endpoint,
        payload=payload,
        table=table,
        rung=f"api:{api}",
        content_type="application/json",
        trail=[url, endpoint],
    )
