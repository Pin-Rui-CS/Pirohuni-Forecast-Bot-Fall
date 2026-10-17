from __future__ import annotations

from bot.scrapers.adapters.GoogleSheets import GoogleSheetsAdapter
from bot.scrapers.adapters.GoogleTrends import GoogleTrendsAdapter
from bot.scrapers.adapters.Metaculus import MetaculusAdapter
from bot.scrapers.adapters.Pdf import PdfAdapter
from bot.scrapers.adapters.base import UrlAdapter
from bot.scrapers.adapters.Wikipedia import WikipediaAdapter
from bot.scrapers.adapters.YahooQuotes import YahooQuotesAdapter


ADAPTERS: list[UrlAdapter] = [
    MetaculusAdapter(),
    GoogleTrendsAdapter(),
    GoogleSheetsAdapter(),
    WikipediaAdapter(),
    YahooQuotesAdapter(),
    # Extension-based, so it goes after the host-specific adapters.
    PdfAdapter(),
]


def find_adapter(url: str) -> UrlAdapter | None:
    for adapter in ADAPTERS:
        if adapter.can_handle(url):
            return adapter
    return None
