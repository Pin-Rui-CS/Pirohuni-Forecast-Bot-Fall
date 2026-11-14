"""SEC EDGAR filing data files, read as filed.

Question 46726 (2026-11-13): Berkshire's 13F information table
(.../000119312526352200/56757.xml) could not be downloaded plainly -- EDGAR
answers 403 to a client that looks like a browser and is not one -- so it went
through the generic scrape rungs, which returned its text with every tag and
separator stripped ("...02079K305596807916700SHDFND2,4,111670000..."). A model
then split 16,700 shares into 16,700,000 and the brief carried a false
122.7M-share baseline against the question's 105,979,600.

EDGAR serves these files to a client that says what it is. This adapter
identifies itself exactly as the public-data kit already does on every run
(apiagent-kit/src/core/http.ts: APIAGENT_UA_PRODUCT / APIAGENT_UA_URL, plus
SEC_EDGAR_CONTACT when set), and to sec.gov only.

Scope: the data files under /Archives/edgar/ (.xml, .txt, .json). The
"xslForm..." addresses EDGAR links from a filing index are the same XML dressed
as a web page; they are mapped back to the file itself. Ordinary filing pages
(.htm) stay with the generic rungs, which render them well.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from bot.scrapers.adapters.base import AdapterResult, UrlAdapter

_HOSTS = {"www.sec.gov", "sec.gov"}
_DATA_SUFFIXES = (".xml", ".txt", ".json")
_STYLED = re.compile(r"/xsl[^/]+/")
# A filing's data table runs to tens of kB; a 10-K's complete submission to
# tens of MB. Past this the file is left to the generic rungs.
MAX_BYTES = 3_000_000
MAX_CONTENT_CHARS = 300_000


def user_agent() -> str:
    """The declared-crawler name the public-data kit sends (same env, same
    defaults, same shape), so EDGAR sees one identity from this bot."""
    product = os.getenv("APIAGENT_UA_PRODUCT", "").strip() or "SoCLaaS-Experiments/1.0"
    site = os.getenv("APIAGENT_UA_URL", "").strip() or "https://soclaas-experiments.vercel.app"
    contact = os.getenv("SEC_EDGAR_CONTACT", "").strip()
    return f"{product} (+{site}; {contact})" if contact else f"{product} (+{site})"


def data_file_url(url: str) -> str | None:
    """The address of the data file behind a sec.gov URL, or None when the URL
    is not an EDGAR data file."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in _HOSTS or not parts.path.startswith("/Archives/edgar/"):
        return None
    path = _STYLED.sub("/", parts.path)
    # A styled address may end ".html" for a file stored as ".xml".
    if path != parts.path and path.lower().endswith(".html"):
        path = path[:-5] + ".xml"
    if not path.lower().endswith(_DATA_SUFFIXES):
        return None
    return urlunsplit(("https", "www.sec.gov", path, "", ""))


class SecEdgarAdapter(UrlAdapter):
    @property
    def name(self) -> str:
        return "sec-edgar"

    def can_handle(self, url: str) -> bool:
        return data_file_url(url) is not None

    async def raw_page(self, url: str, timeout: float = 30.0) -> str | None:
        target = data_file_url(url)
        if target is None:
            return None
        chunks, total = [], 0
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": user_agent()}) as client:
            async with client.stream("GET", target) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        return None
                    chunks.append(chunk)
        return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace") or None

    async def extract(self, url: str, query: str = "", timeout: float = 30.0) -> AdapterResult:
        text = await self.raw_page(url, timeout)
        if not text:
            raise ValueError("EDGAR data file is empty or over the size limit")
        target = data_file_url(url)
        note = "" if len(text) <= MAX_CONTENT_CHARS else (
            f"\n\n[cut at {MAX_CONTENT_CHARS:,} of {len(text):,} characters]")
        content = (f"SEC EDGAR data file, as filed (tags kept so each number stays with its field).\n"
                   f"Source: {target}\n\n{text[:MAX_CONTENT_CHARS]}{note}")
        return AdapterResult(url=url, adapter=self.name, content=content,
                             metadata={"source_url": target, "characters": len(text)})
