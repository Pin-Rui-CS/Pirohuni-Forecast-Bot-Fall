"""Free retrieval ladder for the historical series behind a resolution source.

Plain HTTP + regex. No LLM calls, no Firecrawl credits, no browser -- so this
can run on every numeric question without moving either budget.

Motivating failure (question 44875, 2026-07-27): the resolution source
``bsky.jazco.dev/stats`` is a Vite shell whose chart is canvas-drawn. Every
scrape returned the same 250-byte boilerplate, the pipeline recorded
"artifact missing" and forecast anyway, extrapolating 4.85 months from a
journalist's eyeball of a chart. The full 1,248-row daily series was one
unauthenticated GET away at ``bsky-search.jazco.io/stats`` -- a URL that exists
only as a fetch() target inside the shell's own JavaScript bundle.

The rungs, in ascending cost (all still free):

  1    the page as served, after following meta-refresh / JS redirects
  1.5  JSON embedded in inline <script> tags
  1.75 iframes, one level deep (chart embeds -- Datawrapper, Flourish, ...)
  2    endpoints named in the page HTML and its JS bundles, ranked and fetched

Escalation is driven by the gate in ``series_gate`` -- "do we hold >= 100
(period, number) rows?" -- NOT by a "looks like an empty JS shell" heuristic.
That heuristic was tried first and measured badly: FRED, Our World in Data and
FiveThirtyEight all serve bulky HTML and never tripped it, while yielding no
series. Non-emptiness is likewise useless as a signal, since the 250-byte shell
that started all this is a perfectly valid HTTP 200.

Measured 2026-07-29 on five resolution-source-style pages: solved bsky.jazco.dev
(1,248 rows), ourworldindata.org/grapher (19,851 rows) and natesilver.net
(554 rows, reached through a Datawrapper iframe and a 28-hop meta-refresh
chain). FiveThirtyEight builds its data URL at runtime, so no literal exists to
find; FRED rate-limits plain clients. Roughly half of tracker pages, at no cost.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from series_gate import MIN_SERIES_ROWS, SeriesTable, extract_series

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# A ranked candidate that turns out to be a 165 MB graph export (seen on
# s3.jazco.io while testing) must not be pulled into memory. Data files that
# actually hold a tracker series are far smaller than this.
MAX_PAYLOAD_BYTES = 25_000_000
MAX_BUNDLE_BYTES = 12_000_000
MAX_BUNDLES = 5
MAX_CANDIDATES = 12
MAX_IFRAMES = 4
MAX_REDIRECT_HOPS = 40

# src= on an <iframe>, tolerating the backslash-escaped form pages use when
# they embed their own HTML inside a JSON payload.
_IFRAME_SRC = re.compile(r'''<iframe[^>]+src=[\\"']*([^"'\\ >]+)''', re.I)
# meta-refresh and JS location redirects. httpx follows neither, and a
# Datawrapper embed chains ~28 of them between the URL in the page and the
# live chart.
_REDIRECT = re.compile(
    r'''(?ix)
    (?: http-equiv=["']refresh["'][^>]*content=["'][^"']*url=
      | window\.location (?:\.href)? \s*=\s* ["'] )
    ([^"'>]+)'''
)
_SCRIPT_SRC = re.compile(r'''<script[^>]+src=["']([^"']+)["']''', re.I)
_INLINE_SCRIPT = re.compile(r"(?is)<script\b[^>]*>(.*?)</script>")
_BUNDLER_PATH = re.compile(r"/(assets|_next/static|static/js|_app/immutable|build|dist)/", re.I)
_ABS_URL = re.compile(r"""https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{6,220}""")
_REL_DATA = re.compile(r"""["'`](/[A-Za-z0-9._/-]{3,120}\.(?:json|csv|tsv))["'`]""")
_BARE_DATA = re.compile(r"""["'`]([A-Za-z0-9._-]{3,60}\.(?:json|csv|tsv))["'`]""")
_JSON_BLOB = re.compile(r"(\{.{200,}\}|\[.{200,}\])", re.S)

# Third-party infrastructure that never carries the question's data. Ranking
# without this list drowns: the FiveThirtyEight page alone offers 748 URLs.
_JUNK_HOSTS = (
    "googletagmanager", "google-analytics", "doubleclick", "adservice",
    "sentry", "posthog", "braze", "datadoghq", "cloudflareinsights",
    "fonts.g", "gstatic", "w3.org", "schema.org", "creativecommons",
    "youtube", "vimeo", "facebook", "twitter", "linkedin", "instagram",
    "gravatar", "npmjs", "jquery", "bootstrapcdn", "polyfill",
)
_ASSET_SUFFIXES = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                   ".webp", ".woff", ".woff2", ".ttf", ".eot", ".ico", ".mp4")
_DATA_SUFFIXES = (".json", ".csv", ".tsv", ".xml")
_DATA_WORDS = ("api", "stats", "data", "series", "json", "csv", "graph", "chart",
               "observations", "metrics", "export", "download", "history", "daily")


@dataclass
class SeriesArtifact:
    """A historical series retrieved for a resolution source."""

    source_url: str
    endpoint: str
    payload: str
    table: SeriesTable
    rung: str
    content_type: str = ""
    trail: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return len(self.table)


async def discover_series(
    page_url: str,
    *,
    timeout: int = 30,
    client: httpx.AsyncClient | None = None,
) -> SeriesArtifact | None:
    """Climb the ladder for one page. Returns None if no rung yielded a series.

    Never raises for network reasons: this is an optional enrichment path, and
    a resolution source that cannot be reached must fall through to the ordinary
    scrape rather than fail the question.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT,
                     "Accept": "text/html,application/json,text/csv,*/*"},
            timeout=timeout,
            follow_redirects=True,
        )
    try:
        return await _climb(page_url, client, depth=0, trail=[])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Series discovery failed for %s: %s", page_url, exc)
        return None
    finally:
        if owns_client:
            await client.aclose()


async def _climb(
    page_url: str,
    client: httpx.AsyncClient,
    *,
    depth: int,
    trail: list[str],
) -> SeriesArtifact | None:
    fetched = await _fetch(client, page_url)
    if fetched is None:
        return None
    html, content_type, final_url = fetched

    # Rung 1 -- the page as served.
    table = extract_series(html, content_type)
    if table and len(table) >= MIN_SERIES_ROWS:
        trail.append(f"rung1 page {final_url} ({len(table)} rows)")
        return SeriesArtifact(page_url, final_url, html, table, "page",
                              content_type, trail)
    trail.append(f"rung1 {final_url}: {len(table) if table else 0} rows")

    # Rung 1.5 -- JSON sitting in an inline <script>.
    for index, body in enumerate(_INLINE_SCRIPT.findall(html)):
        if len(body) < 300:
            continue
        for blob in _JSON_BLOB.findall(body)[:3]:
            table = extract_series(blob)
            if table and len(table) >= MIN_SERIES_ROWS:
                trail.append(f"rung1.5 inline script #{index} ({len(table)} rows)")
                return SeriesArtifact(page_url, f"{final_url}#inline-script-{index}",
                                      blob, table, "inline-script", "application/json", trail)

    # Rung 1.75 -- chart embeds. natesilver.net renders through Datawrapper
    # iframes; the outer page holds no numbers at all.
    if depth == 0:
        for frame in _iframe_urls(html, final_url)[:MAX_IFRAMES]:
            found = await _climb(frame, client, depth=1, trail=trail)
            if found is not None:
                found.source_url = page_url
                found.rung = f"iframe/{found.rung}"
                return found

    # Rung 2 -- endpoints named in the page and its bundles.
    return await _discover_endpoints(page_url, final_url, html, client, trail)


async def _discover_endpoints(
    page_url: str,
    final_url: str,
    html: str,
    client: httpx.AsyncClient,
    trail: list[str],
) -> SeriesArtifact | None:
    # Pages that embed their own markup inside a JSON payload escape the quotes;
    # unescaping exposes URLs the raw regex would miss (this is how the
    # Datawrapper externalData URL surfaces).
    unescaped = html.replace('\\"', '"').replace("\\/", "/")
    urls: set[str] = set(_ABS_URL.findall(html)) | set(_ABS_URL.findall(unescaped))
    relative: set[str] = set(_REL_DATA.findall(html)) | set(_REL_DATA.findall(unescaped))
    bare: set[str] = set(_BARE_DATA.findall(html))

    for script in _bundle_urls(html, final_url)[:MAX_BUNDLES]:
        fetched = await _fetch_text(client, script, MAX_BUNDLE_BYTES)
        if fetched is None:
            continue
        body = fetched[0]
        urls |= set(_ABS_URL.findall(body))
        relative |= set(_REL_DATA.findall(body))
        bare |= set(_BARE_DATA.findall(body))

    origin = f"{urlsplit(final_url).scheme}://{urlsplit(final_url).netloc}"
    for path in relative:
        urls.add(origin + path)
    for name in bare:
        base = final_url if final_url.endswith("/") else final_url + "/"
        urls.add(urljoin(base, name))
        urls.add(f"{origin}/data/{name}")

    candidates = _rank_candidates(urls, final_url)
    trail.append(f"rung2 {len(candidates)} candidates from {len(urls)} urls")

    for candidate in candidates:
        fetched = await _fetch(client, candidate)
        if fetched is None:
            continue
        body, content_type, _ = fetched
        if content_type.startswith("text/html"):
            continue
        table = extract_series(body, content_type)
        if table and len(table) >= MIN_SERIES_ROWS:
            trail.append(f"rung2 hit {candidate} ({len(table)} rows)")
            return SeriesArtifact(page_url, candidate, body, table, "endpoint",
                                  content_type, trail)
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
async def _fetch(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str, str] | None:
    """GET a URL, following meta-refresh/JS redirect stubs. -> (body, ctype, url)."""
    current = url
    for _ in range(MAX_REDIRECT_HOPS):
        fetched = await _fetch_text(client, current, MAX_PAYLOAD_BYTES)
        if fetched is None:
            return None
        body, content_type = fetched
        # Redirect stubs are tiny by construction; anything substantial is the
        # real page, and scanning it for a location assignment would follow
        # unrelated JS.
        if len(body) >= 2000:
            return body, content_type, current
        match = _REDIRECT.search(body)
        if not match:
            return body, content_type, current
        nxt = urljoin(current, match.group(1).strip())
        if nxt == current:
            return body, content_type, current
        current = nxt
    return None


async def _fetch_text(
    client: httpx.AsyncClient, url: str, max_bytes: int
) -> tuple[str, str] | None:
    """Stream a URL, abandoning it once it exceeds ``max_bytes``.

    Returns (body, content_type). Streaming rather than ``client.get`` because a
    ranked candidate on s3.jazco.io turned out to be a 165 MB graph export.
    """
    try:
        async with client.stream("GET", url) as response:
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if response.status_code >= 400:
                return None
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                logger.debug("Skipping %s: content-length %s exceeds cap", url, declared)
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    logger.debug("Skipping %s: body exceeded %d bytes", url, max_bytes)
                    return None
                chunks.append(chunk)
        body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        return body, content_type
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Candidate extraction and ranking
# ---------------------------------------------------------------------------
def _iframe_urls(html: str, base: str) -> list[str]:
    frames: list[str] = []
    for src in _IFRAME_SRC.findall(html):
        url = urljoin(base, src.strip())
        if any(junk in url.lower() for junk in _JUNK_HOSTS):
            continue
        if url not in frames:
            frames.append(url)
    return frames


def _bundle_urls(html: str, base: str) -> list[str]:
    scripts: list[str] = []
    for src in _SCRIPT_SRC.findall(html):
        url = urljoin(base, src.strip())
        if not (_BUNDLER_PATH.search(url) or url.endswith(".js")):
            continue
        if any(junk in url.lower() for junk in _JUNK_HOSTS):
            continue
        if url not in scripts:
            scripts.append(url)
    return scripts


def _clean_url(url: str) -> str:
    """Undo HTML entity escaping picked up when scraping URLs out of markup.

    OWID's CSV link arrives as ``...?v=1&amp;csvType=full``; that host tolerates
    it, but a literal ``&amp;`` in a query string is a different parameter name
    on any server that does not.
    """
    return html_module.unescape(url).strip().rstrip("\\\"'")


def _rank_candidates(urls: set[str], page_url: str) -> list[str]:
    """Order discovered URLs by how much they look like the page's data source.

    Deterministic and free -- no model ranks these. The signals that mattered in
    testing: a sibling subdomain of the page's own domain (bsky.jazco.dev ->
    bsky-search.jazco.io), a path echoing the page's path, and a data suffix.
    """
    host = urlsplit(page_url).netloc.lower()
    registrable = ".".join(host.split(".")[-2:])
    path_words = [w for w in re.split(r"[/_.-]", urlsplit(page_url).path.lower())
                  if len(w) > 2]

    def score(url: str) -> int:
        parts = urlsplit(url)
        candidate_host, path = parts.netloc.lower(), parts.path.lower()
        points = 0
        if candidate_host == host:
            points += 3
        elif candidate_host.endswith(registrable):
            points += 4
        if any(word in path for word in path_words):
            points += 3
        if path.endswith(_DATA_SUFFIXES):
            points += 6
        if any(word in url.lower() for word in _DATA_WORDS):
            points += 2
        if path.endswith(_ASSET_SUFFIXES):
            points -= 6
        if path.endswith((".html", ".htm")) or "story?id=" in url:
            points -= 8
        if any(junk in url.lower() for junk in _JUNK_HOSTS):
            points -= 20
        return points

    cleaned = {_clean_url(u) for u in urls}
    ranked = sorted(((score(u), len(u), u) for u in cleaned), key=lambda t: (-t[0], t[1]))
    return [u for points, _, u in ranked if points > 0][:MAX_CANDIDATES]
