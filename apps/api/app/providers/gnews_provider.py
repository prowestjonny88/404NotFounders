from __future__ import annotations

import logging
from functools import cached_property
from typing import Any

import httpx

from app.core.exceptions import ProviderError

logger = logging.getLogger(__name__)

BUCKET_A_QUERIES = [
    "polypropylene shipping disruption Asia",
    "petrochemical supply disruption Asia",
    "port congestion Asia plastics",
    "PP resin price Asia",
]

BUCKET_B_QUERIES = [
    "ringgit outlook Malaysia imports",
    "Malaysia ringgit geopolitical risk oil shipping",
    "oil prices Asia plastics manufacturing",
    "USD MYR Malaysia manufacturing",
    "Malaysia polymer import tariff",
]

BUCKET_C_QUERIES = [
    "geopolitical risk Asia shipping oil prices",
    "Red Sea shipping disruption Asia ports",
    "Middle East conflict oil freight Asia",
    "US China trade tariffs plastics Asia",
]


class GNewsProvider:
    """Wraps the gnews library, which reads Google News RSS without an API key."""

    API_URL = "https://gnews.io/api/v4/search"

    def __init__(
        self,
        max_results: int = 5,
        language: str = "en",
        country: str = "MY",
        api_key: str | None = None,
    ) -> None:
        self.max_results = max_results
        self.language = language
        self.country = country
        self.api_key = api_key

    @cached_property
    def client(self):  # type: ignore[return]
        try:
            from gnews import GNews  # type: ignore[import]
        except ImportError as exc:
            raise ProviderError("gnews library is not installed. Run: pip install gnews") from exc
        return GNews(
            language=self.language,
            country=self.country,
            max_results=self.max_results,
        )

    def fetch_articles(self, query: str) -> list[dict[str, Any]]:
        if self.api_key:
            return self._fetch_articles_from_api(query)
        return self._fetch_articles_from_rss(query)

    def _fetch_articles_from_api(self, query: str) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "lang": self.language,
            "country": self.country,
            "max": self.max_results,
            "in": "title,description",
            "apikey": self.api_key,
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(self.API_URL, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise ProviderError(f"GNews API fetch failed for '{query}': {exc}") from exc

        results: list[dict[str, Any]] = []
        for item in payload.get("articles", []) if isinstance(payload, dict) else []:
            source = item.get("source") or {}
            results.append(
                {
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "published_at": item.get("publishedAt", ""),
                    "source": {"name": source.get("name", "") if isinstance(source, dict) else str(source)},
                    "url": item.get("url", ""),
                    "_provider_mode": "gnews_api",
                }
            )
        return results

    def _fetch_articles_from_rss(self, query: str) -> list[dict[str, Any]]:
        try:
            raw = self.client.get_news(query)
        except Exception as exc:
            raise ProviderError(f"GNews RSS fetch failed for '{query}': {exc}") from exc

        results: list[dict[str, Any]] = []
        for item in raw or []:
            publisher = item.get("publisher") or {}
            source_name = publisher.get("title", "") if isinstance(publisher, dict) else str(publisher)
            results.append(
                {
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "published_at": item.get("published date", ""),
                    "source": {"name": source_name},
                    "url": item.get("url", ""),
                    "_provider_mode": "gnews_rss",
                }
            )
        return results

    def fetch_bucket_a(self) -> list[dict[str, Any]]:
        return self._fetch_bucket(BUCKET_A_QUERIES, "logistics")

    def fetch_bucket_b(self) -> list[dict[str, Any]]:
        return self._fetch_bucket(BUCKET_B_QUERIES, "finance")

    def fetch_bucket_c(self) -> list[dict[str, Any]]:
        return self._fetch_bucket(BUCKET_C_QUERIES, "geopolitical")

    def _fetch_bucket(self, queries: list[str], bucket_label: str) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        for query in queries:
            try:
                batch = self.fetch_articles(query)
            except ProviderError as exc:
                logger.warning("GNews bucket '%s' query failed: %s", query, exc)
                continue
            for article in batch:
                article["_query_bucket"] = bucket_label
                article["_query"] = query
            articles.extend(batch)
            logger.debug("GNews '%s' returned %d articles", query, len(batch))
        return articles
