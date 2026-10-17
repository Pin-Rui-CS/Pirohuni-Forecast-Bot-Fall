"""URL-specific scraping adapters used before generic crawlers."""

from bot.scrapers.adapters.base import AdapterResult, UrlAdapter
from bot.scrapers.adapters.registry import find_adapter

__all__ = ["AdapterResult", "UrlAdapter", "find_adapter"]
